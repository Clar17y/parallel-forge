"""Focused decomposition-specific usage checkpoint tests for distinct authoring job subjects."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from forge.api.routes.epic_decomposition import _error as decomposition_route_error
from forge.application.ports.epic_brainstorm import BriefInput
from forge.application.ports.epic_decomposition import DecompositionGatewayResult
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.domain.epic_brainstorm import (
    FrozenBriefContent,
    FrozenRequirement,
)
from forge.domain.subscription import (
    RouteBinding,
    RouteSpec,
    TaskBudget,
)
from forge.domain.task_usage_contract import (
    CheckpointRecord,
    CheckpointState,
    MonitoringMode,
    MonitoringPolicy,
    OwnerCommand,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    WorkUnitBinding,
)
from forge.persistence.models.project import Project
from forge.persistence.models.task_usage import (
    TaskUsageBaseline,
    TaskUsageObservation,
    TaskUsageWorkUnit,
)
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from forge.persistence.repositories.epic_decomposition import PostgresEpicDecompositionUnitOfWork
from forge.persistence.repositories.task_usage_admissions import (
    PostgresTaskUsageAdmissionRepository,
)
from forge.persistence.repositories.task_usage_checkpoints import (
    CheckpointConflict,
    PostgresTaskUsageCheckpointRepository,
)
from forge.persistence.repositories.task_usage_policy import (
    MonitoringPolicyConflict,
    PostgresTaskUsagePolicyRepository,
)
from forge.worker.epic_decomposition import ValidatedDecompositionGateway
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DecompositionBriefFixture:
    def __init__(self, epic_id: UUID, project_id: UUID, req_id: UUID) -> None:
        content = FrozenBriefContent(
            problem="Decomposition problem",
            requirements=(FrozenRequirement(requirement_id=req_id, text="Req 1"),),
        )
        self.current = BriefInput(
            epic_id=epic_id,
            project_id=project_id,
            epic_version=1,
            draft_digest="a" * 64,
            accepted_revision_id=uuid4(),
            accepted_digest="b" * 64,
            accepted_content=content,
        )

    async def input(self, epic_id: UUID, *, for_update: bool = False) -> BriefInput:
        assert epic_id == self.current.epic_id
        return self.current

    async def save_proposal_revision(self, epic_id: UUID, *, expected_version: int, source_job_id: UUID, proposal: object) -> UUID:
        return uuid4()


def test_decomposition_checkpoint_conflict_is_http_conflict() -> None:
    assert decomposition_route_error(CheckpointConflict("stale checkpoint")).status_code == 409


@pytest.mark.asyncio
async def test_decomposition_wrapper_forwards_observed_gateway() -> None:
    class Underlying:
        observed = False

        async def execute(self, *args: object, **kwargs: object) -> DecompositionGatewayResult:
            raise AssertionError("unobserved gateway path used")

        async def execute_observed(
            self, *args: object, **kwargs: object
        ) -> DecompositionGatewayResult:
            self.observed = True
            assert kwargs["usage_binding"] is marker
            assert kwargs["usage_sink"] is marker
            return DecompositionGatewayResult(
                proposal=None, telemetry=None, failure="invalid_output"
            )

    async def cancelled() -> bool:
        return False

    marker = object()
    underlying = Underlying()
    result = await ValidatedDecompositionGateway(underlying).execute_observed(
        marker, (), marker, cancelled=cancelled, lifecycle=marker,
        usage_binding=marker, usage_sink=marker,
    )
    assert underlying.observed and result.failure == "invalid_output"


async def _setup_decomposition_project(factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID, UUID, AuthenticatedActor]:
    project_id, epic_id, req_id = uuid4(), uuid4(), uuid4()
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
            ),
            expected_revision=0,
            actor_id=actor.actor_id,
        )
    return project_id, epic_id, req_id, actor


@pytest.mark.asyncio
async def test_decomposition_submit_admits_distinct_authoring_job(decomposition_session_factory):
    project_id, epic_id, req_id, actor = await _setup_decomposition_project(decomposition_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    brief_fixture = DecompositionBriefFixture(epic_id, project_id, req_id)
    authoring_service = EpicBrainstormService(
        decomposition_session_factory,
        lambda _: brief_fixture,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    uow_factory = lambda: PostgresEpicDecompositionUnitOfWork(decomposition_session_factory)
    service = EpicDecompositionService(uow_factory, authoring_service=authoring_service)

    cid, cver = await service.create(epic_id=epic_id, project_id=project_id, actor=actor, key="decomp-create-1", text="Decompose epic")
    turns = await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=cid)
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=cid,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=cver,
        actor=actor,
        key="decomp-submit-1",
    )
    assert receipt.state == "queued"

    async with decomposition_session_factory() as session:
        admissions = PostgresTaskUsageAdmissionRepository(session)
        admission = await admissions.get(SubjectKey(project_id=project_id, kind="authoring_job", subject_id=receipt.job_id))
        assert admission is not None
        assert admission.policy_revision == 1
        assert admission.subject.kind == "authoring_job"


@pytest.mark.asyncio
async def test_decomposition_continuation_and_independent_adoption(decomposition_session_factory):
    project_id, epic_id, req_id, actor = await _setup_decomposition_project(decomposition_session_factory)
    route = RouteSpec(provider="google", client="antigravity_cli", model="gemini-2.5-pro")
    brief_fixture = DecompositionBriefFixture(epic_id, project_id, req_id)
    authoring_service = EpicBrainstormService(
        decomposition_session_factory,
        lambda _: brief_fixture,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    uow_factory = lambda: PostgresEpicDecompositionUnitOfWork(decomposition_session_factory)
    service = EpicDecompositionService(uow_factory, authoring_service=authoring_service)

    cid, cver = await service.create(epic_id=epic_id, project_id=project_id, actor=actor, key="decomp-create-2", text="Decompose epic")
    turns = await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=cid)
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=cid,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=cver,
        actor=actor,
        key="decomp-submit-2",
    )

    # Simulate paused checkpoint
    async with decomposition_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("worker-1", kinds=frozenset(("decomposition",)))
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
                event_id="decomp-trigger-1", sequence=1, counter_kind="cumulative",
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
                trigger_event_id="decomp-trigger-1",
            )
        )
        pausing = await checkpoints.transition(cp.checkpoint_id, expected_version=0, state=CheckpointState.PAUSING, process_settled=False)
        paused = await checkpoints.transition(pausing.checkpoint_id, expected_version=1, state=CheckpointState.PAUSED, process_settled=True)
        job_row.state = "paused"
        attempt_row.state = "settled"
        attempt_row.process_settled = True

    # Test usage checkpoint inspection via decomposition service
    cp_info = await service.usage_checkpoint(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert cp_info is not None
    assert cp_info.state == CheckpointState.PAUSED

    changed_policy = MonitoringPolicy(
        project_id=project_id, revision=2, mode=MonitoringMode.CHECKPOINT,
        warning_multiplier=1.2, checkpoint_multiplier=1.5,
        history_limit=50, minimum_comparables=2,
    )
    policy_command = OwnerCommand(
        command_id=uuid4(), checkpoint_id=paused.checkpoint_id,
        expected_version=paused.version, idempotency_key="decomp-policy-2",
        action="set_policy", actor_id=actor.actor_id,
        requested_policy=changed_policy,
    )
    _, paused = await service.apply_checkpoint_command(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        actor=actor, command=policy_command,
    )
    assert paused.monitoring_policy_revision == 2
    with pytest.raises(MonitoringPolicyConflict, match="stale"):
        await service.apply_checkpoint_command(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            actor=actor,
            command=OwnerCommand(
                command_id=uuid4(), checkpoint_id=paused.checkpoint_id,
                expected_version=paused.version, idempotency_key="decomp-policy-stale",
                action="set_policy", actor_id=actor.actor_id,
                requested_policy=changed_policy,
            ),
        )

    # Apply continue command without usage pauses
    cmd = OwnerCommand(
        command_id=uuid4(),
        checkpoint_id=paused.checkpoint_id,
        expected_version=paused.version,
        idempotency_key="decomp-cmd-continue-1",
        action="continue",
        actor_id=actor.actor_id,
        continue_mode="without_usage_pauses",
    )
    receipt_cmd, updated_cp = await service.apply_checkpoint_command(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id, actor=actor, command=cmd
    )
    assert receipt_cmd.action == "continue"
    assert updated_cp.state == CheckpointState.RESUMED
    assert updated_cp.usage_pauses_disabled is True
    async with decomposition_session_factory() as session, session.begin():
        session.add(TaskUsageObservation(
            id=uuid4(), project_id=project_id, work_unit_id=unit_id,
            event_id="decomp-trigger-2", sequence=2, counter_kind="cumulative",
            input_tokens=200, output_tokens=0, final=False,
            observed_at=datetime.now(UTC),
        ))
        await session.flush()
        next_checkpoint = await PostgresTaskUsageCheckpointRepository(session).request(
            CheckpointRecord(
                checkpoint_id=uuid4(), binding=binding, baseline_binding=binding,
                state=CheckpointState.REQUESTED, policy_revision=1,
                reference_digest="e" * 64, trigger_event_id="decomp-trigger-2",
            )
        )
        assert next_checkpoint.generation == 2
        assert next_checkpoint.monitoring_policy_revision == 2
        assert next_checkpoint.usage_pauses_disabled
        assert next_checkpoint.policy_revision == 1
        assert next_checkpoint.reference_digest == "e" * 64
