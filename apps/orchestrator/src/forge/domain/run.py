"""Immutable run state and snapshot value types."""

from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID


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


@dataclass(frozen=True, slots=True)
class SuspensionContext:
    """The state and suspension metadata hidden by an outer pause."""

    state: RunState
    suspended_state: RunState | None
    suspension_kind: SuspensionKind | None


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

    def with_state(
        self,
        state: RunState,
        suspended_state: RunState | None = None,
        suspension_kind: SuspensionKind | None = None,
        suspension_context: SuspensionContext | None = None,
    ) -> RunSnapshot:
        """Return a new snapshot with one version increment and no mutation."""

        return replace(
            self,
            state=state,
            suspended_state=suspended_state,
            suspension_kind=suspension_kind,
            suspension_context=suspension_context,
            version=self.version + 1,
        )


__all__ = ["RunSnapshot", "RunState", "SuspensionContext", "SuspensionKind"]
