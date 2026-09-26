"""Lifecycle of an audited operator stop; provider result bytes remain immutable."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionTaskStop(Base, TimestampMixin):
    __tablename__ = "subscription_task_stops"
    __table_args__ = (
        UniqueConstraint("task_id", "stop_task_version", name="uq_task_stop_version"),
        CheckConstraint(
            "state IN ('requested','paused','cancelled','resumed','superseded')",
            name="task_stop_state",
        ),
        CheckConstraint(
            "(state = 'superseded') = (superseding_receipt_id IS NOT NULL)",
            name="task_stop_superseded",
        ),
        CheckConstraint(
            "(settled_task_version IS NULL) = (settlement_payload IS NULL)",
            name="task_stop_settlement_version",
        ),
        CheckConstraint(
            "stop_task_version >= 1 AND lease_generation >= 1", name="task_stop_versions"
        ),
        CheckConstraint(
            "(settlement_payload IS NULL) = (settlement_digest IS NULL)",
            name="task_stop_proof_pair",
        ),
        CheckConstraint(
            "(state = 'superseded' AND resume_receipt_id IS NULL AND resumed_task_version IS NULL) OR "
            "(state = 'requested' AND settled_task_version IS NULL AND settlement_payload IS NULL "
            "AND resume_receipt_id IS NULL AND resumed_task_version IS NULL) OR "
            "(state IN ('paused','cancelled') AND settled_task_version IS NOT NULL AND settled_task_version >= stop_task_version "
            "AND settlement_payload IS NOT NULL AND resume_receipt_id IS NULL "
            "AND resumed_task_version IS NULL) OR "
            "(state = 'resumed' AND settled_task_version IS NOT NULL AND settled_task_version >= stop_task_version "
            "AND settlement_payload IS NOT NULL AND resume_receipt_id IS NOT NULL "
            "AND resumed_task_version IS NOT NULL AND resumed_task_version = settled_task_version + 1)",
            name="task_stop_lifecycle",
        ),
        Index("ix_task_stop_pending", "state", "id"),
        Index("ix_task_stop_attempt_version", "attempt_id", "stop_task_version"),
    )
    id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("api_mutations.id", ondelete="RESTRICT"), primary_key=True
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="CASCADE"), nullable=False
    )
    stop_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    settled_task_version: Mapped[int | None] = mapped_column(Integer)
    settlement_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    settlement_digest: Mapped[str | None] = mapped_column(String(64))
    resume_receipt_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("api_mutations.id", ondelete="RESTRICT"), unique=True
    )
    resumed_task_version: Mapped[int | None] = mapped_column(Integer)
    superseding_receipt_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("api_mutations.id", ondelete="RESTRICT"), unique=True
    )
