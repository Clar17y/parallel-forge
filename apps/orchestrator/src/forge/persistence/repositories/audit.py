"""PostgreSQL append-only operator audit repository."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.audit import OperatorAuditRecord
from forge.observability.redaction import Redactor
from forge.persistence.models import OperatorAuditEvent


class AuditRepositoryError(RuntimeError):
    """Base for safe operator audit persistence errors."""


class PostgresAuditRepository:
    """Persist only bounded, recursively redacted operator evidence."""

    def __init__(self, session: AsyncSession, *, redactor: Redactor | None = None) -> None:
        self._session = session
        self._redactor = redactor or Redactor()

    async def append(
        self,
        *,
        actor_id: UUID,
        event_type: str,
        subject_type: str,
        subject_id: UUID,
        payload: Mapping[str, object],
        correlation_id: UUID | None = None,
        schema_version: int = 1,
    ) -> OperatorAuditRecord:
        """Append one redacted event to the caller's transaction."""

        if not event_type or len(event_type) > 96 or not subject_type or len(subject_type) > 96:
            raise ValueError("audit event identity is invalid")
        if type(schema_version) is not int or schema_version < 1:
            raise ValueError("audit schema version must be positive")
        redacted = self._redactor.redact(dict(payload))
        if not isinstance(redacted, Mapping):
            raise AuditRepositoryError("audit payload must be an object")
        stored = OperatorAuditEvent(
            id=uuid4(),
            actor_id=actor_id,
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            correlation_id=correlation_id or uuid4(),
            schema_version=schema_version,
            payload=dict(redacted),
        )
        self._session.add(stored)
        await self._session.flush()
        return _audit_from_record(stored)

    async def list_for_subject(
        self, *, subject_type: str, subject_id: UUID
    ) -> Sequence[OperatorAuditRecord]:
        """List one subject's events in append order."""

        result = await self._session.execute(
            select(OperatorAuditEvent)
            .where(
                OperatorAuditEvent.subject_type == subject_type,
                OperatorAuditEvent.subject_id == subject_id,
            )
            .order_by(OperatorAuditEvent.created_at, OperatorAuditEvent.id)
        )
        return [_audit_from_record(event) for event in result.scalars().all()]


def _audit_from_record(record: OperatorAuditEvent) -> OperatorAuditRecord:
    if not isinstance(record.payload, Mapping):
        raise AuditRepositoryError("stored audit payload is malformed")
    return OperatorAuditRecord(
        id=record.id,
        actor_id=record.actor_id,
        event_type=record.event_type,
        subject_type=record.subject_type,
        subject_id=record.subject_id,
        correlation_id=record.correlation_id,
        schema_version=record.schema_version,
        payload=dict(record.payload),
        created_at=record.created_at,
    )


__all__ = ["AuditRepositoryError", "PostgresAuditRepository"]
