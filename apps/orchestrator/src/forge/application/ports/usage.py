"""Framework-free usage persistence contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from forge.observability.usage import UsageRecord


class UsageRepository(Protocol):
    """Persist priced model usage and return deterministic grouped totals."""

    async def add_priced(
        self, run_id: UUID, agent_execution_id: UUID, usage: UsageRecord
    ) -> UsageRecord: ...

    async def totals(self, run_id: UUID) -> Sequence[Mapping[str, object]]: ...


__all__ = ["UsageRepository"]
