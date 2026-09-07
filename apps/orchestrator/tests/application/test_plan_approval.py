"""Task 17 worker plan-gate handler contract and discriminating behavior checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from forge.application.handlers.approvals import ApprovePlanHandler
from forge.application.services.approvals import ApprovalCommandValidationError
from forge.application.services.plan_evidence import (
    CurrentPlanSource,
    PlanEvidenceValidationError,
)
from forge.domain.approval import ApprovalGate
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Approval

FIXED_NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


class _FakeClock:
    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@dataclass
class _FakeAuth:
    approval: Approval | None = None
    create_approval_count: int = 0
    consume_challenge_count: int = 0

    async def get_approval(self, *, approval_id: UUID, for_update: bool = False) -> Approval | None:
        del for_update
        if self.approval is not None and self.approval.id == approval_id:
            return self.approval
        return None

    async def create_approval(self, **kwargs: Any) -> None:
        del kwargs
        self.create_approval_count += 1

    async def consume_challenge(self, **kwargs: Any) -> None:
        del kwargs
        self.consume_challenge_count += 1

    async def invalidate_plan_gate(self, *, run_id: UUID, run_version: int, at: datetime) -> None:
        if (
            self.approval is not None
            and self.approval.run_id == run_id
            and self.approval.run_version == run_version
            and self.approval.gate == "plan"
            and self.approval.invalidated_at is None
        ):
            self.approval.invalidated_at = at


@dataclass
class _TransitionCall:
    run_id: UUID
    expected_version: int
    target: RunState
    event_type: str
    event_payload: dict[str, Any]
    actor_class: str
    actor_id: UUID | None


@dataclass
class _RestartPlanningCall:
    run_id: UUID
    expected_version: int
    policy_version: int
    base_ref: str
    base_sha: str
    event_type: str
    event_payload: dict[str, Any]
    actor_class: str


class _FakeRuns:
    def __init__(self, run: RunSnapshot) -> None:
        self.run = run
        self.transitions: list[_TransitionCall] = []
        self.restarts: list[_RestartPlanningCall] = []

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
        del occurred_at, payload_schema_version
        assert run_id == self.run.id
        assert expected_version == self.run.version
        self.transitions.append(
            _TransitionCall(
                run_id=run_id,
                expected_version=expected_version,
                target=target,
                event_type=event_type,
                event_payload=dict(event_payload),
                actor_class=actor_class,
                actor_id=actor_id,
            )
        )
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

    async def restart_planning(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        policy_version: int,
        base_ref: str,
        base_sha: str,
        event_type: str,
        event_payload: dict[str, Any],
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> RunSnapshot:
        del actor_id, occurred_at
        assert run_id == self.run.id
        assert expected_version == self.run.version
        self.restarts.append(
            _RestartPlanningCall(
                run_id=run_id,
                expected_version=expected_version,
                policy_version=policy_version,
                base_ref=base_ref,
                base_sha=base_sha,
                event_type=event_type,
                event_payload=dict(event_payload),
                actor_class=actor_class,
            )
        )
        self.run = RunSnapshot(
            id=self.run.id,
            project_id=self.run.project_id,
            task_id=self.run.task_id,
            state=RunState.PLANNING,
            version=expected_version + 1,
            policy_version=policy_version,
            base_ref=base_ref,
            base_sha=base_sha,
        )
        return self.run


@dataclass
class _EnqueuedCommand:
    id: UUID
    run_id: UUID
    command_type: str
    idempotency_key: str
    payload: dict[str, Any]
    expected_run_version: int
    actor_id: UUID | None


class _FakeCommands:
    def __init__(self) -> None:
        self.enqueued: list[_EnqueuedCommand] = []

    async def enqueue(
        self,
        *,
        run_id: UUID,
        command_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        expected_run_version: int,
        actor_id: UUID | None = None,
        payload_schema_version: int = 1,
        available_at: datetime | None = None,
    ) -> CommandEnvelope:
        del payload_schema_version, available_at
        command_id = uuid4()
        self.enqueued.append(
            _EnqueuedCommand(
                id=command_id,
                run_id=run_id,
                command_type=command_type,
                idempotency_key=idempotency_key,
                payload=dict(payload),
                expected_run_version=expected_run_version,
                actor_id=actor_id,
            )
        )
        return CommandEnvelope(
            id=command_id,
            run_id=run_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            payload=payload,
            status=CommandStatus.PENDING,
            expected_run_version=expected_run_version,
            actor_id=actor_id,
            payload_schema_version=1,
            attempt=0,
            available_at=FIXED_NOW,
            lease_owner=None,
            lease_expires_at=None,
        )


class _FakeExecutions:
    def __init__(self, next_attempt_value: int = 2) -> None:
        self.next_attempt_value = next_attempt_value

    async def next_attempt(self, run_id: UUID, kind: str) -> int:
        del run_id, kind
        return self.next_attempt_value


class _FakeUnitOfWork:
    def __init__(self, run: RunSnapshot, approval: Approval | None = None) -> None:
        self.auth = _FakeAuth(approval=approval)
        self.runs = _FakeRuns(run)
        self.commands = _FakeCommands()
        self.executions = _FakeExecutions()
        self.commit_count = 0
        self.rollback_count = 0

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


class _FakeValidator:
    def __init__(
        self,
        *,
        drift: bool = False,
        source_policy_version: int = 2,
        source_base_sha: str = "b" * 40,
    ) -> None:
        self.drift = drift
        self.source_policy_version = source_policy_version
        self.source_base_sha = source_base_sha

    async def validate(self, work: Any, run_id: UUID) -> Any:
        del work, run_id
        if self.drift:
            raise PlanEvidenceValidationError("source repository drifted")
        return None

    async def current_source(self, work: Any, run_id: UUID) -> CurrentPlanSource:
        del work, run_id
        return CurrentPlanSource(
            policy_version=self.source_policy_version,
            base_ref="refs/heads/main",
            base_sha=self.source_base_sha,
        )


def _fixture_run(
    *,
    run_id: UUID | None = None,
    version: int = 2,
    policy_version: int = 1,
    state: RunState = RunState.AWAITING_PLAN_APPROVAL,
    evidence_digest: str = "a" * 64,
) -> RunSnapshot:
    return RunSnapshot(
        id=run_id or uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=state,
        version=version,
        policy_version=policy_version,
        base_ref="refs/heads/main",
        base_sha="1" * 40,
        pending_gate=ApprovalGate.PLAN if state is RunState.AWAITING_PLAN_APPROVAL else None,
        pending_evidence_digest=evidence_digest
        if state is RunState.AWAITING_PLAN_APPROVAL
        else None,
    )


def _fixture_approval(
    run: RunSnapshot,
    actor_id: UUID,
    *,
    gate: str = "plan",
    evidence_digest: str | None = None,
    run_version: int | None = None,
    policy_version: int | None = None,
    invalidated_at: datetime | None = None,
) -> Approval:
    return Approval(
        id=uuid4(),
        run_id=run.id,
        gate=gate,
        evidence_digest=evidence_digest or run.pending_evidence_digest or "a" * 64,
        run_version=run.version if run_version is None else run_version,
        policy_version=run.policy_version if policy_version is None else policy_version,
        authenticated_actor_id=actor_id,
        invalidated_at=invalidated_at,
        invalidation_reason=None,
        created_at=FIXED_NOW - timedelta(minutes=1),
    )


def _fixture_command(
    run: RunSnapshot,
    approval: Approval,
    actor_id: UUID,
    *,
    command_type: str = "approve_plan",
    status: CommandStatus = CommandStatus.LEASED,
    expected_run_version: int | None = None,
    payload: dict[str, Any] | None = None,
) -> CommandEnvelope:
    cmd_payload = {"approval_id": str(approval.id)} if payload is None else payload
    return CommandEnvelope(
        id=uuid4(),
        run_id=run.id,
        command_type=command_type,
        idempotency_key=f"approval:{approval.id}",
        payload=cmd_payload,
        status=status,
        expected_run_version=run.version if expected_run_version is None else expected_run_version,
        actor_id=actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=FIXED_NOW,
        lease_owner="worker-1",
        lease_expires_at=FIXED_NOW + timedelta(minutes=2),
    )


def test_approve_plan_handler_is_available() -> None:
    assert ApprovePlanHandler.__name__ == "ApprovePlanHandler"


@pytest.mark.asyncio
async def test_exact_valid_approval_advances_once_and_queues_prepare_worktree() -> None:
    actor_id = uuid4()
    run = _fixture_run(version=3, policy_version=2)
    approval = _fixture_approval(run, actor_id)
    command = _fixture_command(run, approval, actor_id)
    work = _FakeUnitOfWork(run, approval)
    clock = _FakeClock()
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(drift=False), clock=clock)

    await handler(command, work)

    assert len(work.runs.transitions) == 1
    transition = work.runs.transitions[0]
    assert transition.run_id == run.id
    assert transition.expected_version == 3
    assert transition.target is RunState.PREPARING_WORKTREE
    assert transition.event_type == "run.plan_approved"
    assert transition.event_payload == {"approval_id": str(approval.id)}
    assert transition.actor_class == "operator"
    assert transition.actor_id == actor_id

    assert len(work.commands.enqueued) == 1
    enqueued = work.commands.enqueued[0]
    assert enqueued.run_id == run.id
    assert enqueued.command_type == "prepare_worktree"
    assert enqueued.expected_run_version == 4
    assert enqueued.payload == {}
    assert enqueued.actor_id == actor_id
    assert enqueued.idempotency_key == f"{run.id}:prepare-worktree:4"

    assert work.commit_count == 1
    assert work.auth.create_approval_count == 0
    assert work.auth.consume_challenge_count == 0
    assert approval.invalidated_at is None


@pytest.mark.asyncio
async def test_wrong_actor_causes_no_transition_queue_invalidation_or_commit() -> None:
    actor_id = uuid4()
    wrong_actor_id = uuid4()
    run = _fixture_run()
    approval = _fixture_approval(run, actor_id)
    command = _fixture_command(run, approval, wrong_actor_id)
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert approval.invalidated_at is None
    assert work.commit_count == 0
    assert work.auth.create_approval_count == 0
    assert work.auth.consume_challenge_count == 0


@pytest.mark.asyncio
async def test_cross_run_causes_no_transition_queue_invalidation_or_commit() -> None:
    actor_id = uuid4()
    run = _fixture_run()
    other_run = _fixture_run()
    approval = _fixture_approval(other_run, actor_id)
    command = _fixture_command(run, approval, actor_id)
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert approval.invalidated_at is None
    assert work.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_gate", ["pr", "merge", "custom"])
async def test_wrong_gate_causes_no_transition_queue_invalidation_or_commit(
    wrong_gate: str,
) -> None:
    actor_id = uuid4()
    run = _fixture_run()
    approval = _fixture_approval(run, actor_id, gate=wrong_gate)
    command = _fixture_command(run, approval, actor_id)
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert approval.invalidated_at is None
    assert work.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_version", "command_expected_version", "run_version"),
    [
        (2, 3, 2),  # command expected version mismatch
        (3, 3, 2),  # run version mismatch
        (2, 2, 3),  # run version mismatch
    ],
)
async def test_version_mismatch_causes_no_transition_queue_invalidation_or_commit(
    approval_version: int,
    command_expected_version: int,
    run_version: int,
) -> None:
    actor_id = uuid4()
    run = _fixture_run(version=run_version)
    approval = _fixture_approval(run, actor_id, run_version=approval_version)
    command = _fixture_command(
        run, approval, actor_id, expected_run_version=command_expected_version
    )
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert approval.invalidated_at is None
    assert work.commit_count == 0


@pytest.mark.asyncio
async def test_policy_version_mismatch_causes_no_transition_queue_invalidation_or_commit() -> None:
    actor_id = uuid4()
    run = _fixture_run(policy_version=1)
    approval = _fixture_approval(run, actor_id, policy_version=2)
    command = _fixture_command(run, approval, actor_id)
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert approval.invalidated_at is None
    assert work.commit_count == 0


@pytest.mark.asyncio
async def test_evidence_digest_mismatch_causes_no_transition_queue_invalidation_or_commit() -> None:
    actor_id = uuid4()
    run = _fixture_run(evidence_digest="a" * 64)
    approval = _fixture_approval(run, actor_id, evidence_digest="b" * 64)
    command = _fixture_command(run, approval, actor_id)
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert approval.invalidated_at is None
    assert work.commit_count == 0


@pytest.mark.asyncio
async def test_invalidated_record_causes_no_transition_queue_or_commit() -> None:
    actor_id = uuid4()
    run = _fixture_run()
    approval = _fixture_approval(run, actor_id, invalidated_at=FIXED_NOW - timedelta(seconds=10))
    command = _fixture_command(run, approval, actor_id)
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert work.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "non_plan_state",
    [
        RunState.CREATED,
        RunState.PLANNING,
        RunState.PREPARING_WORKTREE,
        RunState.IMPLEMENTING,
        RunState.COMPLETED,
    ],
)
async def test_wrong_run_state_causes_no_transition_queue_invalidation_or_commit(
    non_plan_state: RunState,
) -> None:
    actor_id = uuid4()
    run = _fixture_run(state=non_plan_state)
    approval = _fixture_approval(run, actor_id)
    command = _fixture_command(run, approval, actor_id)
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert approval.invalidated_at is None
    assert work.commit_count == 0


@pytest.mark.asyncio
async def test_missing_approval_causes_no_transition_queue_or_commit() -> None:
    actor_id = uuid4()
    run = _fixture_run()
    missing_approval_id = uuid4()
    command = CommandEnvelope(
        id=uuid4(),
        run_id=run.id,
        command_type="approve_plan",
        idempotency_key=f"approval:{missing_approval_id}",
        payload={"approval_id": str(missing_approval_id)},
        status=CommandStatus.LEASED,
        expected_run_version=run.version,
        actor_id=actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=FIXED_NOW,
        lease_owner="worker-1",
        lease_expires_at=FIXED_NOW + timedelta(minutes=2),
    )
    work = _FakeUnitOfWork(run, approval=None)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert work.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_type", "status", "payload"),
    [
        ("start_planning", CommandStatus.LEASED, {"approval_id": str(uuid4())}),
        ("approve_plan", CommandStatus.PENDING, {"approval_id": str(uuid4())}),
        ("approve_plan", CommandStatus.COMPLETED, {"approval_id": str(uuid4())}),
        ("approve_plan", CommandStatus.LEASED, {}),
        ("approve_plan", CommandStatus.LEASED, {"approval_id": "not-a-uuid"}),
        ("approve_plan", CommandStatus.LEASED, {"approval_id": str(uuid4()), "extra": "x"}),
    ],
)
async def test_invalid_command_envelope_causes_no_transition_queue_or_commit(
    command_type: str, status: CommandStatus, payload: dict[str, Any]
) -> None:
    actor_id = uuid4()
    run = _fixture_run()
    approval = _fixture_approval(run, actor_id)
    command = CommandEnvelope(
        id=uuid4(),
        run_id=run.id,
        command_type=command_type,
        idempotency_key="test:command",
        payload=payload,
        status=status,
        expected_run_version=run.version,
        actor_id=actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=FIXED_NOW,
        lease_owner="worker-1" if status is CommandStatus.LEASED else None,
        lease_expires_at=FIXED_NOW + timedelta(minutes=2)
        if status is CommandStatus.LEASED
        else None,
        completed_at=FIXED_NOW if status is CommandStatus.COMPLETED else None,
    )
    work = _FakeUnitOfWork(run, approval)
    handler = ApprovePlanHandler(evidence_validator=_FakeValidator(), clock=_FakeClock())

    with pytest.raises(ApprovalCommandValidationError, match="approval command is invalid"):
        await handler(command, work)

    assert len(work.runs.transitions) == 0
    assert len(work.commands.enqueued) == 0
    assert approval.invalidated_at is None
    assert work.commit_count == 0


@pytest.mark.asyncio
async def test_authoritative_drift_invalidates_approval_and_enqueues_restart_planning() -> None:
    actor_id = uuid4()
    run = _fixture_run(version=3, policy_version=1)
    approval = _fixture_approval(run, actor_id)
    command = _fixture_command(run, approval, actor_id)
    work = _FakeUnitOfWork(run, approval)
    clock = _FakeClock()
    handler = ApprovePlanHandler(
        evidence_validator=_FakeValidator(
            drift=True, source_policy_version=2, source_base_sha="2" * 40
        ),
        clock=clock,
    )

    await handler(command, work)

    # Invalidation occurred
    assert approval.invalidated_at == FIXED_NOW

    # Restart planning called instead of transition to PREPARING_WORKTREE
    assert len(work.runs.transitions) == 0
    assert len(work.runs.restarts) == 1
    restart = work.runs.restarts[0]
    assert restart.run_id == run.id
    assert restart.expected_version == 3
    assert restart.policy_version == 2
    assert restart.base_sha == "2" * 40
    assert restart.event_type == "approval.stale"
    assert restart.event_payload == {
        "approval_id": str(approval.id),
        "command_id": str(command.id),
        "planning_command_id": str(work.commands.enqueued[0].id),
        "planning_payload": {"semantic_attempt": 2},
        "semantic_attempt": 2,
    }
    assert restart.actor_class == "worker"

    # Queues start_planning command with semantic attempt
    assert len(work.commands.enqueued) == 1
    enqueued = work.commands.enqueued[0]
    assert enqueued.run_id == run.id
    assert enqueued.command_type == "start_planning"
    assert enqueued.expected_run_version == 4
    assert enqueued.payload == {"semantic_attempt": 2}
    assert enqueued.idempotency_key == f"{run.id}:start-planning:2"
    assert enqueued.actor_id == actor_id

    assert work.commit_count == 1
    assert work.auth.create_approval_count == 0
    assert work.auth.consume_challenge_count == 0
