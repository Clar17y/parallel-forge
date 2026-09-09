"""Append-only operator audit application contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OperatorAuditRecord:
    """Redacted operator action evidence."""

    id: UUID
    actor_id: UUID
    event_type: str
    subject_type: str
    subject_id: UUID
    correlation_id: UUID
    schema_version: int
    payload: dict[str, object]
    created_at: datetime | None = None


class AuditRepository(Protocol):
    """Append and query immutable operator audit events."""

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
    ) -> OperatorAuditRecord: ...

    async def list_for_subject(
        self, *, subject_type: str, subject_id: UUID
    ) -> Sequence[OperatorAuditRecord]: ...


__all__ = ["AuditRepository", "OperatorAuditRecord"]
