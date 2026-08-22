"""Framework-free operation-intent and adapter contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from forge.domain.operation import OperationIntent, OperationOutcome


class OperationAdapter(Protocol):
    """An adapter whose remote call is deliberately outside DB transactions."""

    async def invoke(self, intent: OperationIntent) -> OperationOutcome: ...

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome: ...


class OperationRepository(Protocol):
    """Persistence boundary for intent-before-effect orchestration."""

    async def begin(
        self,
        *,
        run_id: UUID,
        operation_type: str,
        idempotency_key: str,
        request_digest: str,
        request_payload: Mapping[str, object],
        request_schema_version: int = 1,
    ) -> OperationIntent: ...

    async def get(self, intent_id: UUID) -> OperationIntent: ...

    async def complete(self, intent_id: UUID, outcome: OperationOutcome) -> OperationIntent: ...

    async def fail(
        self, intent_id: UUID, *, error: str, needs_reconciliation: bool = False
    ) -> OperationIntent: ...

    async def list_unresolved(self) -> Sequence[OperationIntent]: ...


__all__ = ["OperationAdapter", "OperationRepository"]
