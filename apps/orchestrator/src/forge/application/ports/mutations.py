"""Durable API mutation receipt application contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ApiMutationRecord:
    """A hashed-key mutation receipt and its optional safe response."""

    id: UUID
    actor_id: UUID
    action: str
    scope: str
    key_hash: str
    request_digest: str
    lifecycle_state: str
    response_status: int | None
    response_payload: dict[str, object] | None
    resource_kind: str | None
    resource_id: UUID | None
    is_replay: bool = False


class MutationRepository(Protocol):
    """Reserve and complete receipts in the caller's transaction."""

    async def reserve(
        self,
        *,
        actor_id: UUID,
        action: str,
        scope: str,
        idempotency_key: str,
        request_digest: str,
    ) -> ApiMutationRecord: ...

    async def complete(
        self,
        mutation_id: UUID,
        *,
        response_status: int,
        response_payload: Mapping[str, object],
        resource_kind: str | None = None,
        resource_id: UUID | None = None,
    ) -> ApiMutationRecord: ...

    async def get(self, mutation_id: UUID) -> ApiMutationRecord: ...


__all__ = ["ApiMutationRecord", "MutationRepository"]
