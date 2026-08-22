"""PostgreSQL repository for intent-before-effect operation recovery."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_payload,
    thaw_payload,
)
from forge.persistence.models import OperationIntent as OperationIntentRecord
from forge.persistence.repositories.runs import PersistenceDataError


class OperationError(RuntimeError):
    """Base class for operation-intent persistence errors."""


class OperationNotFound(OperationError):
    """The requested operation intent does not exist."""


class IdempotencyConflict(OperationError):
    """An idempotency key was reused for a different immutable request."""


class OperationStateConflict(OperationError):
    """A terminal operation outcome cannot be rewritten contradictorily."""


_ERROR_SECRET = re.compile(
    r"(?i)(password|secret|credential|token|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)


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
        now = _utc_now()
        candidate = OperationIntent(
            id=uuid4(),
            run_id=request.run_id,
            kind=request.kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
            status=OperationStatus.PENDING,
            remote_resource_id=None,
            attempt=0,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
            outcome=None,
            error=None,
            request_schema_version=request.request_schema_version,
            is_new=True,
        )
        async with self._session_factory() as session, session.begin():
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
                    attempt_count=0,
                    started_at=None,
                    completed_at=None,
                )
                .on_conflict_do_nothing(index_elements=[OperationIntentRecord.idempotency_key])
                .returning(OperationIntentRecord.id)
            )
            inserted_id = inserted.scalar_one_or_none()
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

    async def get(self, intent_id: UUID) -> OperationIntent:
        async with self._session_factory() as session:
            record = await session.get(OperationIntentRecord, intent_id)
            if record is None:
                raise OperationNotFound(f"operation intent {intent_id} was not found")
            return _intent_from_record(record)

    async def complete(self, intent_id: UUID, outcome: OperationOutcome) -> OperationIntent:
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
            now = _utc_now()
            record.status = "SUCCEEDED"
            record.remote_resource_id = outcome.remote_resource_id
            record.outcome_schema_version = outcome.outcome_schema_version
            record.outcome_payload = thaw_payload(outcome.payload)
            record.completed_at = now
            record.last_error = None
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
            now = _utc_now()
            record.status = "NEEDS_RECONCILIATION" if needs_reconciliation else "FAILED"
            record.last_error = bounded_error
            record.completed_at = None if needs_reconciliation else now
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
            is_new=is_new,
        )
    except (TypeError, ValueError) as error:
        raise PersistenceDataError("stored operation intent is malformed") from error


def _bounded_error(error: str) -> str:
    value = str(error)
    value = _ERROR_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return value[:1024]


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "IdempotencyConflict",
    "OperationError",
    "OperationNotFound",
    "OperationStateConflict",
    "PostgresOperationRepository",
]
