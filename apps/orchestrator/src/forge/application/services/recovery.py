"""Intent-before-effect execution and restart reconciliation services."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from forge.application.ports.operations import OperationAdapter, OperationRepository
from forge.domain.lease import validate_lease_seconds
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

    def __init__(
        self, operations: OperationRepository, *, execution_lease_seconds: float = 30
    ) -> None:
        self._operations = operations
        validate_lease_seconds(execution_lease_seconds)
        self._execution_lease_seconds = execution_lease_seconds
        self._poll_interval = min(execution_lease_seconds / 3, 0.1)

    async def execute(
        self,
        request: OperationRequest | Mapping[str, object],
        adapter: OperationAdapter,
    ) -> OperationOutcome:
        values = _request_values(request)
        owner_id = f"forge-operation-{uuid4().hex}"
        intent = await self._operations.begin(
            **values,
            execution_owner=owner_id,
            execution_lease_seconds=self._execution_lease_seconds,
        )
        if intent.status is OperationStatus.SUCCEEDED:
            return intent.to_outcome()
        if not intent.is_new:
            return await self._observe_or_reconcile(intent, adapter, owner_id)

        return await self._invoke_owned(intent, adapter, owner_id)

    async def _invoke_owned(
        self, intent: OperationIntent, adapter: OperationAdapter, owner_id: str
    ) -> OperationOutcome:
        renewal = asyncio.create_task(self._renew_until_done(intent.id, owner_id))

        try:
            outcome = await adapter.invoke(intent)
        except asyncio.CancelledError:
            # The committed intent and its owner lease remain for expiry/recovery.
            raise
        except Exception as error:
            with suppress(Exception):
                await self._operations.fail(
                    intent.id,
                    error=str(error),
                    needs_reconciliation=True,
                    owner_id=owner_id,
                )
            raise
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        return await self._store_outcome(intent.id, outcome, owner_id)

    async def _observe_or_reconcile(
        self, intent: OperationIntent, adapter: OperationAdapter, owner_id: str
    ) -> OperationOutcome:
        """Wait for an active owner, then reconcile only after its lease expires."""

        while True:
            current = await self._operations.get(intent.id)
            if current.status is OperationStatus.SUCCEEDED:
                return current.to_outcome()
            if current.status is OperationStatus.FAILED:
                raise RecoveryError(f"operation intent {intent.id} is terminally failed")
            if (
                current.execution_owner is not None
                and current.execution_lease_expires_at is not None
                and current.execution_lease_expires_at > _utc_now()
            ):
                await asyncio.sleep(self._poll_interval)
                continue
            claim = await self._operations.claim_for_recovery(
                current.id,
                owner_id=owner_id,
                lease_seconds=self._execution_lease_seconds,
            )
            if not claim.acquired:
                await asyncio.sleep(self._poll_interval)
                continue
            return await self._reconcile_owned(claim.intent, adapter, owner_id)

    async def _reconcile_owned(
        self, intent: OperationIntent, adapter: OperationAdapter, owner_id: str
    ) -> OperationOutcome:
        renewal = asyncio.create_task(self._renew_until_done(intent.id, owner_id))
        try:
            try:
                outcome = await adapter.reconcile(intent)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with suppress(Exception):
                    await self._operations.fail(
                        intent.id,
                        error=str(error),
                        needs_reconciliation=True,
                        owner_id=owner_id,
                    )
                raise
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        return await self._store_outcome(intent.id, outcome, owner_id)

    async def _store_outcome(
        self, intent_id: UUID, outcome: OperationOutcome, owner_id: str
    ) -> OperationOutcome:
        if outcome.status is OperationStatus.SUCCEEDED:
            await self._operations.complete(intent_id, outcome, owner_id=owner_id)
            return outcome
        await self._operations.fail(
            intent_id,
            error=outcome.error or "operation adapter returned a non-success outcome",
            needs_reconciliation=outcome.status is OperationStatus.NEEDS_RECONCILIATION,
            owner_id=owner_id,
        )
        return outcome

    async def _renew_until_done(self, intent_id: UUID, owner_id: str) -> None:
        delay = self._execution_lease_seconds / 3
        while True:
            await asyncio.sleep(delay)
            try:
                await self._operations.renew_execution(
                    intent_id,
                    owner_id=owner_id,
                    lease_seconds=self._execution_lease_seconds,
                )
            except Exception:  # noqa: BLE001 - lease loss stops renewal safely
                return


class RecoveryService:
    """Reconcile unresolved intents without ever invoking their side effect."""

    def __init__(
        self, operations: OperationRepository, *, execution_lease_seconds: float = 30
    ) -> None:
        self._operations = operations
        validate_lease_seconds(execution_lease_seconds)
        self._execution_lease_seconds = execution_lease_seconds

    async def reconcile(self, intent_id: UUID, adapter: OperationAdapter) -> OperationIntent:
        owner_id = f"forge-recovery-{uuid4().hex}"
        intent = await self._operations.get(intent_id)
        if intent.status not in {
            OperationStatus.PENDING,
            OperationStatus.NEEDS_RECONCILIATION,
        }:
            return intent
        claim = await self._operations.claim_for_recovery(
            intent.id,
            owner_id=owner_id,
            lease_seconds=self._execution_lease_seconds,
        )
        if not claim.acquired:
            return claim.intent
        try:
            outcome = await adapter.reconcile(claim.intent)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - adapter failures become durable outcomes
            return await self._operations.fail(
                claim.intent.id,
                error=str(error),
                needs_reconciliation=True,
                owner_id=owner_id,
            )
        if outcome.status is OperationStatus.SUCCEEDED:
            return await self._operations.complete(claim.intent.id, outcome, owner_id=owner_id)
        return await self._operations.fail(
            claim.intent.id,
            error=outcome.error or "operation could not be reconciled",
            needs_reconciliation=True,
            owner_id=owner_id,
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
    values.pop("execution_owner", None)
    values.pop("execution_lease_seconds", None)
    return values


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["OperationExecutor", "RecoveryError", "RecoveryService"]
