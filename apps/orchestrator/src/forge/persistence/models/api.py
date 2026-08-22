"""Durable API idempotency receipts and operator audit records."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class ApiMutation(Base, TimestampMixin):
    """One durable idempotency receipt for an authenticated API mutation."""

    __tablename__ = "api_mutations"
    __table_args__ = (
        UniqueConstraint(
            "actor_id", "action", "key_hash", name="uq_api_mutations_actor_action_key_hash"
        ),
        CheckConstraint("lifecycle_state IN ('RESERVED','COMPLETED')", name="lifecycle_state"),
        CheckConstraint(
            "char_length(key_hash) = 64 AND key_hash ~ '^[0-9a-f]{64}$'", name="key_hash"
        ),
        CheckConstraint(
            "char_length(request_digest) = 64 AND request_digest ~ '^[0-9a-f]{64}$'",
            name="request_digest",
        ),
        CheckConstraint(
            "(lifecycle_state = 'RESERVED' AND response_status IS NULL AND response_payload IS NULL "
            "AND resource_kind IS NULL AND resource_id IS NULL) OR "
            "(lifecycle_state = 'COMPLETED' AND response_status IS NOT NULL "
            "AND response_payload IS NOT NULL)",
            name="completion_shape",
        ),
        Index("ix_api_mutations_lifecycle_created_at", "lifecycle_state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(96), nullable=False)
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False, default="RESERVED")
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resource_kind: Mapped[str | None] = mapped_column(String(96))
    resource_id: Mapped[UUID | None] = mapped_column(Uuid)


class OperatorAuditEvent(Base):
    """Append-only, bounded operator action evidence."""

    __tablename__ = "operator_audit_events"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint("char_length(event_type) BETWEEN 1 AND 96", name="event_type_bounded"),
        CheckConstraint("char_length(subject_type) BETWEEN 1 AND 96", name="subject_type_bounded"),
        Index(
            "ix_operator_audit_events_subject_created_at",
            "subject_type",
            "subject_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(96), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, default=uuid4)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["ApiMutation", "OperatorAuditEvent"]
