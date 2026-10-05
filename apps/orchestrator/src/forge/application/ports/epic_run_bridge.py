"""Caller-transaction persistence and trusted eligibility contracts."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from forge.domain.epic_run_bridge import DependencyEvidence, EpicAttempt, EpicExecutionSnapshot


class EpicRunBridgeRepository(Protocol):
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
