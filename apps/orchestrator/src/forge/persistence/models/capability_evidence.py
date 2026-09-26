"""Durable current/superseded official-client capability evidence."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from forge.persistence.models.base import Base


class CapabilityEvidence(Base):
    __tablename__ = "capability_evidence"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="capability_evidence_revision_positive"),
        CheckConstraint(
            "identity_digest ~ '^[0-9a-f]{64}$'", name="capability_evidence_identity_digest"
        ),
        CheckConstraint("expires_at > observed_at", name="capability_evidence_validity_window"),
        CheckConstraint(
            "invalidated_at IS NULL OR invalidated_at >= observed_at",
            name="capability_evidence_invalidation_order",
        ),
        Index(
            "uq_capability_evidence_active_identity",
            "identity_digest",
            unique=True,
            postgresql_where=text("invalidated_at IS NULL"),
        ),
        Index(
            "uq_capability_evidence_identity_revision",
            "identity_digest",
            "revision",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("artifacts.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["CapabilityEvidence"]
