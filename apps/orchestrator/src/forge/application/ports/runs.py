"""Framework-free run persistence contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from forge.domain.run import RunSnapshot, RunState


class RunRepository(Protocol):
    """Persistence operations for the authoritative run snapshot."""

    async def get(self, run_id: UUID) -> RunSnapshot: ...

    async def create(self, run: RunSnapshot) -> None: ...

    async def transition(
        self,
        run_id: UUID,
        expected_version: int,
        target: RunState,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...


__all__ = ["RunRepository"]
