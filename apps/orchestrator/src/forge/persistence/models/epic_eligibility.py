"""Immutable successful integration evidence for one frozen execution item."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class EpicCompletionHandoff(Base):
    __tablename__ = "epic_completion_handoffs"
    __table_args__ = (
        UniqueConstraint("execution_id", "item_id", name="uq_epic_completion_item"),
        UniqueConstraint("attempt_id", name="uq_epic_completion_attempt"),
        CheckConstraint("merge_sha ~ '^[0-9a-f]{40}$'", name="merge_sha"),
        CheckConstraint("verified_base_sha ~ '^[0-9a-f]{40}$'", name="verified_base_sha"),
        CheckConstraint("evidence_digest ~ '^[0-9a-f]{64}$'", name="evidence_digest"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epic_executions.id", ondelete="RESTRICT"), nullable=False
    )
    item_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epic_item_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    run_version: Mapped[int] = mapped_column(nullable=False)
    merge_intent_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("operation_intents.id", ondelete="RESTRICT"), nullable=False
    )
    merge_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    integration_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    verified_base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
