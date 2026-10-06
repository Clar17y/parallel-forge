"""Caller-transaction persistence and trusted eligibility contracts."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from forge.domain.epic_run_bridge import DependencyEvidence, EpicAttempt, EpicExecutionSnapshot
from forge.domain.subscription import TaskBudget


class EpicRunBridgeRepository(Protocol):
    async def has_active_execution(self, epic_id: UUID) -> bool: ...
    async def control_state(self, execution_id: UUID) -> str | None: ...
    async def note_owner_child_admission(self, execution_id: UUID) -> None: ...
    async def child_budget_blockers(
        self, epic_id: UUID, project_id: UUID, *, ceiling: TaskBudget, hold: TaskBudget
    ) -> list[str]: ...
    async def create_child_hold(self, attempt: EpicAttempt, budget: TaskBudget) -> None: ...
    async def create_execution(
        self,
        *,
        epic_id: UUID,
        brief_revision_id: UUID,
        brief_digest: str,
        graph_revision_id: UUID,
        graph_digest: str,
    ) -> EpicExecutionSnapshot: ...
    async def get_execution(self, execution_id: UUID) -> EpicExecutionSnapshot: ...
    async def create_attempt(self, attempt: EpicAttempt) -> None: ...
    async def get_attempt(self, epic_id: UUID, attempt_id: UUID) -> EpicAttempt: ...
    async def list_attempts(self, epic_id: UUID) -> Sequence[EpicAttempt]: ...


class EligibilityPort(Protocol):
    """Only server-owned producers can attest integrated predecessors."""

    async def evidence(
        self, *, epic_id: UUID, item_ids: Sequence[UUID], base_sha: str
    ) -> Sequence[DependencyEvidence]: ...
