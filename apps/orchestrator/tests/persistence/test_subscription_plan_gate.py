"""Real settled subscription plans enter the existing evidence-bound human gate."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.projects import RepositoryInspection
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.approval import SubscriptionPlanApprovalEvidence, SubscriptionPlanProducer
from forge.domain.operation import canonical_digest
from forge.domain.plan import PlanOutput, ScopedPlanOutput
from forge.domain.policy import CommandSpec, ProjectPolicy, StepKind
from forge.domain.run import RunSnapshot, RunState
from forge.domain.scheduling import ScheduleTask
from forge.domain.subscription import decode_subscription_record, encode_subscription_record
from forge.domain.tool import repository_resource_identity
from forge.persistence.models import Project, ProjectPolicyVersion, Run, Task
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.tasks import compute_task_digest
from forge.persistence.unit_of_work import PostgresUnitOfWork
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_usage import _known, _reservation


async def proposal_case(
    session_factory, tmp_path, *, primary_budget=None, plan_scope=None, plan_checks=("unit",), review_route=None
):
    factory = lambda: PostgresUnitOfWork(session_factory)
    project_id, task_id = uuid4(), uuid4()
    persisted_run = RunSnapshot(
        id=uuid4(), project_id=project_id, task_id=task_id, policy_version=1
    )
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(tmp_path),
        github_repository=f"example/project-{project_id}",
        default_branch="main",
        commands=()
        if plan_scope is None
        else (
            CommandSpec(
                kind=StepKind.TEST, name="unit", argv=("python", "-m", "pytest"), timeout_seconds=60
            ),
        ),
    )
    external_digest = compute_task_digest(
        title="Plan task",
        body="",
        source_url=None,
        source_updated_at=None,
        external_source=None,
        external_id=None,
    )
    async with factory() as work:
        project = Project(
            id=project_id,
            canonical_path=str(tmp_path),
            github_repository=policy.github_repository,
            default_branch="main",
        )
        document = policy.model_dump(mode="json")
        record = ProjectPolicyVersion(
            project_id=project_id,
            version=1,
            document=document,
            policy_digest=hashlib.sha256(
                json.dumps(
                    document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            document_schema_version=1,
        )
        task = Task(
            id=task_id,
            project_id=project_id,
            title="Plan task",
            body="",
            normalized_text="Plan task",
            task_digest=external_digest,
        )
        work.session.add_all([project, record, task])
        await work.session.flush()
        project.current_policy_version = 1
        await work.session.flush()
        await work.runs.create(persisted_run)
        run = await work.session.get(Run, persisted_run.id)
        run.state, run.base_ref, run.base_sha = RunState.PLANNING.value, "refs/heads/main", "a" * 40
        primary = await _admit_run(
            work, persisted_run, (_route("p"), _route("p")), budget=primary_budget, review_route=review_route
        )
        if plan_scope is not None:
            primary_row = await work.session.get(SubscriptionTask, primary)
            primary_row.payload = encode_subscription_record(
                replace(
                    decode_subscription_record(primary_row.payload), owned_paths=(), named_checks=()
                )
            )
        await work.scheduler.enqueue(
            ScheduleTask(
                run_id=run.id,
                task_id=primary,
                worktree_id=repository_resource_identity(project_id),
                owned_paths=("apps",) if plan_scope is None else (),
                max_repairs=3,
            )
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("planner", _reservation())
    assert admission is not None and admission.task.task_id == primary
    plan = PlanOutput(
        summary="Implement one bounded change",
        assumptions=(),
        affected_components=("apps",),
        steps=("Implement and validate",),
        required_checks=plan_checks,
        risks=("Implementation risk",),
        security_considerations=(),
        dependency_changes=(),
    )
    if plan_scope is not None:
        plan = ScopedPlanOutput(**plan.model_dump(), owned_paths=plan_scope)
    launch_proof = await record_stopped_launch(session_factory, admission)
    telemetry = _known()
    outcome = await executor.settle(
        admission,
        SubscriptionInvocationResult(
            attempt=admission.attempt, decision=plan, telemetry=telemetry, launch_proof=launch_proof
        ),
    )
    assert outcome.disposition == "decision_pending"
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.result_payload["schema_version"] == 4
        assert canonical_digest(result.result_payload) == result.result_digest
        result_digest = result.result_digest
        context = result.result_payload["proposal_context"]
        assert context["task"] == encode_subscription_record(admission.task)
        await work.rollback()
    plan_digest = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()
    producer = SubscriptionPlanProducer(
        run_id=admission.attempt.run_id,
        task_id=primary,
        attempt_id=admission.attempt.attempt_id,
        plan_attempt=1,
        plan_digest=plan_digest,
        task_digest=canonical_digest(context["task"]),
        envelope_digest=canonical_digest(context["envelope"]),
        route_digest=canonical_digest(context["route"]),
        budget_digest=canonical_digest(context["budget"]),
        telemetry={
            "input_tokens": telemetry.input_tokens,
            "output_tokens": telemetry.output_tokens,
            "duration_ms": telemetry.duration_ms,
        },
    )
    evidence = SubscriptionPlanApprovalEvidence(
        task_version=1,
        plan_attempt=1,
        task_digest=external_digest,
        plan_digest=plan_digest,
        repository=policy.github_repository,
        base_ref="refs/heads/main",
        base_sha="a" * 40,
        policy_version=1,
        required_checks={name: "planned" for name in plan.required_checks},
        runner_mode=policy.runner_mode,
        local_remediation_limit=policy.local_remediation_limit,
        token_budget=policy.planner_model.max_input_tokens + policy.planner_model.max_output_tokens,
        cost_budget_minor=policy.planner_model.max_cost_minor,
        duration_budget_seconds=policy.planner_model.max_duration_seconds,
        producer=producer,
        result_digest=result_digest,
    )
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    inspector = SimpleNamespace(
        inspect=lambda **kwargs: RepositoryInspection(
            canonical_path=str(tmp_path),
            github_repository=policy.github_repository,
            default_branch="main",
            base_ref="refs/heads/main",
            base_sha="a" * 40,
        )
    )
    validator = PlanEvidenceValidator(store, inspector, data_root=str(tmp_path / "state"))
    return factory, store, evidence, plan, validator


@pytest.mark.integration
async def test_settled_subscription_plan_reaches_real_human_gate(session_factory, tmp_path):
    factory, store, evidence, plan, validator = await proposal_case(session_factory, tmp_path)
    service = SubscriptionPlanGateService(store, factory)
    outcome = await service.request(evidence, plan)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert run.pending_evidence_digest == outcome.evidence_digest
        assert await validator.validate(work, run.id) == evidence
        await work.rollback()
    assert (await service.request(evidence, plan)).replayed


async def approve_proposal(factory, session_factory, evidence, validator, outcome):
    from datetime import UTC, datetime, timedelta

    from forge.application.handlers.approvals import ApprovePlanHandler
    from forge.application.services.approvals import (
        ApprovalAuthorizationService,
        ApprovalChallengeService,
    )
    from forge.application.services.auth import AuthenticatedActor

    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    expiry = datetime.now(UTC) + timedelta(hours=1)
    async with factory() as work:
        await work.auth.create_session(
            session_id=actor.session_id,
            token_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
            csrf_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
            actor_id=actor.actor_id,
            expires_at=expiry,
            idle_expires_at=expiry,
            absolute_expires_at=expiry,
        )
        await work.commit()
    challenge = await ApprovalChallengeService(factory).issue(
        actor=actor,
        run_id=evidence.producer.run_id,
        gate="plan",
        run_version=outcome.run_version,
        evidence_digest=outcome.evidence_digest,
    )
    approval = await ApprovalAuthorizationService(
        factory, plan_evidence_validator=validator
    ).authorize(
        actor=actor,
        run_id=evidence.producer.run_id,
        gate="plan",
        run_version=outcome.run_version,
        evidence_digest=outcome.evidence_digest,
        challenge_token=challenge.token,
    )
    from forge.persistence.repositories.commands import PostgresCommandRepository

    command = await PostgresCommandRepository(session_factory).claim_next(
        worker_id="approval-worker", lease_seconds=30
    )
    assert command is not None and command.payload["approval_id"] == str(approval.id)
    async with factory() as work:
        await ApprovePlanHandler(validator)(command, work)
    return approval


@pytest.mark.integration
async def test_subscription_plan_requires_real_operator_approval_before_loading(
    session_factory, tmp_path
):
    from forge.application.services.approved_plan import ApprovedPlanError, ApprovedPlanLoader

    factory, store, evidence, plan, validator = await proposal_case(session_factory, tmp_path)
    outcome = await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    loader = ApprovedPlanLoader(store)
    with pytest.raises(ApprovedPlanError):
        async with factory() as work:
            await loader.load(work, evidence.producer.run_id)
    approval = await approve_proposal(factory, session_factory, evidence, validator, outcome)
    async with factory() as work:
        loaded = await loader.load(work, evidence.producer.run_id)
        assert loaded.plan == plan and loaded.evidence == evidence
        assert loaded.approval_id == approval.id
        assert loaded.run.state is RunState.PREPARING_WORKTREE
        assert await work.executions.get_outcome(loaded.run.id, "plan", 1) is None
        await work.rollback()


@pytest.mark.integration
@pytest.mark.parametrize(
    "field,value",
    [
        ("token_budget", 999999999),
        ("task_digest", "f" * 64),
        ("repository", "foreign/repository"),
        ("required_checks", {"invented": "planned"}),
        ("local_remediation_limit", 20),
    ],
)
async def test_proposal_rejects_forged_legacy_approval_fields(
    session_factory, tmp_path, field, value
):
    from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError

    factory, store, evidence, plan, _ = await proposal_case(session_factory, tmp_path)
    forged = evidence.model_copy(update={field: value})
    with pytest.raises(SubscriptionPlanGateError):
        await SubscriptionPlanGateService(store, factory).request(forged, plan)
    async with factory() as work:
        assert (await work.runs.get(evidence.producer.run_id)).state is RunState.PLANNING
        assert await work.subscription_plan_gate.get(evidence.producer.attempt_id) is None
        await work.rollback()


@pytest.mark.integration
async def test_proposal_rejects_forged_producer_telemetry(session_factory, tmp_path):
    from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError

    factory, store, evidence, plan, _ = await proposal_case(session_factory, tmp_path)
    producer = evidence.producer.model_copy(
        update={"telemetry": {"input_tokens": 999999, "output_tokens": 0, "duration_ms": 1}}
    )
    with pytest.raises(SubscriptionPlanGateError):
        await SubscriptionPlanGateService(store, factory).request(
            evidence.model_copy(update={"producer": producer}), plan
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "task_pause",
        "task_cancel",
        "task_version",
        "epoch",
        "task_payload",
        "attempt_route",
        "result_payload",
    ],
)
async def test_proposal_rejects_changed_durable_source(session_factory, tmp_path, mutation):
    from copy import deepcopy

    from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun
    from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask

    factory, store, evidence, plan, _ = await proposal_case(session_factory, tmp_path)
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        attempt = await work.session.get(SubscriptionAttempt, evidence.producer.attempt_id)
        if mutation == "task_pause":
            task.pause_requested = True
        elif mutation == "task_cancel":
            task.cancel_requested = True
        elif mutation == "task_version":
            task.version += 1
        elif mutation == "epoch":
            scheduler = await work.session.get(SubscriptionSchedulerRun, evidence.producer.run_id)
            scheduler.candidate_epoch += 1
        elif mutation == "task_payload":
            value = deepcopy(task.payload)
            value["extra"] = True
            task.payload = value
        elif mutation == "attempt_route":
            attempt.route_payload = {"forged": True}
        elif mutation == "result_payload":
            result = await work.session.get(SubscriptionAttemptResult, evidence.producer.attempt_id)
            value = deepcopy(result.result_payload)
            value["decision"]["value"]["summary"] = "Replaced decision"
            result.result_payload = value
        await work.commit()
    with pytest.raises(SubscriptionPlanGateError):
        await SubscriptionPlanGateService(store, factory).request(evidence, plan)


@pytest.mark.integration
async def test_approved_plan_keeps_historical_task_context_after_continuation(
    session_factory, tmp_path
):
    from forge.application.services.approved_plan import ApprovedPlanLoader
    from forge.persistence.models.subscription import SubscriptionTask

    factory, store, evidence, plan, validator = await proposal_case(session_factory, tmp_path)
    outcome = await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    await approve_proposal(factory, session_factory, evidence, validator, outcome)
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        task.version += 1
        task.state = "queued"
        await work.commit()
    async with factory() as work:
        assert (await ApprovedPlanLoader(store).load(work, evidence.producer.run_id)).plan == plan
        await work.rollback()


@pytest.mark.integration
async def test_proposal_replay_revalidates_current_controls(session_factory, tmp_path):
    from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError
    from forge.persistence.models.subscription import SubscriptionTask

    factory, store, evidence, plan, _ = await proposal_case(session_factory, tmp_path)
    service = SubscriptionPlanGateService(store, factory)
    await service.request(evidence, plan)
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        task.pause_requested = True
        await work.commit()
    with pytest.raises(SubscriptionPlanGateError):
        await service.request(evidence, plan)


@pytest.mark.integration
async def test_proposal_rollback_retries_without_partial_gate(
    session_factory, tmp_path, monkeypatch
):
    from forge.persistence.repositories.runs import PostgresRunRepository

    factory, store, evidence, plan, _ = await proposal_case(session_factory, tmp_path)
    service = SubscriptionPlanGateService(store, factory)
    original = PostgresRunRepository.await_approval

    async def fail_after_transition(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected precommit failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresRunRepository, "await_approval", fail_after_transition)
        with pytest.raises(RuntimeError, match="precommit"):
            await service.request(evidence, plan)
    async with factory() as work:
        assert await work.subscription_plan_gate.get(evidence.producer.attempt_id) is None
        assert (await work.runs.get(evidence.producer.run_id)).state is RunState.PLANNING
        assert not await work.artifacts.get_by_producer(
            run_id=evidence.producer.run_id,
            producer_type="subscription_plan",
            producer_id=evidence.producer.attempt_id,
        )
        await work.rollback()
    assert not (await service.request(evidence, plan)).replayed
    assert (await service.request(evidence, plan)).replayed


@pytest.mark.integration
async def test_proposal_storage_io_has_no_uow_and_stop_wins_before_publication(
    session_factory, tmp_path, monkeypatch
):
    from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError
    from forge.persistence.models.subscription import SubscriptionTask

    factory, store, evidence, plan, _ = await proposal_case(session_factory, tmp_path)
    active = 0

    class TrackedWork(PostgresUnitOfWork):
        async def __aenter__(self):
            nonlocal active
            result = await super().__aenter__()
            active += 1
            return result

        async def __aexit__(self, *args):
            nonlocal active
            try:
                return await super().__aexit__(*args)
            finally:
                active -= 1

    methods = {name: getattr(store, name) for name in ("put_bytes", "verify", "open_bytes")}
    seen = []
    stopped = False

    def guard(name):
        async def invoke(*args, **kwargs):
            nonlocal stopped
            assert active == 0
            seen.append(name)
            value = await methods[name](*args, **kwargs)
            if name == "open_bytes" and not stopped:
                stopped = True
                async with factory() as work:
                    task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
                    task.cancel_requested = True
                    await work.commit()
            return value

        return invoke

    for name in methods:
        monkeypatch.setattr(store, name, guard(name))
    with pytest.raises(SubscriptionPlanGateError):
        await SubscriptionPlanGateService(store, lambda: TrackedWork(session_factory)).request(
            evidence, plan
        )
    assert set(seen) == set(methods) and active == 0
    async with factory() as work:
        assert await work.subscription_plan_gate.get(evidence.producer.attempt_id) is None
        await work.rollback()


@pytest.mark.integration
async def test_populated_plan_gate_refuses_migration_downgrade(session_factory, tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    factory, store, evidence, plan, _ = await proposal_case(session_factory, tmp_path)
    await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260910_0015_subscription_plan_gate.py"
    )
    spec = importlib.util.spec_from_file_location("subscription_plan_gate_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    async with session_factory() as session, session.begin():
        connection = await session.connection()
        with pytest.raises(RuntimeError, match="evidence is durable"):
            await connection.run_sync(downgrade)
    async with factory() as work:
        retained = await work.subscription_plan_gate.verify(evidence)
        assert retained.evidence_digest == canonical_digest(evidence.model_dump(mode="json"))
        await work.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("published", [False, True])
async def test_run_pause_prevents_direct_gate_authority(session_factory, tmp_path, published):
    from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError

    factory, store, evidence, plan, _ = await proposal_case(session_factory, tmp_path)
    if published:
        await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        await work.commit()
    async with factory() as work:
        with pytest.raises(SubscriptionPlanGateError):
            if published:
                await work.subscription_plan_gate.verify(evidence)
            else:
                await work.subscription_plan_gate.record(evidence)
        await work.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("artifact", ["plan", "evidence"])
async def test_pending_plan_rejects_changed_artifact_bytes(
    session_factory, tmp_path, monkeypatch, artifact
):
    from forge.application.services.plan_evidence import PlanEvidenceValidationError

    factory, store, evidence, plan, validator = await proposal_case(session_factory, tmp_path)
    outcome = await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    digest = evidence.plan_digest if artifact == "plan" else outcome.evidence_digest
    original = store.open_bytes

    async def changed_bytes(requested_digest, **kwargs):
        value = await original(requested_digest, **kwargs)
        return value + b" " if requested_digest == digest else value

    monkeypatch.setattr(store, "open_bytes", changed_bytes)
    async with factory() as work:
        with pytest.raises(PlanEvidenceValidationError):
            await validator.validate(work, evidence.producer.run_id)
        await work.rollback()


@pytest.mark.integration
async def test_historical_approved_plan_requires_retained_launch_proof(session_factory, tmp_path):
    from forge.application.services.approved_plan import ApprovedPlanError, ApprovedPlanLoader
    from forge.persistence.models.subscription import SubscriptionClientLaunch
    from sqlalchemy import select

    factory, store, evidence, plan, validator = await proposal_case(session_factory, tmp_path)
    outcome = await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    await approve_proposal(factory, session_factory, evidence, validator, outcome)
    async with factory() as work:
        row = await work.session.scalar(
            select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == evidence.producer.attempt_id
            )
        )
        row.terminal_payload = {"stop_confirmed": True}
        await work.commit()
    async with factory() as work:
        with pytest.raises(ApprovedPlanError):
            await ApprovedPlanLoader(store).load(work, evidence.producer.run_id)


@pytest.mark.integration
async def test_settled_plan_can_resume_publication_by_attempt_identity(session_factory, tmp_path):
    factory, store, evidence, _plan, validator = await proposal_case(session_factory, tmp_path)
    service = SubscriptionPlanGateService(store, factory)
    outcome = await service.request_settled(evidence.producer.attempt_id)
    assert outcome.evidence_digest == canonical_digest(evidence.model_dump(mode="json"))
    async with factory() as work:
        assert await validator.validate(work, evidence.producer.run_id) == evidence
        await work.rollback()
    assert (await service.request_settled(evidence.producer.attempt_id)).replayed


@pytest.mark.integration
async def test_plan_gate_atomically_applies_result_and_releases_execution_slot(
    session_factory, tmp_path
):
    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
    from sqlalchemy import select

    factory, store, evidence, _plan, _ = await proposal_case(session_factory, tmp_path)
    service = SubscriptionPlanGateService(store, factory)
    await service.request_settled(evidence.producer.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, evidence.producer.attempt_id)
        attempt = await work.session.get(SubscriptionAttempt, evidence.producer.attempt_id)
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        scheduled = await work.session.scalar(
            select(SubscriptionScheduledTask).where(
                SubscriptionScheduledTask.task_id == evidence.producer.task_id
            )
        )
        assert result.disposition == "plan_approval" and result.accepted
        assert attempt.status == "terminal"
        assert task.state == scheduled.state == "blocked"
        assert scheduled.lease_owner is None and scheduled.lease_expires_at is None
        assert task.version == attempt.task_version + 2
        await work.rollback()
    assert (await service.request_settled(evidence.producer.attempt_id)).replayed


@pytest.mark.integration
async def test_plan_application_failure_rolls_back_gate_result_and_capacity(
    session_factory, tmp_path, monkeypatch
):
    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
    from forge.persistence.repositories.subscription_plan_gate import (
        PostgresSubscriptionPlanGateRepository,
    )
    from sqlalchemy import select

    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    original = PostgresSubscriptionPlanGateRepository.mark_applied

    async def fail_after_application(self, value):
        await original(self, value)
        raise RuntimeError("injected application commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            PostgresSubscriptionPlanGateRepository, "mark_applied", fail_after_application
        )
        with pytest.raises(RuntimeError, match="application commit failure"):
            await SubscriptionPlanGateService(store, factory).request_settled(
                evidence.producer.attempt_id
            )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, evidence.producer.attempt_id)
        attempt = await work.session.get(SubscriptionAttempt, evidence.producer.attempt_id)
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        scheduled = await work.session.scalar(
            select(SubscriptionScheduledTask).where(
                SubscriptionScheduledTask.task_id == evidence.producer.task_id
            )
        )
        assert result.disposition == "decision_pending" and not result.accepted
        assert attempt.status == task.state == scheduled.state == "reconciling"
        assert scheduled.lease_owner == attempt.lease_owner
        assert task.version == attempt.task_version + 1
        assert (await work.runs.get(evidence.producer.run_id)).state is RunState.PLANNING
        assert await work.subscription_plan_gate.get(evidence.producer.attempt_id) is None
        await work.rollback()
    assert not (
        await SubscriptionPlanGateService(store, factory).request_settled(
            evidence.producer.attempt_id
        )
    ).replayed


@pytest.mark.integration
async def test_settled_plan_rechecks_stop_after_source_read(session_factory, tmp_path, monkeypatch):
    from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError
    from forge.persistence.models.subscription import SubscriptionTask

    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    service = SubscriptionPlanGateService(store, factory)
    original = service.request

    async def stop_then_publish(value, plan):
        async with factory() as work:
            task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
            task.cancel_requested = True
            await work.commit()
        return await original(value, plan)

    monkeypatch.setattr(service, "request", stop_then_publish)
    with pytest.raises(SubscriptionPlanGateError):
        await service.request_settled(evidence.producer.attempt_id)
    async with factory() as work:
        assert await work.subscription_plan_gate.get(evidence.producer.attempt_id) is None
        result = await work.session.get(SubscriptionAttemptResult, evidence.producer.attempt_id)
        assert result.disposition == "decision_pending" and not result.accepted
        await work.rollback()


@pytest.mark.integration
async def test_concurrent_settled_plan_publication_applies_once(session_factory, tmp_path):
    import asyncio

    from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask

    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    service = SubscriptionPlanGateService(store, factory)
    outcomes = await asyncio.gather(
        *(service.request_settled(evidence.producer.attempt_id) for _ in range(2))
    )
    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    assert len({outcome.evidence_digest for outcome in outcomes}) == 1
    async with factory() as work:
        attempt = await work.session.get(SubscriptionAttempt, evidence.producer.attempt_id)
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        assert task.version == attempt.task_version + 2
        assert (
            await work.subscription_budget.usage(evidence.producer.run_id)
        ).consumed.provider_attempts == 1
        await work.rollback()


@pytest.mark.integration
async def test_settled_plan_external_task_is_append_only(session_factory, tmp_path):
    from sqlalchemy.exc import DBAPIError

    factory, store, evidence, _, validator = await proposal_case(session_factory, tmp_path)
    with pytest.raises(DBAPIError, match="tasks are append-only"):
        async with factory() as work:
            run = await work.runs.get(evidence.producer.run_id)
            task = await work.session.get(Task, run.task_id)
            task.title = "Different requested work"
            task.normalized_text = task.title
            task.task_digest = compute_task_digest(
                title=task.title,
                body=task.body,
                source_url=task.source_url,
                source_updated_at=task.source_updated_at,
                external_source=task.external_source,
                external_id=task.external_id,
            )
            await work.commit()
    await SubscriptionPlanGateService(store, factory).request_settled(evidence.producer.attempt_id)
    async with factory() as work:
        assert await validator.validate(work, evidence.producer.run_id) == evidence
        await work.rollback()


@pytest.mark.integration
async def test_result_replay_after_plan_approval_keeps_applied_receipt(
    session_factory, tmp_path, monkeypatch
):
    original = SubscriptionDecisionExecutor.settle
    captured = []

    async def capture(self, admission, result):
        captured.append((self, admission, result))
        return await original(self, admission, result)

    with monkeypatch.context() as patch:
        patch.setattr(SubscriptionDecisionExecutor, "settle", capture)
        factory, store, evidence, _, validator = await proposal_case(session_factory, tmp_path)
    outcome = await SubscriptionPlanGateService(store, factory).request_settled(
        evidence.producer.attempt_id
    )
    await approve_proposal(factory, session_factory, evidence, validator, outcome)
    executor, admission, result = captured[0]
    replay = await executor.settle(admission, result)
    assert replay.replayed and replay.accepted and replay.disposition == "plan_approval"
    async with factory() as work:
        assert (await work.runs.get(evidence.producer.run_id)).state is RunState.PREPARING_WORKTREE
        assert (
            await work.subscription_budget.usage(evidence.producer.run_id)
        ).consumed.provider_attempts == 1
        await work.rollback()
