"""Intent-before-effect execution and restart reconciliation services."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from forge.application.ports.operations import OperationAdapter, OperationRepository
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
)


class RecoveryError(RuntimeError):
    """A recovery pass cannot safely continue."""


class OperationExecutor:
    """Persist an intent before invoking an adapter and never blindly reinvoke."""

    def __init__(self, operations: OperationRepository) -> None:
        self._operations = operations

    async def execute(
        self,
        request: OperationRequest | Mapping[str, object],
        adapter: OperationAdapter,
    ) -> OperationOutcome:
        values = _request_values(request)
        intent = await self._operations.begin(**values)
        if intent.status is OperationStatus.SUCCEEDED:
            return intent.to_outcome()
        if not intent.is_new:
            return await self._reconcile_intent(intent, adapter)

        try:
            outcome = await adapter.invoke(intent)
        except asyncio.CancelledError:
            # The committed pending intent is intentionally left unresolved.
            raise
        except Exception as error:
            await self._operations.fail(intent.id, error=str(error), needs_reconciliation=True)
            raise
        return await self._store_outcome(intent.id, outcome)

    async def _reconcile_intent(
        self, intent: OperationIntent, adapter: OperationAdapter
    ) -> OperationOutcome:
        if intent.status is OperationStatus.FAILED:
            raise RecoveryError(f"operation intent {intent.id} is terminally failed")
        return await self._reconcile_and_store(intent, adapter)

    async def _reconcile_and_store(
        self, intent: OperationIntent, adapter: OperationAdapter
    ) -> OperationOutcome:
        outcome = await adapter.reconcile(intent)
        return await self._store_outcome(intent.id, outcome)

    async def _store_outcome(self, intent_id: UUID, outcome: OperationOutcome) -> OperationOutcome:
        if outcome.status is OperationStatus.SUCCEEDED:
            await self._operations.complete(intent_id, outcome)
            return outcome
        await self._operations.fail(
            intent_id,
            error=outcome.error or "operation adapter returned a non-success outcome",
            needs_reconciliation=outcome.status is OperationStatus.NEEDS_RECONCILIATION,
        )
        return outcome


class RecoveryService:
    """Reconcile unresolved intents without ever invoking their side effect."""

    def __init__(self, operations: OperationRepository) -> None:
        self._operations = operations

    async def reconcile(self, intent_id: UUID, adapter: OperationAdapter) -> OperationIntent:
        intent = await self._operations.get(intent_id)
        if intent.status not in {
            OperationStatus.PENDING,
            OperationStatus.NEEDS_RECONCILIATION,
        }:
            return intent
        outcome = await adapter.reconcile(intent)
        if outcome.status is OperationStatus.SUCCEEDED:
            return await self._operations.complete(intent.id, outcome)
        return await self._operations.fail(
            intent.id,
            error=outcome.error or "operation could not be reconciled",
            needs_reconciliation=True,
        )

    async def reconcile_all(
        self, adapters: Mapping[str, OperationAdapter]
    ) -> tuple[OperationIntent, ...]:
        """Reconcile all unresolved intents through explicitly registered adapters."""

        intents = await self._operations.list_unresolved()
        results: list[OperationIntent] = []
        for intent in intents:
            adapter = adapters.get(intent.kind)
            if adapter is None:
                raise RecoveryError(f"no adapter registered for {intent.kind!r}")
            results.append(await self.reconcile(intent.id, adapter))
        return tuple(results)


def _request_values(
    request: OperationRequest | Mapping[str, object],
) -> dict[str, Any]:
    if isinstance(request, OperationRequest):
        return {
            "run_id": request.run_id,
            "operation_type": request.kind,
            "idempotency_key": request.idempotency_key,
            "request_digest": request.request_digest,
            "request_payload": request.request_payload,
            "request_schema_version": request.request_schema_version,
        }
    values = dict(request)
    if "operation_type" not in values and "kind" in values:
        values["operation_type"] = values.pop("kind")
    return values


__all__ = ["OperationExecutor", "RecoveryError", "RecoveryService"]
