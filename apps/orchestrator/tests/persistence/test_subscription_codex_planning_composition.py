"""Forge's concrete Codex adapter reaches plan approval using a supervised fake peer."""

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_subscription_plan_gate as plan_gate_fixture
import test_subscription_planning_start as planning_fixture
from forge.agents.codex_gateway import (
    CodexCapabilityReport,
    CodexInstallation,
    codex_account_identity,
)
from forge.agents.codex_runtime import CodexRuntimeAdapter
from forge.application.services.projects import PolicyUpdateRequest, ProjectService
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateOutcome
from forge.application.services.subscription_planning import SubscriptionPlanningService
from forge.domain.approval import ApprovalGate
from forge.domain.run import RunState
from forge.domain.subscription import (
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    UnknownTelemetryPolicy,
)
from forge.domain.tool import ToolName
from forge.evaluations.subscription_fixtures import get_acceptance_command_specs
from forge.persistence.models.subscription import SubscriptionClientLaunch
from forge.persistence.models.subscription_quota import SubscriptionQuotaObservation
from forge.persistence.models.subscription_recovery import SubscriptionApplicationDiagnostic
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.worker.composition import compose_worker_handlers
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_preparation import preparation_case
from test_subscription_usage import _reservation

from apps.orchestrator.tests.agents.capability_support import bind_fake_capability_report


@pytest.mark.integration
async def test_concrete_codex_registration_uses_bound_read_and_preserves_human_plan_gate(
    session_factory, tmp_path, monkeypatch
):
    primary = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    monkeypatch.setattr(planning_fixture, "_route", lambda _: primary)
    original_seed = planning_fixture._seed_project_task

    async def seed_with_named_check(session_factory, directory):
        actor, project_id, task_id = await original_seed(session_factory, directory)
        await ProjectService(lambda: PostgresUnitOfWork(session_factory)).update_policy(
            actor=actor,
            project_id=project_id,
            idempotency_key="codex-plan-check-policy",
            request=PolicyUpdateRequest(
                expected_policy_version=1, commands=get_acceptance_command_specs()
            ),
        )
        return actor, project_id, task_id

    monkeypatch.setattr(planning_fixture, "_seed_project_task", seed_with_named_check)
    factory, original, command = await planning_fixture.planning_start_case(
        session_factory, tmp_path
    )
    (tmp_path / "repo/README.md").write_text(
        "Plan input from the registered repository", encoding="utf-8"
    )
    async with factory() as work:
        await SubscriptionPlanningService(TaskBudget(
            max_provider_attempts=64,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=5),
        )).execute(
            command, work
        )
    from forge.persistence.repositories.commands import PostgresCommandRepository

    await PostgresCommandRepository(session_factory).complete(
        command.id, worker_id="planning-owner"
    )
    isolated, home = tmp_path / "isolated-client", tmp_path / "client-home"
    isolated.mkdir()
    home.mkdir()
    installation = CodexInstallation(
        executable=sys.executable,
        cwd=str(isolated),
        client_home=str(home),
        model=primary.model,
        effort=primary.effort.value,
        account=codex_account_identity("codex@example.invalid"),
        executable_digest=hashlib.sha256(
            Path(sys.executable).resolve(strict=True).read_bytes()
        ).hexdigest(),
        script=(
            str(Path(__file__).parents[1] / "agents/codex_notification_peer.py"),
            "plan",
            primary.model,
            primary.effort.value,
            "0",
        ),
        duration_seconds=10,
    )
    # The process and capability source are explicit fixtures; all construction,
    # request binding, transport, broker effects and settlement use Forge code.
    report = CodexCapabilityReport(
        supported=True,
        installed_version="0.153.4",
        account_kind="chatgpt",
        native_tools_isolated=True,
        model=primary.model,
        effort=primary.effort.value,
        client_home=str(home),
        account=installation.account,
        executable_digest=installation.executable_digest,
    )

    def verify(value, scope):
        assert value == installation
        return bind_fake_capability_report(
            report,
            scope=scope,
            client_version="0.153.4",
            executable_digest=value.executable_digest,
            client_home=value.client_home,
            account=value.account,
            verifier_id="fake-codex-verification",
        )

    handlers = compose_worker_handlers(
        Settings(
            data_root=tmp_path,
            prompt_root=Path(__file__).resolve().parents[4] / "agents",
            provider_secret_reference="",
            subscription_attempt_budget=_reservation(),
        ),
        session_factory,
        subscription_adapters=(CodexRuntimeAdapter(installation, SimpleNamespace(verify=verify)),),
    )
    try:
        worker = handlers.subscription_invocations("concrete-codex-primary")
        outcome = await worker.run_once()
        assert outcome is not None and isinstance(outcome.application, SubscriptionPlanGateOutcome)
        assert outcome.attempt.result.failure is None
        proof = outcome.attempt.result.launch_proof
        assert proof.permits_decision and proof.process_identity and proof.pid > 0
        assert await worker.run_once() is None
        async with factory() as work:
            run = await work.runs.get(original.id)
            assert (
                run.state is RunState.AWAITING_PLAN_APPROVAL
                and run.pending_gate is ApprovalGate.PLAN
            )
            assert run.pending_evidence_digest == outcome.application.evidence_digest
            envelope = await work.subscription.envelope_for_run(run.id)
            assert envelope.route_for(SpecialistPurpose.PRIMARY).effective == primary
            calls = await work.tool_calls.list_for_run(run.id)
            assert len(calls) == 1 and calls[0].tool_name is ToolName.REPOSITORY_READ_FILE
            assert calls[0].subscription_attempt_id == outcome.admission.attempt.attempt_id
            result = await work.session.get(
                SubscriptionAttemptResult, outcome.admission.attempt.attempt_id
            )
            assert result.accepted
            launches = list((await work.session.scalars(select(SubscriptionClientLaunch))).all())
            assert len(launches) == 1 and launches[0].state == "terminal"
            assert launches[0].terminal_payload["launch_id"] == proof.launch_id
            assert (
                await work.session.scalar(
                    select(func.count()).select_from(SubscriptionQuotaObservation)
                )
                == 0
            )
            usage = await work.subscription_budget.usage(run.id)
            assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 0
            assert (
                usage.outstanding.provider_attempts == 0
                and await work.scheduler._active_count() == 0
            )
            assert outcome.attempt.result.telemetry.input_tokens == 13
            assert outcome.attempt.result.telemetry.is_quota_known is False
    finally:
        await handlers.aclose()

    # Continue that same real-client plan through human approval and preparation,
    # then send an illegal implementation response through the real adapter.
    from forge.application.ports.worktrees import ManagedWorktree
    from forge.application.services.approved_plan import ApprovedPlanLoader
    from forge.application.services.delivery_preparation import DeliveryPreparationService
    from forge.application.services.plan_evidence import (
        PlanEvidenceValidator,
        build_subscription_plan_evidence,
    )
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.domain.resource import WorktreeIdentity
    from forge.persistence.models import Run, RunCommand
    from test_subscription_plan_gate import approve_proposal

    store = FilesystemArtifactStore(tmp_path / "artifacts")
    async with factory() as work:
        proposal = await work.subscription_plan_gate.proposal(outcome.admission.attempt.attempt_id)
        run = await work.runs.get(proposal.producer.run_id)
        evidence = await build_subscription_plan_evidence(
            work, run, proposal.producer, proposal.result_digest, proposal.plan
        )
        await work.rollback()
    inspector = planning_fixture.StableInspector(tmp_path / "repo")
    validator = PlanEvidenceValidator(store, inspector, data_root=str(tmp_path / "data"))
    await approve_proposal(factory, session_factory, evidence, validator, outcome.application)
    async with session_factory() as session:
        approval_command_id = await session.scalar(select(RunCommand.id).where(
            RunCommand.run_id == original.id, RunCommand.command_type == "approve_plan"
        ))
    await PostgresCommandRepository(session_factory).complete(
        approval_command_id, worker_id="approval-worker"
    )
    prepare_command = await PostgresCommandRepository(session_factory).claim_next(
        worker_id="prepare-worker", lease_seconds=30
    )
    assert prepare_command is not None and prepare_command.command_type == "prepare_worktree"

    class Provisioner:
        async def prepare(self, run_id, policy):
            async with factory() as work:
                run = await work.runs.get(run_id)
                row = await work.session.get(Run, run_id)
                row.worktree_path = str(tmp_path / "prepared")
                await work.commit()
                return ManagedWorktree(
                    identity=WorktreeIdentity.for_run(
                        run.project_id, run.id, run.branch_name, policy.database.enabled
                    ),
                    path=tmp_path / "prepared", base_sha=run.base_sha,
                )

    async with factory() as work:
        await DeliveryPreparationService(ApprovedPlanLoader(store), Provisioner()).execute(
            prepare_command, work
        )
    illegal_installation = CodexInstallation(
        executable=installation.executable, cwd=installation.cwd,
        client_home=installation.client_home, model=installation.model, effort=installation.effort,
        account=installation.account, executable_digest=installation.executable_digest,
        script=(str(Path(__file__).parents[1] / "agents/codex_notification_peer.py"),
                "illegal_primary_handoff", primary.model, primary.effort.value, "0"),
        duration_seconds=10,
    )
    from forge.application.services.subscription_requests import SubscriptionRequestBuilder

    implementation_requests = []
    original_build = SubscriptionRequestBuilder.build

    async def capture_implementation(self, admission):
        request = await original_build(self, admission)
        implementation_requests.append(request)
        return request

    monkeypatch.setattr(SubscriptionRequestBuilder, "build", capture_implementation)
    illegal_handlers = compose_worker_handlers(
        Settings(
            data_root=tmp_path, prompt_root=Path(__file__).resolve().parents[4] / "agents",
            provider_secret_reference="", subscription_attempt_budget=_reservation(),
        ),
        session_factory,
        subscription_adapters=(CodexRuntimeAdapter(
            illegal_installation, SimpleNamespace(verify=lambda value, scope: bind_fake_capability_report(
                report, scope=scope, client_version="0.153.4",
                executable_digest=value.executable_digest, client_home=value.client_home,
                account=value.account, verifier_id="fake-codex-verification",
            ))
        ),),
    )
    try:
        implementation_worker = illegal_handlers.subscription_invocations("prepared-primary")
        implementation = await implementation_worker.run_once()
        assert implementation is not None
        assert implementation.attempt.result.role_rejection.kind == "handoff"
        assert implementation.attempt.settlement.disposition == "role_correction_queued"
        correction = await implementation_worker.run_once()
        assert correction is not None
        assert correction.attempt.result.role_rejection.kind == "handoff"
        assert correction.attempt.settlement.disposition == "role_rejected"
        assert implementation_requests[0].untrusted_context["approved_implementation"] is not None
        assert implementation_requests[1].untrusted_context["role_correction"]["kind"] == "handoff"
        async with factory() as work:
            diagnostic = await work.session.get(
                SubscriptionApplicationDiagnostic, correction.admission.attempt.attempt_id
            )
            assert diagnostic.resolution == "attention"
            usage = await work.subscription_budget.usage(original.id)
            assert usage.consumed.provider_attempts == 3 and usage.consumed.repairs == 1
    finally:
        await illegal_handlers.aclose()


@pytest.mark.integration
async def test_real_codex_illegal_primary_output_gets_one_typed_correction_and_feedback(
    session_factory, tmp_path, monkeypatch
):
    from forge.application.services.subscription_requests import SubscriptionRequestBuilder

    built_requests = []
    original_build = SubscriptionRequestBuilder.build

    async def capture_request(self, admission):
        request = await original_build(self, admission)
        built_requests.append(request)
        return request

    monkeypatch.setattr(SubscriptionRequestBuilder, "build", capture_request)
    primary = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    monkeypatch.setattr(plan_gate_fixture, "_route", lambda _: primary)
    factory, evidence, command, preparation, _ = await preparation_case(
        session_factory,
        tmp_path,
        plan_scope=("apps",),
        primary_budget=TaskBudget(
            max_provider_attempts=5, max_repairs=3,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=5),
        ),
    )
    async with factory() as work:
        await preparation.execute(command, work)
    isolated, home = tmp_path / "isolated-client", tmp_path / "client-home"
    isolated.mkdir()
    home.mkdir()
    installation = CodexInstallation(
        executable=sys.executable,
        cwd=str(isolated),
        client_home=str(home),
        model=primary.model,
        effort=primary.effort.value,
        account=codex_account_identity("codex@example.invalid"),
        executable_digest=hashlib.sha256(
            Path(sys.executable).resolve(strict=True).read_bytes()
        ).hexdigest(),
        script=(
            str(Path(__file__).parents[1] / "agents/codex_notification_peer.py"),
            "illegal_primary_handoff", primary.model, primary.effort.value, "0",
        ),
        duration_seconds=10,
    )
    report = CodexCapabilityReport(
        supported=True, installed_version="0.153.4", account_kind="chatgpt",
        native_tools_isolated=True, model=primary.model, effort=primary.effort.value,
        client_home=str(home), account=installation.account,
        executable_digest=installation.executable_digest,
    )
    def verify(value, scope):
        return bind_fake_capability_report(
            report, scope=scope, client_version="0.153.4",
            executable_digest=value.executable_digest,
            client_home=value.client_home, account=value.account,
            verifier_id="fake-codex-verification",
        )
    handlers = compose_worker_handlers(
        Settings(
            data_root=tmp_path,
            prompt_root=Path(__file__).resolve().parents[4] / "agents",
            provider_secret_reference="",
            subscription_attempt_budget=_reservation(),
        ),
        session_factory,
        subscription_adapters=(CodexRuntimeAdapter(installation, SimpleNamespace(verify=verify)),),
    )
    try:
        worker = handlers.subscription_invocations("illegal-primary")
        first = await worker.run_once()
        assert first is not None
        assert first.attempt.result.decision is None
        assert first.attempt.result.role_rejection.kind == "handoff"
        assert first.attempt.settlement.disposition == "role_correction_queued"
        second = await worker.run_once()
        assert second is not None
        assert second.admission.attempt.attempt_number == first.admission.attempt.attempt_number + 1
        assert built_requests[1].untrusted_context["role_correction"]["kind"] == "handoff"
        assert second.attempt.settlement.disposition == "role_rejected"
        async with factory() as work:
            diagnostic = await work.session.get(
                SubscriptionApplicationDiagnostic, second.admission.attempt.attempt_id
            )
            assert diagnostic.resolution == "attention"
            usage = await work.subscription_budget.usage(evidence.producer.run_id)
            assert usage.consumed.provider_attempts == 3 and usage.consumed.repairs == 1
    finally:
        await handlers.aclose()
