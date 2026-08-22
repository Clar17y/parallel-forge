"""Framework-free durable command contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from forge.domain.command import CommandEnvelope


class CommandRepository(Protocol):
    """Persistence boundary for idempotent commands and worker leases."""

    async def enqueue(
        self,
        *,
        run_id: UUID,
        command_type: str,
        idempotency_key: str,
        payload: Mapping[str, object],
        expected_run_version: int = 0,
        actor_id: UUID | None = None,
        payload_schema_version: int = 1,
        available_at: datetime | None = None,
    ) -> CommandEnvelope: ...

    async def get(self, command_id: UUID) -> CommandEnvelope: ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> CommandEnvelope | None: ...

    async def claim_next(
        self, *, worker_id: str, lease_seconds: float
    ) -> CommandEnvelope | None: ...

    async def renew(
        self, command_id: UUID, *, worker_id: str, lease_seconds: float
    ) -> CommandEnvelope: ...

    async def complete(
        self, command_id: UUID, *, worker_id: str, result: Mapping[str, object] | None = None
    ) -> CommandEnvelope: ...

    async def fail(
        self,
        command_id: UUID,
        *,
        worker_id: str,
        error: str,
        transient: bool = False,
    ) -> CommandEnvelope: ...


__all__ = ["CommandRepository"]
