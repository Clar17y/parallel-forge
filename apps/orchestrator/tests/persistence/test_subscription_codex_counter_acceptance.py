"""Codex primary protocol completes the offline counter delivery to the human PR gate."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_subscription_counter_acceptance as counter_fixture
from forge.agents.codex_gateway import (
    CodexCapabilityReport,
    CodexInstallation,
    codex_account_identity,
)
from forge.agents.codex_runtime import CodexRuntimeAdapter
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.approval import ApprovalGate
from forge.domain.policy import RunnerMode
from forge.domain.run import RunState
from forge.domain.subscription import (
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    UnknownTelemetryPolicy,
)
from forge.domain.tool import ToolName
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.subscription_fixtures import _run_git_command, release_slow_unit_barrier
from forge.persistence.models import Approval
from forge.persistence.models.subscription import SubscriptionClientLaunch
from forge.persistence.models.subscription_quota import SubscriptionQuotaObservation
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.worker.composition import compose_worker_handlers
from forge.worker.subscription_broker import DurableClientProcessLifecycle
from sqlalchemy import func, select
from subscription_counter_manifest import retain_counter_manifest
from subscription_worktree_case import command_once
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import PATHS, WRITER, CounterScript, prepared_counter_case
from test_subscription_counter_docker import (
    assert_docker_execution,
    counter_runner_image,  # noqa: F401
)

from apps.orchestrator.tests.agents.capability_support import bind_fake_capability_report

PRIMARY = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")


@pytest.fixture
def clean_peer_exit(monkeypatch):
    errors = []
    original = DurableClientProcessLifecycle.finished

    async def finished(self, receipt, result):
        if result is not None and result.stderr:
            errors.append(result.stderr)
        await original(self, receipt, result)

    monkeypatch.setattr(DurableClientProcessLifecycle, "finished", finished)
    yield
    assert errors == [], errors


class CodexCounterScript(CounterScript):
    """Only the writer uses the existing synthetic gateway; primary transport is real."""

    def __init__(self, directory):
        super().__init__()
        isolated, home = directory / "isolated-client", directory / "client-home"
        isolated.mkdir()
        home.mkdir()
        self.installation = CodexInstallation(
            executable=sys.executable,
            cwd=str(isolated),
            client_home=str(home),
            model=PRIMARY.model,
            effort=PRIMARY.effort.value,
            account=codex_account_identity("codex@example.invalid"),
            executable_digest=hashlib.sha256(
                Path(sys.executable).resolve(strict=True).read_bytes()
            ).hexdigest(),
            duration_seconds=60,
            script=(str(Path(__file__).parents[1] / "agents/codex_counter_peer.py"),),
        )
        report = CodexCapabilityReport(
            supported=True,
            installed_version="0.153.4",
            account_kind="chatgpt",
            billing_allowance_enforced=True,
            native_tools_isolated=True,
            model=PRIMARY.model,
            effort=PRIMARY.effort.value,
            client_home=str(home),
            account=self.installation.account,
            executable_digest=self.installation.executable_digest,
        )

        def verify(installation, scope):
            assert installation == self.installation
            return bind_fake_capability_report(
                report,
                scope=scope,
                client_version="0.153.4",
                executable_digest=installation.executable_digest,
                client_home=installation.client_home,
                account=installation.account,
                verifier_id="fake-codex-verification",
            )

        self.verifier = SimpleNamespace(verify=verify)

    def adapter(self, route):
        if route == PRIMARY:
            return CodexRuntimeAdapter(self.installation, self.verifier)
        assert route == WRITER
        return super().adapter(route)


@pytest.mark.integration
@pytest.mark.parametrize(
    "runner_mode",
    [RunnerMode.TRUSTED_HOST, pytest.param(RunnerMode.DOCKER, marks=pytest.mark.docker)],
    ids=["trusted-host", "docker"],
)
async def test_codex_primary_delegates_repairs_recovers_and_retains_final_human_gate(
    session_factory, tmp_path, monkeypatch, clean_peer_exit, runner_mode, request
):
    runner_image = (
        request.getfixturevalue("counter_runner_image") if runner_mode is RunnerMode.DOCKER else ""
    )
    monkeypatch.setattr(counter_fixture, "PRIMARY", PRIMARY)
    requests = []
    original_build = SubscriptionRequestBuilder.build

    async def record_request(self, admission):
        request = await original_build(self, admission)
        requests.append(request)
        return request

    monkeypatch.setattr(SubscriptionRequestBuilder, "build", record_request)
    script = CodexCounterScript(tmp_path)
    case = await prepared_counter_case(
        session_factory,
        tmp_path,
        script=script,
        runner_mode=runner_mode,
        runner_image=runner_image,
        primary_budget=TaskBudget(
            max_provider_attempts=8,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=6),
        ),
    )
    handlers = case.handlers
    try:
        worked = await case.worker.run_once()
        assert not script.errors, script.errors
        assert worked.attempt.result.failure is None
        assert worked.attempt.settlement.disposition == "decision_pending"
        await handlers.aclose()
        handlers = compose_worker_handlers(
            case.settings,
            session_factory,
            subscription_adapters=(script.adapter(PRIMARY), script.adapter(WRITER)),
        )
        case.handlers = handlers
        recovery = await handlers.subscription_decision_recovery.reconcile_all()
        assert recovery.applied == 1 and recovery.deferred == recovery.unsupported == 0
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 0
        worker = handlers.subscription_invocations("codex-counter-restarted")
        outcomes = []
        for expected in ("task_accepted", "review_selected", "acceptance_validation_queued"):
            outcome = await worker.run_once()
            assert outcome is not None and outcome.attempt.result.failure is None
            assert outcome.application.accepted and outcome.application.disposition == expected
            outcomes.append(outcome)
        async with case.factory() as work:
            tree = Path((await work.runs.get(case.run.id)).worktree_path)
        # Final validation runs every required policy check, including slow-unit.
        # Only the harness controls its reserved synchronization directory.
        release_slow_unit_barrier(tree)
        await command_once(case, session_factory, case.run.id, "validate")
        async with case.factory() as work:
            validated = await work.runs.get(case.run.id)
            events = await work.events.list_after(case.run.id, 0)
        assert validated.state is RunState.AWAITING_PR_APPROVAL, [
            (event.event_type, event.payload.get("failed_checks")) for event in events[-5:]
        ]
        assert await worker.run_once() is None
        primary_requests = [r for r in requests if r.task.purpose is SpecialistPurpose.PRIMARY]
        assert len(requests) == 6 and len(primary_requests) == 5 and len(script.requests) == 1
        assert all(r.task.route.effective == PRIMARY for r in primary_requests)
        assert script.requests[0].task.route.effective == WRITER
        assert script.requests[0].task.owned_paths == PATHS
        assert len({r.attempt.attempt_id for r in requests}) == 6
        for outcome in (case.planned, case.delegated, *outcomes):
            telemetry = outcome.attempt.result.telemetry
            assert telemetry.input_tokens == 31 and telemetry.output_tokens == 17
            assert telemetry.is_quota_known is False
        primary_attempts = {r.attempt.attempt_id for r in primary_requests}
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            assert (
                run.state is RunState.AWAITING_PR_APPROVAL and run.pending_gate is ApprovalGate.PR
            )
            assert run.pending_evidence_digest
            approvals = list((await work.session.scalars(select(Approval))).all())
            assert len(approvals) == 1 and approvals[0].gate == "plan"
            launches = list((await work.session.scalars(select(SubscriptionClientLaunch))).all())
            assert len(launches) == 6 and all(row.state == "terminal" for row in launches)
            physical = [row for row in launches if row.attempt_id in primary_attempts]
            assert len(physical) == 5
            assert len({row.launch_id for row in physical}) == 5
            assert all(
                row.terminal_payload["stop_confirmed"]
                and row.terminal_payload["pid"] > 0
                and row.terminal_payload["process_identity"] != "a1-script"
                for row in physical
            )
            physical_launches = [dict(row.terminal_payload) for row in physical]
            source = await work.session.get(
                SubscriptionAttemptResult, outcomes[0].admission.attempt.attempt_id
            )
            assert source.application_payload["handoff_attempt_id"] == str(
                worked.admission.attempt.attempt_id
            )
            usage = await work.subscription_budget.usage(run.id)
            assert usage.consumed.provider_attempts == 6 and usage.consumed.repairs == 0
            assert usage.consumed.named_checks == 3 and usage.outstanding.provider_attempts == 0
            assert await work.scheduler._active_count() == 0
            assert (
                await work.session.scalar(
                    select(func.count()).select_from(SubscriptionQuotaObservation)
                )
                == 0
            )
            calls = await work.tool_calls.list_for_run(run.id)
            assert any(
                call.subscription_attempt_id in primary_attempts
                and call.tool_name is ToolName.GIT_COMMIT
                for call in calls
            )
        tree = Path(run.worktree_path)
        assert _run_git_command(["git", "status", "--porcelain"], tree).stdout == ""
        changed = _run_git_command(
            ["git", "diff", "--name-only", run.base_sha], tree
        ).stdout.splitlines()
        assert set(changed) == set(PATHS)
        assert "value + 1" in (tree / PATHS[0]).read_text(encoding="utf-8")
        store = FilesystemArtifactStore(case.settings.artifact_root)
        passed = next(
            item
            for item in script.receipts
            if item["tool_name"] == ToolName.BUILD_RUN_NAMED_CHECK.value
            and item["status"] == "succeeded"
        )
        grade = await read_check_evidence(
            case.fixture.case_contract,
            store,
            {**passed, "tool_call_id": passed["operation_id"]},
            command_name="unit",
        )
        assert grade is not None and all(grade[0].values()) and all(grade[1].values())
        manifest = await retain_counter_manifest(
            case.factory,
            store,
            case.fixture,
            SimpleNamespace(requests=requests, receipts=script.receipts),
            run_id=run.id,
            tmp_path=tmp_path,
            grade=grade,
            scenario="A1-Codex-primary-protocol",
            operator_view={
                "physical_primary_launches": physical_launches,
                "synthetic_writer_launches": 1,
                "human_pr_approval_pending": True,
                "final_evidence_digest": run.pending_evidence_digest,
            },
        )
        evidence = json.loads(manifest.read_text(encoding="utf-8"))["evidence"]
        assert run.pending_evidence_digest in evidence["artifacts"]
        assert (manifest.parent / run.pending_evidence_digest).is_file()
        if runner_mode is RunnerMode.DOCKER:
            results = assert_docker_execution(manifest, runner_image)
            assert len(results) >= 3
    finally:
        await handlers.aclose()
