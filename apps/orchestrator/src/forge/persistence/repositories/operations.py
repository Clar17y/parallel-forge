"""PostgreSQL repository for intent-before-effect operation recovery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.event import RunEvent
from forge.domain.lease import validate_lease_seconds
from forge.domain.operation import (
    OperationExecutionClaim,
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_payload,
    thaw_payload,
)
from forge.domain.payload import redact_durable_text
from forge.persistence.models import OperationIntent as OperationIntentRecord
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.runs import PersistenceDataError, PostgresRunRepository


class OperationError(RuntimeError):
    """Base class for operation-intent persistence errors."""


class OperationNotFound(OperationError):
    """The requested operation intent does not exist."""


class IdempotencyConflict(OperationError):
    """An idempotency key was reused for a different immutable request."""


class OperationStateConflict(OperationError):
    """A terminal operation outcome cannot be rewritten contradictorily."""


class OperationLeaseError(OperationError):
    """The caller does not hold the current execution lease."""


class PostgresOperationRepository:
    """Commit operation intent/outcome rows in separate short transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def begin(
        self,
        *,
        run_id: UUID,
        operation_type: str | None = None,
        idempotency_key: str,
        request_digest: str,
        request_payload: Mapping[str, object],
        request_schema_version: int = 1,
        kind: str | None = None,
        operation_kind: str | None = None,
        execution_owner: str | None = None,
        execution_lease_seconds: float | None = None,
    ) -> OperationIntent:
        resolved_kind = operation_type or kind or operation_kind
        if resolved_kind is None:
            raise ValueError("operation type is required")
        request = OperationRequest(
            run_id=run_id,
            kind=resolved_kind,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            request_payload=request_payload,
            request_schema_version=request_schema_version,
        )
        _validate_execution_request(execution_owner, execution_lease_seconds)
        now = _utc_now()
        execution_lease_expires_at = (
            now + timedelta(seconds=execution_lease_seconds)
            if execution_owner is not None and execution_lease_seconds is not None
            else None
        )
        candidate = OperationIntent(
            id=uuid4(),
            run_id=request.run_id,
            kind=request.kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
            status=OperationStatus.PENDING,
            remote_resource_id=None,
            attempt=1 if execution_owner is not None else 0,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
            outcome=None,
            error=None,
            request_schema_version=request.request_schema_version,
            execution_owner=execution_owner,
            execution_lease_expires_at=execution_lease_expires_at,
            is_new=True,
        )
        async with self._session_factory() as session, session.begin():
            run = await PostgresRunRepository(session).get_for_update(request.run_id)
            inserted = await session.execute(
                insert(OperationIntentRecord)
                .values(
                    id=candidate.id,
                    run_id=candidate.run_id,
                    operation_kind=candidate.kind,
                    idempotency_key=candidate.idempotency_key,
                    request_digest=candidate.request_digest,
                    request_schema_version=candidate.request_schema_version,
                    request_payload=thaw_payload(candidate.request_payload),
                    status="PENDING",
                    attempt_count=candidate.attempt,
                    execution_owner=candidate.execution_owner,
                    execution_lease_expires_at=candidate.execution_lease_expires_at,
                    started_at=None,
                    completed_at=None,
                )
                .on_conflict_do_nothing(index_elements=[OperationIntentRecord.idempotency_key])
                .returning(OperationIntentRecord.id)
            )
            inserted_id = inserted.scalar_one_or_none()
            if inserted_id is not None:
                await PostgresEventRepository(session).append(
                    RunEvent(
                        run_id=request.run_id,
                        run_version=run.version,
                        event_type="operation.intent_created",
                        actor_class="system",
                        payload_schema_version=1,
                        occurred_at=now,
                        payload={
                            "operation_intent_id": str(candidate.id),
                            "operation_kind": candidate.kind,
                            "request_digest": candidate.request_digest,
                            "request_schema_version": candidate.request_schema_version,
                        },
                    )
                )
            record = await session.get(OperationIntentRecord, inserted_id or candidate.id)
            if record is None:
                record = (
                    await session.execute(
                        select(OperationIntentRecord).where(
                            OperationIntentRecord.idempotency_key == candidate.idempotency_key
                        )
                    )
                ).scalar_one_or_none()
            if record is None:
                raise OperationError("operation intent disappeared before it could be loaded")
            actual = _intent_from_record(record, is_new=inserted_id is not None)
            if inserted_id is None:
                _check_begin_match(candidate, actual)
            return actual

    async def claim_for_recovery(
        self, intent_id: UUID, *, owner_id: str, lease_seconds: float
    ) -> OperationExecutionClaim:
        """CAS-claim an unowned or expired unresolved intent for reconciliation."""

        _validate_execution_request(owner_id, lease_seconds)
        now = _utc_now()
        expiry = now + timedelta(seconds=lease_seconds)
        async with self._session_factory() as session, session.begin():
            record = await self._locked(intent_id, session)
            current = _intent_from_record(record)
            if current.status not in {
                OperationStatus.PENDING,
                OperationStatus.NEEDS_RECONCILIATION,
            }:
                return OperationExecutionClaim(intent=current, acquired=False)
            if (
                record.execution_owner is not None
                and record.execution_lease_expires_at is not None
                and record.execution_lease_expires_at > now
            ):
                return OperationExecutionClaim(intent=current, acquired=False)
            record.execution_owner = owner_id
            record.execution_lease_expires_at = expiry
            record.attempt_count += 1
            if record.started_at is None:
                record.started_at = now
            await session.flush()
            await session.refresh(record)
            return OperationExecutionClaim(intent=_intent_from_record(record), acquired=True)

    async def renew_execution(
        self, intent_id: UUID, *, owner_id: str, lease_seconds: float
    ) -> OperationIntent:
        """Extend an active execution lease only for its current owner."""

        _validate_execution_request(owner_id, lease_seconds)
        now = _utc_now()
        async with self._session_factory() as session, session.begin():
            record = await self._locked(intent_id, session)
            _require_execution_owner(record, owner_id, now)
            record.execution_lease_expires_at = now + timedelta(seconds=lease_seconds)
            await session.flush()
            await session.refresh(record)
            return _intent_from_record(record)

    async def get(self, intent_id: UUID) -> OperationIntent:
        async with self._session_factory() as session:
            record = await session.get(OperationIntentRecord, intent_id)
            if record is None:
                raise OperationNotFound(f"operation intent {intent_id} was not found")
            return _intent_from_record(record)

    async def complete(
        self, intent_id: UUID, outcome: OperationOutcome, *, owner_id: str | None = None
    ) -> OperationIntent:
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise ValueError("complete requires a succeeded operation outcome")
        async with self._session_factory() as session, session.begin():
            record = await self._locked(intent_id, session)
            if record.status == "SUCCEEDED":
                existing = _intent_from_record(record)
                if existing.remote_resource_id != outcome.remote_resource_id or canonical_payload(
                    existing.outcome or {}
                ) != canonical_payload(outcome.payload):
                    raise OperationStateConflict(
                        f"operation intent {intent_id} already has a different outcome"
                    )
                return existing
            if record.status not in {"PENDING", "NEEDS_RECONCILIATION"}:
                raise OperationStateConflict(
                    f"operation intent {intent_id} is already terminal: {record.status}"
                )
            _require_execution_owner(record, owner_id, _utc_now())
            now = _utc_now()
            record.status = "SUCCEEDED"
            record.remote_resource_id = outcome.remote_resource_id
            record.outcome_schema_version = outcome.outcome_schema_version
            record.outcome_payload = thaw_payload(outcome.payload)
            record.completed_at = now
            record.last_error = None
            record.execution_owner = None
            record.execution_lease_expires_at = None
            if record.started_at is None:
                record.started_at = now
            await session.flush()
            await session.refresh(record)
            return _intent_from_record(record)

    async def fail(
        self,
        intent_id: UUID,
        *,
        error: str,
        needs_reconciliation: bool = False,
        owner_id: str | None = None,
    ) -> OperationIntent:
        bounded_error = _bounded_error(error)
        async with self._session_factory() as session, session.begin():
            record = await self._locked(intent_id, session)
            if record.status == "FAILED" and not needs_reconciliation:
                existing = _intent_from_record(record)
                if existing.error == bounded_error:
                    return existing
                raise OperationStateConflict(
                    f"operation intent {intent_id} already has a different failure"
                )
            if record.status == "SUCCEEDED":
                raise OperationStateConflict(f"operation intent {intent_id} already succeeded")
            _require_execution_owner(record, owner_id, _utc_now())
            now = _utc_now()
            record.status = "NEEDS_RECONCILIATION" if needs_reconciliation else "FAILED"
            record.last_error = bounded_error
            record.completed_at = None if needs_reconciliation else now
            record.outcome_payload = None
            record.outcome_schema_version = None
            record.remote_resource_id = None
            record.execution_owner = None
            record.execution_lease_expires_at = None
            if record.started_at is None:
                record.started_at = now
            await session.flush()
            await session.refresh(record)
            return _intent_from_record(record)

    async def list_unresolved(self) -> Sequence[OperationIntent]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(OperationIntentRecord)
                .where(
                    or_(
                        OperationIntentRecord.status == "PENDING",
                        OperationIntentRecord.status == "NEEDS_RECONCILIATION",
                    )
                )
                .order_by(
                    OperationIntentRecord.updated_at,
                    OperationIntentRecord.created_at,
                    OperationIntentRecord.id,
                )
            )
            return [_intent_from_record(record) for record in result.scalars().all()]

    async def _locked(self, intent_id: UUID, session: AsyncSession) -> OperationIntentRecord:
        record = (
            await session.execute(
                select(OperationIntentRecord)
                .where(OperationIntentRecord.id == intent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            raise OperationNotFound(f"operation intent {intent_id} was not found")
        return record


def _check_begin_match(expected: OperationIntent, actual: OperationIntent) -> None:
    if (
        expected.run_id != actual.run_id
        or expected.kind != actual.kind
        or expected.request_digest != actual.request_digest
        or expected.request_schema_version != actual.request_schema_version
        or canonical_payload(expected.request_payload) != canonical_payload(actual.request_payload)
    ):
        raise IdempotencyConflict(
            f"operation idempotency key {expected.idempotency_key!r} has a different request"
        )


def _intent_from_record(record: OperationIntentRecord, *, is_new: bool = False) -> OperationIntent:
    status = {
        "PENDING": OperationStatus.PENDING,
        "SUCCEEDED": OperationStatus.SUCCEEDED,
        "FAILED": OperationStatus.FAILED,
        "NEEDS_RECONCILIATION": OperationStatus.NEEDS_RECONCILIATION,
    }.get(record.status)
    if status is None:
        raise PersistenceDataError(f"unknown stored operation status {record.status!r}")
    if not isinstance(record.request_payload, Mapping):
        raise PersistenceDataError("stored operation request payload is not an object")
    if record.outcome_payload is not None and not isinstance(record.outcome_payload, Mapping):
        raise PersistenceDataError("stored operation outcome payload is not an object")
    try:
        return OperationIntent(
            id=record.id,
            run_id=record.run_id,
            kind=record.operation_kind,
            idempotency_key=record.idempotency_key,
            request_digest=record.request_digest,
            request_payload=record.request_payload,
            status=status,
            remote_resource_id=record.remote_resource_id,
            attempt=record.attempt_count,
            created_at=record.created_at,
            updated_at=record.updated_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            outcome=record.outcome_payload,
            error=record.last_error,
            request_schema_version=record.request_schema_version,
            outcome_schema_version=record.outcome_schema_version,
            execution_owner=record.execution_owner,
            execution_lease_expires_at=record.execution_lease_expires_at,
            is_new=is_new,
        )
    except (TypeError, ValueError) as error:
        raise PersistenceDataError("stored operation intent is malformed") from error


def _bounded_error(error: str) -> str:
    value = str(error)
    return redact_durable_text(value)[:1024]


def _validate_execution_request(owner_id: str | None, lease_seconds: float | None) -> None:
    if owner_id is None:
        if lease_seconds is not None:
            raise ValueError("an execution lease duration requires an owner")
        return
    if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 255:
        raise ValueError("operation execution owner must contain 1-255 characters")
    if lease_seconds is None:
        raise ValueError("an execution owner requires a lease duration")
    validate_lease_seconds(lease_seconds)


def _require_execution_owner(
    record: OperationIntentRecord, owner_id: str | None, now: datetime
) -> None:
    if (
        owner_id is None
        or record.execution_owner != owner_id
        or record.execution_lease_expires_at is None
        or record.execution_lease_expires_at <= now
    ):
        raise OperationLeaseError(f"caller does not hold operation intent {record.id}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "IdempotencyConflict",
    "OperationError",
    "OperationLeaseError",
    "OperationNotFound",
    "OperationStateConflict",
    "PostgresOperationRepository",
]
