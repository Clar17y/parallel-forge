"""PostgreSQL append-only causal event repository."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.event import RunEvent, thaw_payload
from forge.observability.context import current_context
from forge.observability.redaction import Redactor
from forge.persistence.models import Run
from forge.persistence.models import RunEvent as RunEventRecord
from forge.persistence.repositories.runs import (
    PersistenceDataError,
    RunNotFound,
)


class InvalidEventCursor(ValueError):
    """A Last-Event-ID cursor is outside the supported nonnegative range."""


class PostgresEventRepository:
    """Persist immutable events using a safe row-lock sequence allocator."""

    def __init__(self, session: AsyncSession, *, redactor: Redactor | None = None) -> None:
        self._session = session
        self._redactor = redactor or Redactor()

    async def append(self, event: RunEvent) -> RunEvent:
        """Append one event and allocate its next sequence under the run lock."""

        payload = thaw_payload(event.payload)
        context = current_context()
        if context.run_id is not None and context.run_id != event.run_id:
            raise PersistenceDataError("event run does not match the active correlation context")
        correlation = context.to_dict()
        if correlation:
            caller_correlation = payload.get("correlation")
            merged_correlation = (
                dict(caller_correlation) if isinstance(caller_correlation, Mapping) else {}
            )
            merged_correlation.update(correlation)
            payload["correlation"] = merged_correlation
        redacted_payload = self._redactor.redact(payload)
        if not isinstance(redacted_payload, Mapping):
            raise PersistenceDataError("redacted event payload is not an object")

        lock_result = await self._session.execute(
            select(Run.id).where(Run.id == event.run_id).with_for_update()
        )
        if lock_result.scalar_one_or_none() is None:
            raise RunNotFound(event.run_id)

        max_result = await self._session.execute(
            select(func.coalesce(func.max(RunEventRecord.sequence), 0)).where(
                RunEventRecord.run_id == event.run_id
            )
        )
        next_sequence = int(max_result.scalar_one()) + 1
        if event.sequence is not None and event.sequence != next_sequence:
            raise PersistenceDataError(
                f"event sequence {event.sequence} is not the next sequence {next_sequence}"
            )
        stored = replace(event, sequence=next_sequence, payload=redacted_payload)
        record = RunEventRecord(
            id=stored.event_id,
            sequence=next_sequence,
            run_id=stored.run_id,
            run_version=stored.run_version,
            event_type=stored.event_type,
            actor_class=stored.actor_class,
            actor_id=stored.actor_id,
            payload_schema_version=stored.payload_schema_version,
            payload=thaw_payload(stored.payload),
            occurred_at=stored.occurred_at,
        )
        self._session.add(record)
        await self._session.flush()
        return stored

    async def list_after(self, run_id: UUID, sequence: int) -> list[RunEvent]:
        """List events strictly after a nonnegative cursor in sequence order."""

        if type(sequence) is not int or sequence < 0:
            raise InvalidEventCursor("event cursor must be a nonnegative integer")
        result = await self._session.execute(
            select(RunEventRecord)
            .where(
                RunEventRecord.run_id == run_id,
                RunEventRecord.sequence > sequence,
            )
            .order_by(RunEventRecord.sequence.asc())
        )
        records = result.scalars().all()
        return [_event_from_record(record) for record in records]


def _event_from_record(record: RunEventRecord) -> RunEvent:
    if not isinstance(record.payload, Mapping):
        raise PersistenceDataError("event payload is not an object")
    if type(record.sequence) is not int or record.sequence < 1:
        raise PersistenceDataError("event sequence is not positive")
    if type(record.payload_schema_version) is not int or record.payload_schema_version < 1:
        raise PersistenceDataError("event payload schema version is not positive")
    try:
        return RunEvent(
            event_id=record.id,
            run_id=record.run_id,
            sequence=record.sequence,
            run_version=record.run_version,
            event_type=record.event_type,
            actor_class=record.actor_class,
            actor_id=record.actor_id,
            payload=record.payload,
            payload_schema_version=record.payload_schema_version,
            occurred_at=record.occurred_at,
        )
    except (TypeError, ValueError) as error:
        raise PersistenceDataError("persisted event is malformed") from error


__all__ = ["InvalidEventCursor", "PostgresEventRepository"]
