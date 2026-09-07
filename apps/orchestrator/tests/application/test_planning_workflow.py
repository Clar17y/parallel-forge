"""Task 17 PlanningService boundary tests: contracts, immutable context, readonly tools, and semantic attempts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from forge.agents.prompt_loader import PromptLoader
from forge.application.ports.executions import ExecutionAdmission, ExecutionStatus
from forge.application.ports.projects import ProjectPolicyRecord, ProjectRecord
from forge.application.ports.tasks import TaskRecord
from forge.application.services.planning import (
    PlanningOutcome,
    PlanningRecoveryRequired,
    PlanningService,
    PlanningValidationError,
    canonical_json_bytes,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    UntrustedSourceKind,
)
from forge.domain.approval import ApprovalGate
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.plan import PlanOutput
from forge.domain.policy import AgentModelPolicy, CommandSpec, ProjectPolicy, StepKind
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import ToolName
from forge.observability.usage import UsageRecord
from forge.persistence.repositories.artifacts import ArtifactNotFound
from forge.tools.repository import RepositoryReader

FIXED_NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


class _FakeClock:
    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _RecordingGateway:
    def __init__(
        self,
        *,
        output: PlanOutput | None = None,
        finish_status: AgentFinishStatus = AgentFinishStatus.SUCCEEDED,
    ) -> None:
        self.requests: list[AgentRequest] = []
        self._output = output
        self._finish_status = finish_status

    async def execute(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        plan_output = self._output or PlanOutput(
            summary="Initial plan",
            assumptions=(),
            affected_components=("backend",),
            steps=("Implement planner coverage",),
            required_checks=("unit",),
            risks=("Risk 1",),
            security_considerations=(),
            dependency_changes=(),
        )
        usage = UsageRecord(
            provider=request.provider,
            model=request.model,
            prompt_version=request.instruction_version,
            input_tokens=100,
            output_tokens=50,
            duration_ms=250,
            tool_call_count=2,
            pricing_version="test-v1",
            estimated_cost_minor=1,
            currency="USD",
        )
        return AgentResult(
            execution_id=request.execution_id,
            role=AgentRole.PLANNER,
            finish_status=self._finish_status,
            output=plan_output if self._finish_status is AgentFinishStatus.SUCCEEDED else None,
            parent_execution_id=None,
            provider=request.provider,
            model=request.model,
            instruction_digest=request.instruction_digest,
            usage=usage,
            usage_attempts=(usage,),
            tool_call_count=usage.tool_call_count,
            duration_ms=usage.duration_ms,
        )


@dataclass
class _FinalizedExecution:
    execution_id: UUID
    status: ExecutionStatus
    finish_status: AgentFinishStatus


class _FakeExecutionsRepo:
    async def next_attempt(self, run_id: UUID, kind: str) -> int:
        assert kind == "plan"
        return 2

    def __init__(self, *, is_new: bool = True) -> None:
        self.is_new = is_new
        self.admitted_attempts: list[int] = []
        self.admitted_roles: list[AgentRole] = []
        self.finalized: list[_FinalizedExecution] = []

    async def admit(
        self,
        run_id: UUID,
        step_id: UUID,
        agent_execution_id: UUID,
        kind: str,
        attempt: int,
        role: AgentRole,
        instruction_version: str,
        provider: str,
        model: str,
        *,
        input_artifact_id: UUID | None = None,
        transition_from: str | None = None,
        transition_to: str | None = None,
        admitted_at: datetime | None = None,
    ) -> ExecutionAdmission:
        del transition_from, transition_to
        self.admitted_attempts.append(attempt)
        self.admitted_roles.append(role)
        return ExecutionAdmission(
            run_id=run_id,
            step_id=step_id,
            agent_execution_id=agent_execution_id,
            kind=kind,
            attempt=attempt,
            role=role,
            instruction_version=instruction_version,
            provider=provider,
            model=model,
            input_artifact_id=input_artifact_id,
            transition_from="CREATED",
            transition_to="PLANNING",
            admitted_at=admitted_at or FIXED_NOW,
            is_new=self.is_new,
        )

    async def finalize(
        self,
        run_id: UUID,
        step_id: UUID,
        execution_id: UUID,
        finish_status: AgentFinishStatus,
        usage: UsageRecord,
        *,
        output_artifact_id: UUID | None = None,
        completed_at: datetime | None = None,
        provider: str,
        model: str,
        instruction_version: str,
        kind: str,
        attempt: int,
        role: AgentRole,
    ) -> _FinalizedExecution:
        del (
            run_id,
            step_id,
            usage,
            output_artifact_id,
            completed_at,
            provider,
            model,
            instruction_version,
            kind,
            attempt,
            role,
        )
        status = (
            ExecutionStatus.SUCCEEDED
            if finish_status is AgentFinishStatus.SUCCEEDED
            else ExecutionStatus.FAILED
        )
        record = _FinalizedExecution(
            execution_id=execution_id, status=status, finish_status=finish_status
        )
        self.finalized.append(record)
        return record


class _FakeArtifactsRepo:
    def __init__(self) -> None:
        self.by_digest: dict[str, ArtifactDescriptor] = {}
        self.recorded: list[ArtifactDescriptor] = []

    async def get_by_digest(self, digest: str, *, run_id: UUID | None = None) -> ArtifactDescriptor:
        del run_id
        if digest in self.by_digest:
            return self.by_digest[digest]
        raise ArtifactNotFound(f"artifact not found: {digest}")

    async def record(
        self,
        descriptor: ArtifactDescriptor,
        *,
        run_id: UUID,
        producer_type: str,
        producer_id: UUID,
        parent_digests: tuple[str, ...] = (),
    ) -> ArtifactDescriptor:
        artifact_id = descriptor.artifact_id or uuid4()
        recorded = ArtifactDescriptor(
            digest=descriptor.digest,
            media_type=descriptor.media_type,
            byte_count=descriptor.byte_count,
            storage_path=descriptor.storage_path,
            producer_type=producer_type,
            producer_id=producer_id,
            run_id=run_id,
            parent_digests=parent_digests,
            artifact_id=artifact_id,
        )
        self.by_digest[recorded.digest] = recorded
        self.recorded.append(recorded)
        return recorded


class _FakeCommandsRepo:
    def __init__(self) -> None:
        self.commands: dict[UUID, CommandEnvelope] = {}

    def add(self, command: CommandEnvelope) -> None:
        self.commands[command.id] = command

    async def get(self, command_id: UUID) -> CommandEnvelope:
        if command_id in self.commands:
            return self.commands[command_id]
        raise LookupError(f"command not found: {command_id}")


class _FakeRunsRepo:
    def __init__(self, run: RunSnapshot) -> None:
        self.run = run

    async def get_for_update(self, run_id: UUID) -> RunSnapshot:
        assert run_id == self.run.id
        return self.run

    async def transition(
        self,
        run_id: UUID,
        expected_version: int,
        target: RunState,
        event_type: str,
        event_payload: dict[str, Any],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot:
        del actor_class, actor_id, occurred_at, payload_schema_version, event_type, event_payload
        assert run_id == self.run.id
        assert expected_version == self.run.version
        self.run = RunSnapshot(
            id=self.run.id,
            project_id=self.run.project_id,
            task_id=self.run.task_id,
            state=target,
            version=expected_version + 1,
            policy_version=self.run.policy_version,
            base_ref=self.run.base_ref,
            base_sha=self.run.base_sha,
        )
        return self.run

    async def await_approval(
        self,
        run_id: UUID,
        expected_version: int,
        gate: ApprovalGate,
        evidence_digest: str,
        event_type: str,
        event_payload: dict[str, Any],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot:
        del actor_class, actor_id, occurred_at, payload_schema_version, event_type, event_payload
        assert run_id == self.run.id
        assert expected_version == self.run.version
        self.run = RunSnapshot(
            id=self.run.id,
            project_id=self.run.project_id,
            task_id=self.run.task_id,
            state=RunState.AWAITING_PLAN_APPROVAL,
            version=expected_version + 1,
            policy_version=self.run.policy_version,
            base_ref=self.run.base_ref,
            base_sha=self.run.base_sha,
            pending_gate=gate,
            pending_evidence_digest=evidence_digest,
        )
        return self.run

    async def intervene(
        self,
        run_id: UUID,
        expected_version: int,
        event_type: str,
        event_payload: dict[str, Any],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot:
        del actor_class, actor_id, occurred_at, payload_schema_version, event_type, event_payload
        assert run_id == self.run.id
        assert expected_version == self.run.version
        self.run = RunSnapshot(
            id=self.run.id,
            project_id=self.run.project_id,
            task_id=self.run.task_id,
            state=RunState.AWAITING_HUMAN_INTERVENTION,
            version=expected_version + 1,
            policy_version=self.run.policy_version,
            base_ref=self.run.base_ref,
            base_sha=self.run.base_sha,
        )
        return self.run


class _FakeTasksRepo:
    def __init__(self, task: TaskRecord) -> None:
        self.task = task

    async def get(self, task_id: UUID, *, for_update: bool = False) -> TaskRecord:
        del for_update
        assert task_id == self.task.id
        return self.task


class _FakeProjectsRepo:
    def __init__(self, project: ProjectRecord, policy_record: ProjectPolicyRecord) -> None:
        self.project = project
        self.policy_record = policy_record

    async def get(self, project_id: UUID, *, for_update: bool = False) -> ProjectRecord:
        del for_update
        assert project_id == self.project.id
        return self.project

    async def get_policy(
        self, project_id: UUID, version: int, *, for_update: bool = False
    ) -> ProjectPolicyRecord:
        del for_update
        assert project_id == self.project.id
        assert version == self.policy_record.version
        return self.policy_record


class _FakeEventsRepo:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def list_for_version(self, run_id: UUID, version: int) -> list[RunEvent]:
        return [e for e in self.events if e.run_id == run_id and e.run_version == version]


class _FakeUnitOfWork:
    def __init__(
        self,
        run: RunSnapshot,
        task: TaskRecord,
        project: ProjectRecord,
        policy_record: ProjectPolicyRecord,
        *,
        is_new_admission: bool = True,
    ) -> None:
        self.runs = _FakeRunsRepo(run)
        self.tasks = _FakeTasksRepo(task)
        self.projects = _FakeProjectsRepo(project, policy_record)
        self.executions = _FakeExecutionsRepo(is_new=is_new_admission)
        self.artifacts = _FakeArtifactsRepo()
        self.commands = _FakeCommandsRepo()
        self.events = _FakeEventsRepo()
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _build_fixture(
    tmp_path: Path,
    *,
    run_state: RunState = RunState.CREATED,
    run_version: int = 0,
    policy_version: int = 1,
    is_new_admission: bool = True,
    gateway_output: PlanOutput | None = None,
) -> tuple[PlanningService, _RecordingGateway, _FakeUnitOfWork, CommandEnvelope, ProjectPolicy]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "README.md").write_text("Test repository\n", encoding="utf-8")

    prompts_dir = tmp_path / "prompts" / "planner"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "instructions.md").write_text(
        "<!-- forge-instruction-version: planner-test-v1 -->\nPlan carefully without escalation.\n",
        encoding="utf-8",
    )

    project_id = uuid4()
    task_id = uuid4()
    run_id = uuid4()

    policy = ProjectPolicy(
        id=project_id,
        version=policy_version,
        repository_path=str(repo_dir.resolve()),
        github_repository="forge/planning-test",
        default_branch="main",
        commands=(
            CommandSpec(
                kind=StepKind.TEST,
                name="unit",
                argv=("python", "-m", "pytest"),
                timeout_seconds=60,
            ),
        ),
        planner_model=AgentModelPolicy(provider="test-provider", model="planner-model"),
    )
    doc = policy.model_dump(mode="json")
    policy_digest = hashlib.sha256(canonical_json_bytes(doc)).hexdigest()

    policy_record = ProjectPolicyRecord(
        project_id=project_id,
        version=policy_version,
        policy_digest=policy_digest,
        document_schema_version=1,
        document=doc,
    )

    project_record = ProjectRecord(
        id=project_id,
        name="Planning Test",
        canonical_path=policy.repository_path,
        canonical_path_key=policy.repository_path,
        github_repository=policy.github_repository,
        default_branch=policy.default_branch,
        instructions_path=None,
        current_policy_version=policy_version,
        policy=policy_record,
    )

    task_record = TaskRecord(
        id=task_id,
        project_id=project_id,
        title="Planning task",
        body="Execute planning boundary safely.",
        source_url=None,
        source_updated_at=None,
        untrusted_external_content=False,
        normalized_text="Title: Planning task\n\nExecute planning boundary safely.",
        task_digest="0" * 64,
        external_source=None,
        external_id=None,
    )

    run_snapshot = RunSnapshot(
        id=run_id,
        project_id=project_id,
        task_id=task_id,
        state=run_state,
        version=run_version,
        policy_version=policy_version,
        base_ref="refs/heads/main",
        base_sha="a" * 40,
        pending_gate=ApprovalGate.PLAN if run_state is RunState.AWAITING_PLAN_APPROVAL else None,
        pending_evidence_digest="f" * 64 if run_state is RunState.AWAITING_PLAN_APPROVAL else None,
    )

    work = _FakeUnitOfWork(
        run_snapshot,
        task_record,
        project_record,
        policy_record,
        is_new_admission=is_new_admission,
    )

    gateway = _RecordingGateway(output=gateway_output)
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    prompt_loader = PromptLoader(tmp_path / "prompts")

    service = PlanningService(
        gateway,
        artifact_store,
        prompt_loader,
        lambda pol: RepositoryReader(
            pol.repository_path,
            secret_paths=pol.effective_secret_paths,
            force_python_search=True,
        ),
        clock=_FakeClock(),
    )

    command = CommandEnvelope(
        id=uuid4(),
        run_id=run_id,
        command_type="start_planning",
        idempotency_key=f"start-planning:{run_id}",
        payload={},
        status=CommandStatus.LEASED,
        expected_run_version=run_version,
        actor_id=uuid4(),
        payload_schema_version=1,
        attempt=1,
        available_at=FIXED_NOW,
        lease_owner="worker-1",
        lease_expires_at=FIXED_NOW + timedelta(minutes=2),
    )

    return service, gateway, work, command, policy


@pytest.mark.asyncio
async def test_planning_initial_execution_binds_immutable_task_and_readonly_tools(
    tmp_path: Path,
) -> None:
    service, gateway, work, command, _ = _build_fixture(
        tmp_path, run_state=RunState.CREATED, run_version=0
    )

    outcome = await service.execute(command, work)

    assert isinstance(outcome, PlanningOutcome)
    assert outcome.changed is True
    assert outcome.run_state is RunState.AWAITING_PLAN_APPROVAL
    assert outcome.finish_status is AgentFinishStatus.SUCCEEDED
    assert outcome.evidence_digest is not None
    assert outcome.plan is not None
    assert outcome.evidence is not None

    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.role is AgentRole.PLANNER
    assert request.allowed_tools == (
        ToolName.REPOSITORY_LIST_FILES,
        ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH,
        ToolName.REPOSITORY_READ_INSTRUCTIONS,
    )
    assert request.context.original_task.content == work.tasks.task.normalized_text
    assert request.context.original_task.source_kind is UntrustedSourceKind.TASK
    assert request.context.revision_feedback is None

    # System instruction must be isolated from untrusted task content
    assert work.tasks.task.normalized_text not in request.system_instruction
    assert work.committed is True


@pytest.mark.asyncio
async def test_fail_closed_ambiguous_initial_planning_replay_requires_recovery(
    tmp_path: Path,
) -> None:
    # Run is already in PLANNING state; a delivery retry arrives with initial payload {}
    service, gateway, work, command, _ = _build_fixture(
        tmp_path, run_state=RunState.PLANNING, run_version=1
    )

    with pytest.raises(PlanningRecoveryRequired, match="planning recovery is required"):
        await service.execute(command, work)

    assert len(gateway.requests) == 0
    assert work.committed is True  # preserved fail-closed transaction


@pytest.mark.asyncio
async def test_semantic_attempt_distinction_allows_revision_retry(
    tmp_path: Path,
) -> None:
    # Run is in PLANNING; command explicitly supplies semantic_attempt >= 2
    service, gateway, work, base_command, _ = _build_fixture(
        tmp_path, run_state=RunState.PLANNING, run_version=1
    )
    revision_command = CommandEnvelope(
        id=uuid4(),
        run_id=base_command.run_id,
        command_type="start_planning",
        idempotency_key=f"start-planning:{base_command.run_id}:2",
        payload={"semantic_attempt": 2},
        status=CommandStatus.LEASED,
        expected_run_version=1,
        actor_id=base_command.actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=FIXED_NOW,
        lease_owner="worker-1",
        lease_expires_at=FIXED_NOW + timedelta(minutes=2),
    )

    source = CommandEnvelope(
        id=uuid4(),
        run_id=revision_command.run_id,
        command_type="approve_plan",
        idempotency_key="stale-approval",
        payload_schema_version=1,
        attempt=1,
        lease_owner=None,
        lease_expires_at=None,
        payload={"approval_id": str(uuid4())},
        status=CommandStatus.COMPLETED,
        expected_run_version=0,
        actor_id=revision_command.actor_id,
        available_at=FIXED_NOW,
        completed_at=FIXED_NOW,
    )
    work.commands.add(source)
    work.events.events.append(
        RunEvent(
            run_id=source.run_id,
            run_version=1,
            event_type="approval.stale",
            actor_class="worker",
            actor_id=source.actor_id,
            payload={
                "command_id": str(source.id),
                "approval_id": source.payload["approval_id"],
                "planning_command_id": str(revision_command.id),
                "planning_payload": dict(revision_command.payload),
                "semantic_attempt": 2,
            },
        )
    )
    outcome = await service.execute(revision_command, work)

    assert outcome.changed is True
    assert outcome.run_state is RunState.AWAITING_PLAN_APPROVAL
    assert len(gateway.requests) == 1
    assert work.executions.admitted_attempts == [2]
    assert work.executions.admitted_roles == [AgentRole.PLANNER]


@pytest.mark.asyncio
async def test_revision_feedback_is_untrusted_and_original_task_preserved(
    tmp_path: Path,
) -> None:
    service, gateway, work, base_command, _ = _build_fixture(
        tmp_path, run_state=RunState.PLANNING, run_version=1
    )

    feedback_text = "Split large migration step into two smaller steps."
    revision_cmd_id = uuid4()
    feedback_payload = {
        "schema_version": 1,
        "command_id": str(revision_cmd_id),
        "feedback": feedback_text,
    }
    feedback_bytes = json.dumps(feedback_payload, separators=(",", ":")).encode("utf-8")
    feedback_digest = hashlib.sha256(feedback_bytes).hexdigest()

    # Pre-store completed request_plan_revision command
    revision_req_command = CommandEnvelope(
        id=revision_cmd_id,
        run_id=base_command.run_id,
        command_type="request_plan_revision",
        idempotency_key=f"request-plan-revision:{base_command.run_id}:1",
        payload={"feedback": feedback_text},
        status=CommandStatus.COMPLETED,
        expected_run_version=0,
        actor_id=base_command.actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=FIXED_NOW,
        completed_at=FIXED_NOW,
        lease_owner=None,
        lease_expires_at=None,
    )
    work.commands.add(revision_req_command)

    # Pre-store feedback artifact
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    descriptor = await store.put_bytes(feedback_bytes, media_type="application/json")
    await work.artifacts.record(
        descriptor,
        run_id=base_command.run_id,
        producer_type="plan_revision_feedback",
        producer_id=revision_cmd_id,
    )

    revision_command = CommandEnvelope(
        id=uuid4(),
        run_id=base_command.run_id,
        command_type="start_planning",
        idempotency_key=f"start-planning:{base_command.run_id}:2",
        payload={"semantic_attempt": 2, "feedback_digest": feedback_digest},
        status=CommandStatus.LEASED,
        expected_run_version=1,
        actor_id=base_command.actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=FIXED_NOW,
        lease_owner="worker-1",
        lease_expires_at=FIXED_NOW + timedelta(minutes=2),
    )

    work.events.events.append(
        RunEvent(
            run_id=revision_command.run_id,
            run_version=1,
            event_type="run.plan_revision_requested",
            actor_class="operator",
            actor_id=revision_command.actor_id,
            payload={
                "command_id": str(revision_req_command.id),
                "planning_command_id": str(revision_command.id),
                "planning_payload": dict(revision_command.payload),
                "feedback_digest": feedback_digest,
                "semantic_attempt": 2,
            },
        )
    )
    outcome = await service.execute(revision_command, work)

    assert outcome.changed is True
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.context.revision_feedback is not None
    assert request.context.revision_feedback.content == feedback_text
    assert request.context.revision_feedback.source_kind is UntrustedSourceKind.TASK
    assert request.context.revision_feedback.source_reference == f"revision:{feedback_digest}"

    # Original task text remains intact
    assert request.context.original_task.content == work.tasks.task.normalized_text
    assert feedback_text not in request.system_instruction


@pytest.mark.asyncio
async def test_queue_delivery_attempt_is_independent_from_semantic_attempt(
    tmp_path: Path,
) -> None:
    # Command delivery attempt is 7 (queue leased 7 times), but semantic attempt is 1 (initial)
    service, _, work, base_command, _ = _build_fixture(
        tmp_path, run_state=RunState.CREATED, run_version=0
    )
    high_delivery_command = CommandEnvelope(
        id=uuid4(),
        run_id=base_command.run_id,
        command_type="start_planning",
        idempotency_key="queue:test:delivery",
        payload={},
        status=CommandStatus.LEASED,
        expected_run_version=0,
        actor_id=base_command.actor_id,
        payload_schema_version=1,
        attempt=7,
        available_at=FIXED_NOW,
        lease_owner="worker-1",
        lease_expires_at=FIXED_NOW + timedelta(minutes=2),
    )

    await service.execute(high_delivery_command, work)

    # Execution attempt admitted is 1, not 7!
    assert work.executions.admitted_attempts == [1]


@pytest.mark.asyncio
async def test_idempotent_replay_when_already_awaiting_approval(tmp_path: Path) -> None:
    service, gateway, work, command, _ = _build_fixture(
        tmp_path, run_state=RunState.AWAITING_PLAN_APPROVAL, run_version=2
    )

    outcome = await service.execute(command, work)

    assert outcome.changed is False
    assert outcome.run_state is RunState.AWAITING_PLAN_APPROVAL
    assert len(gateway.requests) == 0  # gateway is not replayed
    assert work.committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_state",
    [RunState.IMPLEMENTING, RunState.VALIDATING, RunState.COMPLETED, RunState.FAILED],
)
async def test_planning_rejects_invalid_run_state(tmp_path: Path, invalid_state: RunState) -> None:
    service, gateway, work, command, _ = _build_fixture(
        tmp_path, run_state=invalid_state, run_version=1
    )

    with pytest.raises(PlanningValidationError, match="planning request is invalid"):
        await service.execute(command, work)

    assert len(gateway.requests) == 0
    assert work.rolled_back is True


@pytest.mark.asyncio
async def test_planning_rejects_version_mismatch(tmp_path: Path) -> None:
    service, gateway, work, command, _ = _build_fixture(
        tmp_path, run_state=RunState.CREATED, run_version=0
    )
    mismatched_command = CommandEnvelope(
        id=uuid4(),
        run_id=command.run_id,
        command_type="start_planning",
        idempotency_key="mismatch:cmd",
        payload={},
        status=CommandStatus.LEASED,
        expected_run_version=1,  # expected 1, run is 0
        actor_id=command.actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=FIXED_NOW,
        lease_owner="worker-1",
        lease_expires_at=FIXED_NOW + timedelta(minutes=2),
    )

    with pytest.raises(PlanningValidationError, match="planning request is invalid"):
        await service.execute(mismatched_command, work)

    assert len(gateway.requests) == 0
    assert work.rolled_back is True


@pytest.mark.asyncio
async def test_planning_fails_closed_when_plan_omits_policy_required_checks(
    tmp_path: Path,
) -> None:
    plan_missing_checks = PlanOutput(
        summary="Plan missing unit check",
        assumptions=(),
        affected_components=("backend",),
        steps=("Step",),
        required_checks=("other_check",),  # Omits 'unit' required check!
        risks=("Risk 1",),
        security_considerations=(),
        dependency_changes=(),
    )
    service, _, work, command, _ = _build_fixture(
        tmp_path,
        run_state=RunState.CREATED,
        run_version=0,
        gateway_output=plan_missing_checks,
    )

    outcome = await service.execute(command, work)

    assert outcome.changed is True
    assert outcome.run_state is RunState.AWAITING_HUMAN_INTERVENTION
    assert outcome.finish_status is AgentFinishStatus.FAILED
    assert work.runs.run.state is RunState.AWAITING_HUMAN_INTERVENTION


@pytest.mark.asyncio
async def test_planning_recovery_required_on_non_new_admission(tmp_path: Path) -> None:
    service, gateway, work, command, _ = _build_fixture(
        tmp_path,
        run_state=RunState.CREATED,
        run_version=0,
        is_new_admission=False,  # Duplicate or concurrent admission race
    )

    with pytest.raises(PlanningRecoveryRequired, match="planning recovery is required"):
        await service.execute(command, work)

    assert len(gateway.requests) == 0
