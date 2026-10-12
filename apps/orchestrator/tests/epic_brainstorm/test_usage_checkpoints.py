"""Focused authoring-specific usage checkpoint tests for brainstorming and decomposition."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from forge.agents.client_process import ClientProcessReceipt, ClientProcessResult
from forge.api.routes.epic_brainstorm import _error as brainstorm_route_error
from forge.application.ports.epic_brainstorm import (
    AuthoringGatewayResult,
    BrainstormGatewayResult,
    BriefInput,
    ObservableBrainstormGateway,
)
from forge.application.ports.task_usage import (
    TaskUsageObserver,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.application.services.task_usage_baselines import TaskUsageBaselineService
from forge.application.services.task_usage_composition import (
    TaskUsageComposition,
)
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    BrainstormProposal,
    BrainstormTurn,
    FrozenBriefContent,
    FrozenRequirement,
)
from forge.domain.subscription import (
    AttemptTelemetry,
    RouteBinding,
    RouteSpec,
    TaskBudget,
)
from forge.domain.task_usage_contract import (
    CheckpointRecord,
    CheckpointState,
    CounterKind,
    MonitoringMode,
    MonitoringPolicy,
    Observation,
    OwnerCommand,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    TokenCounts,
    WorkUnitBinding,
)
from forge.persistence.models.epic_brainstorm import BrainstormAttemptRow, BrainstormJobRow
from forge.persistence.models.project import Project
from forge.persistence.models.subscription_quota import SubscriptionQuotaPool
from forge.persistence.models.task_usage import (
    TaskUsageBaseline,
    TaskUsageObservation,
    TaskUsageWorkUnit,
)
from forge.persistence.queries.task_usage_baselines import TaskUsageBaselineQuery
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from forge.persistence.repositories.epic_decomposition import PostgresEpicDecompositionUnitOfWork
from forge.persistence.repositories.task_usage_admissions import (
    PostgresTaskUsageAdmissionRepository,
)
from forge.persistence.repositories.task_usage_checkpoints import (
    CheckpointConflict,
    PostgresTaskUsageCheckpointRepository,
)
from forge.persistence.repositories.task_usage_observations import PostgresTaskUsageObservations
from forge.persistence.repositories.task_usage_policy import (
    MonitoringPolicyConflict,
    PostgresTaskUsagePolicyRepository,
)
from forge.worker.epic_brainstorm import EpicBrainstormWorker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class BriefFixture:
    def __init__(self, epic_id: UUID, project_id: UUID) -> None:
        self.current = BriefInput(
            epic_id=epic_id,
            project_id=project_id,
            epic_version=1,
            draft_digest="a" * 64,
            accepted_revision_id=None,
            accepted_digest=None,
        )

    async def input(self, epic_id: UUID, *, for_update: bool = False) -> BriefInput:
        assert epic_id == self.current.epic_id
        return self.current

    async def save_proposal_revision(
        self, epic_id: UUID, *, expected_version: int, source_job_id: UUID, proposal: Any
    ) -> UUID:
        return uuid4()


def test_checkpoint_conflict_is_http_conflict() -> None:
    assert brainstorm_route_error(CheckpointConflict("stale checkpoint")).status_code == 409


class ObservableGatewayStub(ObservableBrainstormGateway):
    def __init__(
        self,
        *,
        proposal: BrainstormProposal | None = None,
        telemetry: AttemptTelemetry | None = None,
        tokens_to_emit: int = 100,
        final_only: bool = False,
        pause_event: asyncio.Event | None = None,
        resume_event: asyncio.Event | None = None,
        cooperative_failure: str = "interrupted",
    ) -> None:
        self._proposal = proposal
        self._telemetry = telemetry
        self._tokens_to_emit = tokens_to_emit
        self._final_only = final_only
        self._pause_event = pause_event
        self._resume_event = resume_event
        self._cooperative_failure = cooperative_failure
        self.observed_called = False

    async def execute(self, job: Any, turns: Any, reader: Any, *, cancelled: Any, lifecycle: Any) -> AuthoringGatewayResult:
        raise NotImplementedError

    async def execute_observed(
        self,
        job: AuthoringJobSnapshot,
        turns: tuple[BrainstormTurn, ...],
        reader: Any,
        *,
        cancelled: Any,
        lifecycle: Any,
        usage_binding: WorkUnitBinding,
        usage_sink: TaskUsageObserver,
    ) -> AuthoringGatewayResult:
        self.observed_called = True
        launch_id = f"launch-{job.job_id}"
        receipt = ClientProcessReceipt(
            launch_id=launch_id, pid=12345, process_start_token="token-1",
            launched_monotonic=0.0,
        )
        finished = ClientProcessResult(
            receipt=receipt, return_code=0, frames=(), stdout_byte_count=0,
            stderr="", stderr_byte_count=0, stdout_truncated=False,
            stderr_truncated=False, outcome="completed", stop_confirmed=True,
        )
        await lifecycle.launch_intent(launch_id)
        await lifecycle.started(receipt)

        obs = Observation(
            binding=usage_binding,
            event_id=f"event-{usage_binding.work_unit_id}-1",
            sequence=1,
            counter_kind=CounterKind.CUMULATIVE,
            tokens=TokenCounts(input_tokens=self._tokens_to_emit // 2, output_tokens=self._tokens_to_emit // 2),
            final=self._final_only,
            final_status="completed" if self._final_only else None,
        )
        await usage_sink.observe_usage(obs)

        if self._pause_event is not None:
            self._pause_event.set()
        if self._resume_event is not None:
            try:
                await self._resume_event.wait()
            except asyncio.CancelledError:
                await lifecycle.finished(receipt, finished)
                return BrainstormGatewayResult(
                    proposal=None,
                    telemetry=AttemptTelemetry(
                        input_tokens=self._tokens_to_emit // 2,
                        output_tokens=self._tokens_to_emit // 2,
                        duration_ms=50,
                    ),
                    failure=self._cooperative_failure,
                )

        if await cancelled():
            await lifecycle.finished(receipt, finished)
            return BrainstormGatewayResult(
                proposal=self._proposal,
                telemetry=self._telemetry or AttemptTelemetry(input_tokens=self._tokens_to_emit // 2, output_tokens=self._tokens_to_emit // 2, duration_ms=50),
                failure=self._cooperative_failure,
            )

        await lifecycle.finished(receipt, finished)
        return BrainstormGatewayResult(
            proposal=self._proposal,
            telemetry=self._telemetry or AttemptTelemetry(input_tokens=self._tokens_to_emit // 2, output_tokens=self._tokens_to_emit // 2, duration_ms=50),
        )


async def _setup_project(factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID, AuthenticatedActor]:
    project_id, epic_id = uuid4(), uuid4()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    async with factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository=f"owner/{project_id}",
                default_branch="main",
            )
        )
        policy_repo = PostgresTaskUsagePolicyRepository(session)
        await policy_repo.append_policy_revision(
            MonitoringPolicy(
                project_id=project_id,
                revision=1,
                mode=MonitoringMode.CHECKPOINT,
                warning_multiplier=1.2,
                checkpoint_multiplier=1.5,
                history_limit=50,
                minimum_comparables=2,
                manual_estimate_tokens=100,
            ),
            expected_revision=0,
            actor_id=actor.actor_id,
        )
    return project_id, epic_id, actor


@pytest.mark.asyncio
async def test_brainstorm_submit_admits_subject_and_freezes_policy(brainstorm_session_factory):
    project_id, epic_id, actor = await _setup_project(brainstorm_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _: BriefFixture(epic_id, project_id),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    cid, cver = await service.create(epic_id=epic_id, project_id=project_id, actor=actor, key="create-1", text="Start")
    turns = await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=cid)
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=cid,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=cver,
        actor=actor,
        key="submit-1",
    )
    assert receipt.state == "queued"

    async with brainstorm_session_factory() as session:
        admissions = PostgresTaskUsageAdmissionRepository(session)
        admission = await admissions.get(SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id))
        assert admission is not None
        assert admission.policy_revision == 1
        assert len(admission.configured_route.members) == 1
        assert admission.configured_route.members[0].model == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_brainstorm_claim_holds_admission_when_checkpoint_active(brainstorm_session_factory):
    project_id, epic_id, actor = await _setup_project(brainstorm_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _: BriefFixture(epic_id, project_id),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    cid, cver = await service.create(epic_id=epic_id, project_id=project_id, actor=actor, key="create-2", text="Start")
    turns = await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=cid)
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=cid,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=cver,
        actor=actor,
        key="submit-2",
    )

    async with brainstorm_session_factory() as session, session.begin():
        cp_repo = PostgresTaskUsageCheckpointRepository(session)
        adm_repo = PostgresTaskUsageAdmissionRepository(session)
        adm = await adm_repo.get(SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id))
        unit_id = uuid4()
        source_id = uuid4()
        binding = WorkUnitBinding(
            subject=adm.subject,
            work_unit_id=unit_id,
            admitted_phase_id="authoring_exchange",
            configured_route=adm.configured_route,
            effective_route=adm.configured_route,
            source=SourceBinding(kind="authoring_attempt", source_id=source_id),
        )
        session.add(
            BrainstormAttemptRow(
                id=source_id,
                job_id=receipt.job_id,
                number=1,
                state="settled",
                owner="fixture",
                fence=uuid4(),
                lease_expires_at=datetime.now(UTC),
                process_settled=True,
            )
        )
        session.add(
            TaskUsageWorkUnit(
                id=unit_id,
                project_id=project_id,
                subject_kind="authoring_job",
                subject_id=receipt.job_id,
                authoring_job_id=receipt.job_id,
                admitted_phase_id="authoring_exchange",
                configured_route_digest=adm.configured_route.fingerprint,
                configured_route_members=[asdict(member) for member in adm.configured_route.members],
                effective_route_digest=adm.configured_route.fingerprint,
                effective_route_members=[asdict(member) for member in adm.configured_route.members],
                source_kind="authoring_attempt",
                source_id=binding.source.source_id,
                authoring_attempt_id=binding.source.source_id,
                schema_version=1,
            )
        )
        await session.flush()
        session.add(
            TaskUsageBaseline(
                work_unit_id=unit_id,
                project_id=project_id,
                subject_kind="authoring_job",
                subject_id=receipt.job_id,
                policy_revision=1,
                history_digest="d" * 64,
                snapshot_digest="a" * 64,
                estimator_version="median_p90_v1",
                window_limit=50,
                minimum_comparables=2,
                cohort_digest=binding.configured_route.fingerprint,
                frozen_at=datetime.now(UTC),
                schema_version=1,
                sample_count=0,
                status="manual",
                reference_tokens=100,
                warning_threshold_tokens=120,
                checkpoint_threshold_tokens=150,
                frozen_payload={},
            )
        )
        await session.flush()
        session.add(
            TaskUsageObservation(
                id=uuid4(), project_id=project_id, work_unit_id=unit_id,
                event_id="trigger-1", sequence=1, counter_kind="cumulative",
                input_tokens=0, output_tokens=0, final=False,
                observed_at=datetime.now(UTC),
            )
        )
        await session.flush()
        await cp_repo.request(
            CheckpointRecord(
                checkpoint_id=uuid4(),
                binding=binding,
                baseline_binding=binding,
                state=CheckpointState.REQUESTED,
                policy_revision=1,
                reference_digest="a" * 64,
                trigger_event_id="trigger-1",
            )
        )

    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("worker-1")
        assert claimed is None


@pytest.mark.asyncio
@pytest.mark.parametrize("final_only", [False, True])
async def test_worker_observes_usage_pauses_and_preserves_racing_proposal(brainstorm_session_factory, final_only):
    project_id, epic_id, actor = await _setup_project(brainstorm_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _: BriefFixture(epic_id, project_id),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    cid, cver = await service.create(epic_id=epic_id, project_id=project_id, actor=actor, key="create-3", text="Start")
    turns = await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=cid)
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=cid,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=cver,
        actor=actor,
        key="submit-3",
    )

    proposal = BrainstormProposal(
        turn_id=uuid4(),
        problem="Preserved problem",
        requirements=("Requirement 1",),
    )
    pause_evt = asyncio.Event()
    resume_evt = asyncio.Event()

    gateway = ObservableGatewayStub(
        proposal=proposal,
        tokens_to_emit=1000,
        final_only=final_only,
        pause_event=pause_evt,
        resume_event=resume_evt,
    )

    composition = TaskUsageComposition(
        observations=PostgresTaskUsageObservations(brainstorm_session_factory),
        totals=TaskUsageTotalsQuery(brainstorm_session_factory),
        baselines=TaskUsageBaselineService(TaskUsageBaselineQuery(brainstorm_session_factory)),
    )

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="test-worker",
        gateway_factory=lambda _: gateway,
        reader_factory=lambda _: type("Reader", (), {})(),
        task_usage=composition,
    )

    worker_task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(pause_evt.wait(), timeout=5)
    except TimeoutError:
        if worker_task.done():
            async with brainstorm_session_factory() as session:
                stalled = await session.get(BrainstormJobRow, receipt.job_id)
                pytest.fail(
                    f"worker returned before observed gateway: {stalled.state}/{stalled.failure}"
                )
        raise

    # While gateway is running, trigger checkpoint request
    async with brainstorm_session_factory() as session, session.begin():
        checkpoints = PostgresTaskUsageCheckpointRepository(session)
        latest_cp = await checkpoints.active_for_subject(SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id))
        assert latest_cp is not None
        assert latest_cp.state in {CheckpointState.REQUESTED, CheckpointState.PAUSING}

    resume_evt.set()
    await worker_task

    # Check job state: racing proposal must be preserved as "proposed"
    async with brainstorm_session_factory() as session:
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job.state == "proposed"
        retained = BrainstormProposal.model_validate(job.proposal)
        assert retained.problem == proposal.problem
        assert job.proposal_digest == retained.digest
        checkpoints = PostgresTaskUsageCheckpointRepository(session)
        cp = await checkpoints.latest_for_subject(SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id))
        assert cp is not None
        assert cp.process_settled
        assert cp.state is CheckpointState.RESOLVED


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("brainstorm", "decomposition"))
@pytest.mark.parametrize(
    ("failure", "expired", "expected_state", "expected_failure", "checkpoint_state"),
    (
        ("timeout", False, "failed", "timeout", CheckpointState.RESOLVED),
        ("interrupted", True, "failed", "timeout", CheckpointState.RESOLVED),
        ("cancelled", False, "cancelled", "cancelled", CheckpointState.RESOLVED),
        ("quota_exhausted", False, "quota_wait", "quota_exhausted", CheckpointState.RESOLVED),
        ("interrupted", False, "paused", "lost_result", CheckpointState.PAUSED),
    ),
)
async def test_settled_recovery_keeps_independent_terminal_causes(
    brainstorm_session_factory, kind, failure, expired, expected_state, expected_failure,
    checkpoint_state,
) -> None:
    project_id, epic_id, actor = await _setup_project(brainstorm_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    brief = BriefFixture(epic_id, project_id)
    if kind == "decomposition":
        brief.current = replace(
            brief.current,
            accepted_revision_id=uuid4(), accepted_digest="b" * 64,
            accepted_content=FrozenBriefContent(
                problem="Accepted problem",
                requirements=(FrozenRequirement(requirement_id=uuid4(), text="Requirement"),),
            ),
        )
    authoring_service = EpicBrainstormService(
        brainstorm_session_factory, lambda _: brief,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    service = (
        EpicDecompositionService(
            lambda: PostgresEpicDecompositionUnitOfWork(brainstorm_session_factory),
            authoring_service=authoring_service,
        )
        if kind == "decomposition"
        else authoring_service
    )
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="recovery-create", text="Start",
    )
    turns = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
    )
    receipt = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=turns[0].turn_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="recovery-submit",
    )
    subject = SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id)
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim(
            "recovery-worker", kinds=frozenset((kind,))
        )
        assert claimed is not None
        job, attempt = claimed
        binding = WorkUnitBinding(
            subject=subject, work_unit_id=uuid4(), admitted_phase_id="authoring_exchange",
            configured_route=RouteCohort.from_members((RouteMember(
                role="authoring", provider="google", model="gemini-2.5-pro", ordinal=0,
            ),)),
            effective_route=RouteCohort.from_members((RouteMember(
                role="authoring", provider="google", model="gemini-2.5-pro", ordinal=0,
            ),)),
            source=SourceBinding(kind="authoring_attempt", source_id=attempt.id),
        )
        session.add(TaskUsageWorkUnit(
            id=binding.work_unit_id, project_id=project_id,
            subject_kind="authoring_job", subject_id=receipt.job_id,
            authoring_job_id=receipt.job_id, admitted_phase_id="authoring_exchange",
            configured_route_digest=binding.configured_route.fingerprint,
            configured_route_members=[asdict(member) for member in binding.configured_route.members],
            effective_route_digest=binding.effective_route.fingerprint,
            effective_route_members=[asdict(member) for member in binding.effective_route.members],
            source_kind="authoring_attempt", source_id=attempt.id,
            authoring_attempt_id=attempt.id, schema_version=1,
        ))
        await session.flush()
        session.add(TaskUsageBaseline(
            work_unit_id=binding.work_unit_id, project_id=project_id,
            subject_kind="authoring_job", subject_id=receipt.job_id,
            policy_revision=1, history_digest="d" * 64, snapshot_digest="e" * 64,
            estimator_version="median_p90_v1", window_limit=50,
            minimum_comparables=2, cohort_digest=binding.configured_route.fingerprint,
            frozen_at=datetime.now(UTC), schema_version=1, sample_count=0,
            status="manual", reference_tokens=100, warning_threshold_tokens=120,
            checkpoint_threshold_tokens=150, frozen_payload={},
        ))
        await session.flush()
        session.add(TaskUsageObservation(
            id=uuid4(), project_id=project_id, work_unit_id=binding.work_unit_id,
            event_id="recovery-trigger", sequence=1, counter_kind="cumulative",
            input_tokens=200, output_tokens=0, final=False, observed_at=datetime.now(UTC),
        ))
        await session.flush()
        checkpoint = await PostgresTaskUsageCheckpointRepository(session).request(
            CheckpointRecord(
                checkpoint_id=uuid4(), binding=binding, baseline_binding=binding,
                state=CheckpointState.REQUESTED, policy_revision=1,
                reference_digest="e" * 64, trigger_event_id="recovery-trigger",
            )
        )
        job.state = "cancel_requested" if failure == "cancelled" else "reconciling"
        job.failure = failure
        attempt.process_settled = True
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if expired:
            attempt.created_at = datetime.now(UTC) - timedelta(days=1)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory, owner="recovery-worker",
        kinds=frozenset((kind,)),
        gateway_factory=lambda _: pytest.fail("recovery launched a gateway"),
        reader_factory=lambda _: type("Reader", (), {})(),
        task_usage=TaskUsageComposition(
            observations=PostgresTaskUsageObservations(brainstorm_session_factory),
            totals=TaskUsageTotalsQuery(brainstorm_session_factory),
            baselines=TaskUsageBaselineService(TaskUsageBaselineQuery(brainstorm_session_factory)),
        ),
    )
    assert await worker.reconcile_settled() == receipt.job_id
    async with brainstorm_session_factory() as session:
        job = await session.get(BrainstormJobRow, receipt.job_id)
        attempt = await session.get(BrainstormAttemptRow, job.current_attempt_id)
        settled = await PostgresTaskUsageCheckpointRepository(session).get(checkpoint.checkpoint_id)
        assert job.state == expected_state
        assert job.failure == expected_failure
        assert attempt.process_settled and attempt.state == "settled"
        assert settled.state is checkpoint_state and settled.process_settled
        if failure == "quota_exhausted":
            pool = await session.scalar(select(SubscriptionQuotaPool))
            assert pool is not None and pool.blocked
    if failure == "quota_exhausted":
        with pytest.raises(CheckpointConflict):
            await service.apply_checkpoint_command(
                epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
                actor=actor,
                command=OwnerCommand(
                    command_id=uuid4(), checkpoint_id=checkpoint.checkpoint_id,
                    expected_version=settled.version, idempotency_key="quota-cannot-continue",
                    action="continue", actor_id=actor.actor_id,
                    continue_mode="without_usage_pauses",
                ),
            )
        async with brainstorm_session_factory() as session:
            job = await session.get(BrainstormJobRow, receipt.job_id)
            pool = await session.scalar(select(SubscriptionQuotaPool))
            assert job is not None and job.state == "quota_wait"
            assert pool is not None and pool.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("brainstorm", "decomposition"))
@pytest.mark.parametrize("final_only", (False, True))
@pytest.mark.parametrize(
    ("terminal_failure", "expected_state", "expected_failure"),
    (
        (None, "failed", "unavailable"),
        ("timeout", "failed", "timeout"),
        ("quota_exhausted", "quota_wait", "quota_exhausted"),
    ),
)
async def test_worker_tick_replays_committed_authoring_crossing_after_sink_timeout(
    brainstorm_session_factory, monkeypatch, kind, final_only,
    terminal_failure, expected_state, expected_failure,
) -> None:
    project_id, epic_id, actor = await _setup_project(brainstorm_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    brief = BriefFixture(epic_id, project_id)
    if kind == "decomposition":
        brief.current = replace(
            brief.current,
            accepted_revision_id=uuid4(), accepted_digest="b" * 64,
            accepted_content=FrozenBriefContent(
                problem="Accepted problem",
                requirements=(FrozenRequirement(requirement_id=uuid4(), text="Requirement"),),
            ),
        )
    authoring_service = EpicBrainstormService(
        brainstorm_session_factory, lambda _: brief,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    service = (
        EpicDecompositionService(
            lambda: PostgresEpicDecompositionUnitOfWork(brainstorm_session_factory),
            authoring_service=authoring_service,
        )
        if kind == "decomposition"
        else authoring_service
    )
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="replay-create", text="Start",
    )
    turns = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
    )
    receipt = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=turns[0].turn_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="replay-submit",
    )

    gateway_errors: list[str] = []

    class CatchingGateway(ObservableGatewayStub):
        async def execute_observed(self, *args, usage_sink, **kwargs):
            class CatchSink:
                async def observe_usage(self, observation):
                    try:
                        await usage_sink.observe_usage(observation)
                    except TimeoutError:
                        pass

            try:
                result = await super().execute_observed(*args, usage_sink=CatchSink(), **kwargs)
                return replace(result, failure=terminal_failure)
            except Exception as error:
                gateway_errors.append(f"{type(error).__name__}: {error}")
                raise

    observations = PostgresTaskUsageObservations(brainstorm_session_factory)
    append = observations.append_observation

    async def committed_then_timed_out(observation):
        await append(observation)
        raise TimeoutError("checkpoint evaluation sink timed out after commit")

    monkeypatch.setattr(observations, "append_observation", committed_then_timed_out)
    racing_proposal = (
        BrainstormProposal(
            turn_id=uuid4(), problem="Retained after replay", requirements=("Requirement",),
        )
        if kind == "brainstorm" and final_only and terminal_failure is None
        else None
    )
    gateway = CatchingGateway(
        proposal=racing_proposal, tokens_to_emit=1000, final_only=final_only,
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory, owner="replay-worker",
        kinds=frozenset((kind,)),
        gateway_factory=lambda _: gateway,
        reader_factory=lambda _: type("Reader", (), {})(),
        task_usage=TaskUsageComposition(
            observations=observations,
            totals=TaskUsageTotalsQuery(brainstorm_session_factory),
            baselines=TaskUsageBaselineService(TaskUsageBaselineQuery(brainstorm_session_factory)),
        ),
    )
    assert await worker.run_once() == receipt.job_id
    assert gateway_errors == []
    assert gateway.observed_called
    subject = SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id)
    async with brainstorm_session_factory() as session:
        assert await PostgresTaskUsageCheckpointRepository(session).latest_for_subject(subject) is None
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job.state == ("proposed" if racing_proposal is not None else expected_state)
        assert job.failure == (None if racing_proposal is not None else expected_failure)
    assert await worker.run_once() is None
    async with brainstorm_session_factory() as session:
        checkpoint = await PostgresTaskUsageCheckpointRepository(session).latest_for_subject(subject)
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert checkpoint is not None and checkpoint.state is CheckpointState.RESOLVED
        assert checkpoint.process_settled
        assert job.state == ("proposed" if racing_proposal is not None else expected_state)
        if racing_proposal is not None:
            assert BrainstormProposal.model_validate(job.proposal).problem == racing_proposal.problem
        else:
            assert job.proposal is None
        if terminal_failure == "quota_exhausted":
            pool = await session.scalar(select(SubscriptionQuotaPool))
            assert pool is not None and pool.blocked
    assert await worker.run_once() is None
    async with brainstorm_session_factory() as session:
        assert len(await PostgresTaskUsageCheckpointRepository(session).list_for_subject(subject)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("brainstorm", "decomposition"))
async def test_live_poll_replays_committed_crossing_before_provider_settlement(
    brainstorm_session_factory, monkeypatch, kind,
) -> None:
    project_id, epic_id, actor = await _setup_project(brainstorm_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    brief = BriefFixture(epic_id, project_id)
    if kind == "decomposition":
        brief.current = replace(
            brief.current,
            accepted_revision_id=uuid4(), accepted_digest="b" * 64,
            accepted_content=FrozenBriefContent(
                problem="Accepted problem",
                requirements=(FrozenRequirement(requirement_id=uuid4(), text="Requirement"),),
            ),
        )
    authoring_service = EpicBrainstormService(
        brainstorm_session_factory, lambda _: brief,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    service = (
        EpicDecompositionService(
            lambda: PostgresEpicDecompositionUnitOfWork(brainstorm_session_factory),
            authoring_service=authoring_service,
        )
        if kind == "decomposition"
        else authoring_service
    )
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor,
        key="live-replay-create", text="Start",
    )
    turns = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
    )
    receipt = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=turns[0].turn_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="live-replay-submit",
    )
    observed, settle, cancellation_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HoldingGateway(ObservableGatewayStub):
        async def execute_observed(
            self, job, turns, reader, *, cancelled, lifecycle, usage_binding, usage_sink,
        ):
            launch_id = f"launch-{job.job_id}"
            receipt = ClientProcessReceipt(
                launch_id=launch_id, pid=12345, process_start_token="token-1",
                launched_monotonic=0.0,
            )
            finished = ClientProcessResult(
                receipt=receipt, return_code=0, frames=(), stdout_byte_count=0,
                stderr="", stderr_byte_count=0, stdout_truncated=False,
                stderr_truncated=False, outcome="completed", stop_confirmed=True,
            )
            await lifecycle.launch_intent(launch_id)
            await lifecycle.started(receipt)
            try:
                await usage_sink.observe_usage(Observation(
                    binding=usage_binding, event_id="live-crossing", sequence=1,
                    counter_kind=CounterKind.CUMULATIVE,
                    tokens=TokenCounts(input_tokens=500, output_tokens=500), final=False,
                ))
            except TimeoutError:
                pass
            observed.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await settle.wait()
            await lifecycle.finished(receipt, finished)
            return BrainstormGatewayResult(
                proposal=None,
                telemetry=AttemptTelemetry(input_tokens=500, output_tokens=500, duration_ms=50),
                failure="interrupted",
            )

    observations = PostgresTaskUsageObservations(brainstorm_session_factory)
    append = observations.append_observation

    async def committed_then_timed_out(observation):
        await append(observation)
        raise TimeoutError("checkpoint evaluation sink timed out after commit")

    monkeypatch.setattr(observations, "append_observation", committed_then_timed_out)
    worker = EpicBrainstormWorker(
        brainstorm_session_factory, owner="live-replay-worker",
        kinds=frozenset((kind,)),
        gateway_factory=lambda _: HoldingGateway(),
        reader_factory=lambda _: type("Reader", (), {})(),
        task_usage=TaskUsageComposition(
            observations=observations,
            totals=TaskUsageTotalsQuery(brainstorm_session_factory),
            baselines=TaskUsageBaselineService(TaskUsageBaselineQuery(brainstorm_session_factory)),
        ),
    )
    subject = SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(observed.wait(), timeout=5)
        active = None
        for _ in range(50):
            async with brainstorm_session_factory() as session:
                active = await PostgresTaskUsageCheckpointRepository(session).active_for_subject(
                    subject
                )
                job = await session.get(BrainstormJobRow, receipt.job_id)
                attempt = await session.get(BrainstormAttemptRow, job.current_attempt_id)
            if active is not None:
                break
            await asyncio.sleep(0.1)
        assert active is not None
        assert active.state in {CheckpointState.REQUESTED, CheckpointState.PAUSING}
        assert not active.process_settled
        assert job.state == "running" and not attempt.process_settled
        await asyncio.wait_for(cancellation_seen.wait(), timeout=5)
        async with brainstorm_session_factory() as session:
            waiting = await PostgresTaskUsageCheckpointRepository(session).get(
                active.checkpoint_id
            )
            assert waiting is not None and waiting.state is CheckpointState.PAUSING
            assert not waiting.process_settled
        settle.set()
        assert await asyncio.wait_for(task, timeout=5) == receipt.job_id
        async with brainstorm_session_factory() as session:
            job = await session.get(BrainstormJobRow, receipt.job_id)
            checkpoint = await PostgresTaskUsageCheckpointRepository(session).latest_for_subject(
                subject
            )
            assert job.state == "paused" and job.proposal is None
            assert checkpoint is not None and checkpoint.state is CheckpointState.PAUSED
            assert checkpoint.process_settled
    finally:
        settle.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("next_action", ["continue", "cancel", "stop", "cancel_during", "quota", "timeout"])
@pytest.mark.parametrize("cooperative_failure", ["interrupted", "cancelled"])
async def test_streaming_checkpoint_settles_before_owner_continuation(brainstorm_session_factory, next_action, cooperative_failure):
    project_id, epic_id, actor = await _setup_project(brainstorm_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _: BriefFixture(epic_id, project_id),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    cid, cver = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="pause-create", text="Start"
    )
    turns = await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=cid)
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=cid,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=cver,
        actor=actor,
        key="pause-submit",
    )
    observed = asyncio.Event()
    hold = asyncio.Event()
    gateway = ObservableGatewayStub(
        tokens_to_emit=1000, pause_event=observed, resume_event=hold,
        cooperative_failure=(
            "quota_exhausted" if next_action == "quota" else
            "timeout" if next_action == "timeout" else cooperative_failure
        ),
    )
    composition = TaskUsageComposition(
        observations=PostgresTaskUsageObservations(brainstorm_session_factory),
        totals=TaskUsageTotalsQuery(brainstorm_session_factory),
        baselines=TaskUsageBaselineService(TaskUsageBaselineQuery(brainstorm_session_factory)),
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="pause-worker",
        gateway_factory=lambda _: gateway,
        reader_factory=lambda _: type("Reader", (), {})(),
        task_usage=composition,
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(observed.wait(), timeout=5)
    subject = SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id)
    async with brainstorm_session_factory() as session:
        cp = await PostgresTaskUsageCheckpointRepository(session).active_for_subject(subject)
        assert cp is not None and cp.state in {CheckpointState.REQUESTED, CheckpointState.PAUSING}
        assert not cp.process_settled
        if next_action == "cancel_during":
            row = await session.get(BrainstormJobRow, receipt.job_id)
            version = row.version
    if next_action == "cancel_during":
        cancelling = await service.cancel(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            expected_job_version=version, actor=actor, key="cancel-during-checkpoint",
        )
        assert cancelling.state == "cancel_requested"
    hold.set()
    await asyncio.wait_for(task, timeout=5)
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormJobRow, receipt.job_id)
        attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
        cp = await PostgresTaskUsageCheckpointRepository(session).active_for_subject(subject)
        if next_action in {"cancel_during", "quota", "timeout"}:
            expected_state = {"cancel_during": "cancelled", "quota": "quota_wait", "timeout": "failed"}[next_action]
            assert row.state == expected_state and row.proposal is None
            assert attempt.process_settled and attempt.state == "settled"
            assert cp is None
            if next_action == "quota":
                assert row.failure == "quota_exhausted" and row.next_eligible_at is not None
            if next_action == "timeout":
                assert row.failure == "timeout"
            return
        assert row.state == "paused" and row.proposal is None
        assert attempt.process_settled and attempt.state == "settled"
        assert cp is not None and cp.state is CheckpointState.PAUSED and cp.process_settled
        assert row.retry_authorized_until is None
        paused_version = row.version

    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "paused" and outcome.failure is None
    if next_action == "cancel":
        cancelled_receipt = await service.cancel(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            expected_job_version=paused_version, actor=actor, key="pause-cancel",
        )
        assert cancelled_receipt.state == "cancelled"
        async with brainstorm_session_factory() as session:
            resolved = await PostgresTaskUsageCheckpointRepository(session).latest_for_subject(subject)
            assert resolved is not None and resolved.state is CheckpointState.RESOLVED
        return
    if next_action == "stop":
        stop_command = OwnerCommand(
            command_id=uuid4(), checkpoint_id=cp.checkpoint_id,
            expected_version=cp.version, idempotency_key="pause-stop",
            action="stop", actor_id=actor.actor_id,
        )
        stop_receipt, resolved = await service.apply_checkpoint_command(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            actor=actor, command=stop_command,
        )
        assert stop_receipt.state == CheckpointState.RESOLVED.value
        assert resolved.state is CheckpointState.RESOLVED
        async with brainstorm_session_factory() as session:
            stopped = await session.get(BrainstormJobRow, receipt.job_id)
            assert stopped.state == "cancelled"
        return

    cmd = OwnerCommand(
        command_id=uuid4(), checkpoint_id=cp.checkpoint_id,
        expected_version=cp.version, idempotency_key="pause-continue",
        action="continue", actor_id=actor.actor_id,
        continue_mode="without_usage_pauses",
    )
    _, resumed = await service.apply_checkpoint_command(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        actor=actor, command=cmd,
    )
    assert resumed.state is CheckpointState.RESUMED
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("next-worker")
        assert claimed is not None
        assert claimed[1].number == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_input", [False, True])
async def test_owner_continuation_commands_without_repair_debit(brainstorm_session_factory, stale_input):
    project_id, epic_id, actor = await _setup_project(brainstorm_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    brief_fixture = BriefFixture(epic_id, project_id)
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _: brief_fixture,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    cid, cver = await service.create(epic_id=epic_id, project_id=project_id, actor=actor, key="create-4", text="Start")
    turns = await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=cid)
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=cid,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=cver,
        actor=actor,
        key="submit-4",
    )

    # Claim and simulate paused checkpoint
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("worker-1")
        assert claimed is not None
        job_row, attempt_row = claimed
        unit_id = uuid4()
        binding = WorkUnitBinding(
            subject=SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id),
            work_unit_id=unit_id,
            admitted_phase_id="authoring_exchange",
            configured_route=RouteCohort.from_members((RouteMember(role="authoring", provider="google", model="gemini-2.5-pro", ordinal=0),)),
            effective_route=RouteCohort.from_members((RouteMember(role="authoring", provider="google", model="gemini-2.5-pro", ordinal=0),)),
            source=SourceBinding(kind="authoring_attempt", source_id=attempt_row.id),
        )
        session.add(
            TaskUsageWorkUnit(
                id=unit_id,
                project_id=project_id,
                subject_kind="authoring_job",
                subject_id=receipt.job_id,
                authoring_job_id=receipt.job_id,
                admitted_phase_id="authoring_exchange",
                configured_route_digest=binding.configured_route.fingerprint,
                configured_route_members=[asdict(member) for member in binding.configured_route.members],
                effective_route_digest=binding.effective_route.fingerprint,
                effective_route_members=[asdict(member) for member in binding.effective_route.members],
                source_kind="authoring_attempt",
                source_id=attempt_row.id,
                authoring_attempt_id=attempt_row.id,
                schema_version=1,
            )
        )
        await session.flush()
        session.add(
            TaskUsageBaseline(
                work_unit_id=unit_id,
                project_id=project_id,
                subject_kind="authoring_job",
                subject_id=receipt.job_id,
                policy_revision=1,
                history_digest="d" * 64,
                snapshot_digest="e" * 64,
                estimator_version="median_p90_v1",
                window_limit=50,
                minimum_comparables=2,
                cohort_digest=binding.configured_route.fingerprint,
                frozen_at=datetime.now(UTC),
                schema_version=1,
                sample_count=0,
                status="manual",
                reference_tokens=100,
                warning_threshold_tokens=120,
                checkpoint_threshold_tokens=150,
                frozen_payload={},
            )
        )
        await session.flush()
        session.add(
            TaskUsageObservation(
                id=uuid4(), project_id=project_id, work_unit_id=unit_id,
                event_id="trigger-1", sequence=1, counter_kind="cumulative",
                input_tokens=0, output_tokens=0, final=False,
                observed_at=datetime.now(UTC),
            )
        )
        await session.flush()
        checkpoints = PostgresTaskUsageCheckpointRepository(session)
        cp = await checkpoints.request(
            CheckpointRecord(
                checkpoint_id=uuid4(),
                binding=binding,
                baseline_binding=binding,
                state=CheckpointState.REQUESTED,
                policy_revision=1,
                reference_digest="e" * 64,
                trigger_event_id="trigger-1",
            )
        )
        pausing = await checkpoints.transition(cp.checkpoint_id, expected_version=0, state=CheckpointState.PAUSING, process_settled=False)
        paused = await checkpoints.transition(pausing.checkpoint_id, expected_version=1, state=CheckpointState.PAUSED, process_settled=True)
        job_row.state = "paused"
        attempt_row.state = "settled"
        attempt_row.process_settled = True

    await PostgresTaskUsageObservations(brainstorm_session_factory).append_observation(
        Observation(
            binding=binding,
            event_id="owner-pause-final-usage",
            sequence=2,
            counter_kind=CounterKind.CUMULATIVE,
            tokens=TokenCounts(input_tokens=55, output_tokens=45),
            final=True,
            final_status="completed",
            reconciles_event_id="trigger-1",
        )
    )

    # Test usage_checkpoint inspection
    cp_info = await service.usage_checkpoint(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert cp_info is not None
    assert cp_info.state == CheckpointState.PAUSED
    _, report = await service.usage_checkpoint_view(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
    )
    assert report is not None
    assert report.input_tokens == 55
    assert report.output_tokens == 45

    changed_policy = MonitoringPolicy(
        project_id=project_id, revision=2, mode=MonitoringMode.CHECKPOINT,
        warning_multiplier=1.2, checkpoint_multiplier=1.5,
        history_limit=50, minimum_comparables=2, manual_estimate_tokens=100,
    )
    policy_command = OwnerCommand(
        command_id=uuid4(), checkpoint_id=paused.checkpoint_id,
        expected_version=paused.version, idempotency_key="cmd-policy-1",
        action="set_policy", actor_id=actor.actor_id,
        requested_policy=changed_policy,
    )
    policy_receipt, paused = await service.apply_checkpoint_command(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        actor=actor, command=policy_command,
    )
    assert policy_receipt.state == CheckpointState.PAUSED.value
    assert paused.monitoring_policy_revision == 2
    assert paused.state is CheckpointState.PAUSED
    with pytest.raises(MonitoringPolicyConflict, match="stale"):
        await service.apply_checkpoint_command(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            actor=actor,
            command=replace(
                policy_command,
                command_id=uuid4(),
                expected_version=paused.version,
                idempotency_key="cmd-policy-stale",
            ),
        )

    # Apply continue command with next_checkpoint
    cmd = OwnerCommand(
        command_id=uuid4(),
        checkpoint_id=paused.checkpoint_id,
        expected_version=paused.version,
        idempotency_key="cmd-continue-1",
        action="continue",
        actor_id=actor.actor_id,
        continue_mode="next_checkpoint",
    )
    if stale_input:
        brief_fixture.current = replace(brief_fixture.current, draft_digest="b" * 64)
        with pytest.raises(CheckpointConflict, match="input changed"):
            await service.apply_checkpoint_command(
                epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
                actor=actor, command=cmd,
            )
        async with brainstorm_session_factory() as session:
            unchanged = await session.get(BrainstormJobRow, receipt.job_id)
            assert unchanged.state == "paused"
        return
    receipt_cmd, updated_cp = await service.apply_checkpoint_command(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id, actor=actor, command=cmd
    )
    assert receipt_cmd.action == "continue"
    assert updated_cp.state == CheckpointState.RESUMED
    assert updated_cp.next_checkpoint_threshold_tokens is not None
    assert updated_cp.next_checkpoint_threshold_tokens > 100
    replay_receipt, replay_checkpoint = await service.apply_checkpoint_command(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        actor=actor, command=cmd,
    )
    assert replay_receipt.replayed
    assert replay_checkpoint.version == updated_cp.version

    # Verify job is queued for continuation without repair debit
    async with brainstorm_session_factory() as session:
        job_row = await session.get(BrainstormJobRow, receipt.job_id)
        assert job_row.state == "queued"
        # Zero repair debit: retry_authorized_until is None, normal attempts apply
        assert job_row.retry_authorized_until is None
    async with brainstorm_session_factory() as session, session.begin():
        successor_attempt = uuid4()
        job = await session.get(BrainstormJobRow, receipt.job_id, with_for_update=True)
        assert job is not None
        job.state = "running"
        job.current_attempt_id = successor_attempt
        session.add(BrainstormAttemptRow(
            id=successor_attempt, job_id=receipt.job_id, number=2,
            state="running", owner="successor-worker", fence=uuid4(),
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            process_settled=False,
        ))
    successor_binding = replace(
        binding, work_unit_id=uuid4(),
        source=SourceBinding(kind="authoring_attempt", source_id=successor_attempt),
    )
    observations = PostgresTaskUsageObservations(brainstorm_session_factory)
    await observations.bind(successor_binding)
    await observations.append_observation(
        Observation(
            binding=successor_binding, event_id="successor-crossing", sequence=1,
            counter_kind=CounterKind.CUMULATIVE,
            tokens=TokenCounts(input_tokens=200, output_tokens=0), final=False,
        )
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory, owner="successor-worker",
        gateway_factory=lambda _: pytest.fail("observation replay launched a gateway"),
        reader_factory=lambda _: type("Reader", (), {})(),
        task_usage=TaskUsageComposition(
            observations=observations,
            totals=TaskUsageTotalsQuery(brainstorm_session_factory),
            baselines=TaskUsageBaselineService(TaskUsageBaselineQuery(brainstorm_session_factory)),
        ),
    )
    await worker.reconcile_committed_observations()
    async with brainstorm_session_factory() as session:
        successor = await PostgresTaskUsageCheckpointRepository(session).latest_for_subject(
            SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id)
        )
        assert successor is not None and successor.generation == 2
        assert successor.state is CheckpointState.REQUESTED
        assert successor.monitoring_policy_revision == 2
        assert successor.policy_revision == 1
        assert successor.reference_digest == "e" * 64
