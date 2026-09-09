"""Immutable run state and snapshot value types."""

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from forge.domain.approval import ApprovalGate
from forge.domain.resource import ResourceState, validate_resource_shape


class _Unset:
    """Sentinel for resource fields omitted from an immutable update."""


_UNSET = _Unset()


class RunState(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    PREPARING_WORKTREE = "PREPARING_WORKTREE"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    REVIEWING = "REVIEWING"
    REMEDIATING = "REMEDIATING"
    AWAITING_PR_APPROVAL = "AWAITING_PR_APPROVAL"
    PUBLISHING_PR = "PUBLISHING_PR"
    MONITORING_PR = "MONITORING_PR"
    AWAITING_HUMAN_INTERVENTION = "AWAITING_HUMAN_INTERVENTION"
    AWAITING_MERGE_APPROVAL = "AWAITING_MERGE_APPROVAL"
    MERGING = "MERGING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SuspensionKind(StrEnum):
    """Why a run is retaining its previous state."""

    PAUSE = "PAUSE"
    INTERVENTION = "INTERVENTION"


_APPROVAL_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_APPROVAL_STATE_BY_GATE = {
    ApprovalGate.PLAN: RunState.AWAITING_PLAN_APPROVAL,
    ApprovalGate.PR: RunState.AWAITING_PR_APPROVAL,
    ApprovalGate.MERGE: RunState.AWAITING_MERGE_APPROVAL,
}


@dataclass(frozen=True, slots=True)
class SuspensionContext:
    """The state and suspension metadata hidden by an outer pause."""

    state: RunState
    suspended_state: RunState | None
    suspension_kind: SuspensionKind | None
    pending_gate: ApprovalGate | None = None
    pending_evidence_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, RunState):
            raise TypeError("suspension context state must be a RunState")
        if self.suspended_state is not None and not isinstance(self.suspended_state, RunState):
            raise TypeError("suspension context suspended state must be a RunState")
        if self.suspension_kind is not None and not isinstance(
            self.suspension_kind, SuspensionKind
        ):
            raise TypeError("suspension context kind must be a SuspensionKind")
        _validate_pending_metadata(
            self.state,
            self.pending_gate,
            self.pending_evidence_digest,
            field_name="suspension context",
        )


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """The complete immutable state needed to advance one run."""

    id: UUID
    project_id: UUID
    task_id: UUID
    state: RunState = RunState.CREATED
    version: int = 0
    suspended_state: RunState | None = None
    local_remediation_count: int = 0
    remote_remediation_count: int = 0
    suspension_kind: SuspensionKind | None = None
    suspension_context: SuspensionContext | None = None
    # Optional for callers that create pre-Task-10 snapshots. Task 10 run
    # creation supplies all three binding values.
    policy_version: int | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    branch_name: str | None = None
    worktree_path: str | None = None
    database_state: ResourceState = ResourceState.DISABLED
    database_name: str | None = None
    database_role: str | None = None
    secret_id: str | None = None
    pending_gate: ApprovalGate | None = None
    pending_evidence_digest: str | None = None

    def __post_init__(self) -> None:
        validate_resource_shape(
            self.database_state,
            self.database_name,
            self.database_role,
            self.secret_id,
        )
        if not isinstance(self.state, RunState):
            raise TypeError("run state must be a RunState")
        if self.suspension_context is not None and not isinstance(
            self.suspension_context, SuspensionContext
        ):
            raise TypeError("suspension context must be a SuspensionContext")
        _validate_pending_metadata(
            self.state,
            self.pending_gate,
            self.pending_evidence_digest,
            field_name="run",
        )

    def with_state(
        self,
        state: RunState,
        suspended_state: RunState | None = None,
        suspension_kind: SuspensionKind | None = None,
        suspension_context: SuspensionContext | None = None,
        pending_gate: ApprovalGate | None = None,
        pending_evidence_digest: str | None = None,
    ) -> RunSnapshot:
        """Return a new snapshot with one version increment and no mutation."""

        return replace(
            self,
            state=state,
            suspended_state=suspended_state,
            suspension_kind=suspension_kind,
            suspension_context=suspension_context,
            pending_gate=pending_gate,
            pending_evidence_digest=pending_evidence_digest,
            version=self.version + 1,
        )

    def with_resource(
        self,
        *,
        worktree_path: str | None | _Unset = _UNSET,
        database_state: ResourceState | _Unset = _UNSET,
        database_name: str | None | _Unset = _UNSET,
        database_role: str | None | _Unset = _UNSET,
        secret_id: str | None | _Unset = _UNSET,
    ) -> RunSnapshot:
        """Return one versioned resource update without changing workflow state."""

        return replace(
            self,
            version=self.version + 1,
            worktree_path=(
                self.worktree_path if isinstance(worktree_path, _Unset) else worktree_path
            ),
            database_state=(
                self.database_state if isinstance(database_state, _Unset) else database_state
            ),
            database_name=(
                self.database_name if isinstance(database_name, _Unset) else database_name
            ),
            database_role=(
                self.database_role if isinstance(database_role, _Unset) else database_role
            ),
            secret_id=self.secret_id if isinstance(secret_id, _Unset) else secret_id,
        )


def _validate_pending_metadata(
    state: RunState,
    pending_gate: ApprovalGate | None,
    pending_evidence_digest: str | None,
    *,
    field_name: str,
) -> None:
    """Validate the database's closed state/gate/digest shape in memory."""

    if pending_gate is not None and not isinstance(pending_gate, ApprovalGate):
        raise TypeError(f"{field_name} pending gate must be an ApprovalGate")
    approval_state = _APPROVAL_STATE_BY_GATE.get(pending_gate) if pending_gate else None
    if state not in _APPROVAL_STATE_BY_GATE.values():
        if pending_gate is not None or pending_evidence_digest is not None:
            raise ValueError(
                f"{field_name} pending approval metadata is not allowed for this state"
            )
        return
    if approval_state is not state:
        raise ValueError(f"{field_name} pending gate does not match its approval state")
    if (
        not isinstance(pending_evidence_digest, str)
        or _APPROVAL_DIGEST.fullmatch(pending_evidence_digest) is None
    ):
        raise ValueError(f"{field_name} pending evidence digest must be lowercase SHA-256")


__all__ = [
    "RunSnapshot",
    "RunState",
    "SuspensionContext",
    "SuspensionKind",
]
