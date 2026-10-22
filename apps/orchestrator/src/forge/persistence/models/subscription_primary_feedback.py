"""Durable operator feedback addressed directly to the root primary task."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionPrimaryFeedback(Base, TimestampMixin):
    __tablename__ = "subscription_primary_feedback"
    __table_args__ = (
        ForeignKeyConstraint(
            ("run_id", "primary_task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_primary_feedback_primary",
        ),
        CheckConstraint(
            "state IN ('pending','delivered','closed')",
            name="subscription_primary_feedback_state",
        ),
        CheckConstraint(
            "observed_run_version >= 0 AND observed_task_version >= 0",
            name="subscription_primary_feedback_versions",
        ),
        CheckConstraint(
            "feedback_bytes BETWEEN 1 AND 4096 AND feedback_bytes = octet_length(feedback)",
            name="subscription_primary_feedback_size",
        ),
        CheckConstraint(
            "feedback_digest ~ '^[0-9a-f]{64}$' "
            "AND request_digest ~ '^[0-9a-f]{64}$' "
            "AND observed_task_digest ~ '^[0-9a-f]{64}$' "
            "AND envelope_digest ~ '^[0-9a-f]{64}$' "
            "AND (application_digest IS NULL OR application_digest ~ '^[0-9a-f]{64}$')",
            name="subscription_primary_feedback_digests",
        ),
        CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('cancelled','budget_exhausted','run_terminal')",
            name="primary_feedback_closed_reason",
        ),
        # Pending rows may carry a delivery_attempt_id: that is a binding to
        # the attempt being built, settled to delivered only on launch proof
        # (mirrors worker feedback). Delivered requires the settled binding.
        CheckConstraint(
            "(state = 'pending' AND delivered_at IS NULL "
            "AND closed_reason IS NULL AND application_digest IS NULL) OR "
            "(state = 'delivered' AND delivery_attempt_id IS NOT NULL "
            "AND delivered_at IS NOT NULL AND closed_reason IS NULL) OR "
            "(state = 'closed' AND delivery_attempt_id IS NULL AND delivered_at IS NULL "
            "AND closed_reason IS NOT NULL AND application_digest IS NOT NULL)",
            name="subscription_primary_feedback_lifecycle",
        ),
        Index("ix_subscription_primary_feedback_primary_state", "run_id", "primary_task_id", "state"),
        Index("ix_subscription_primary_feedback_delivery", "delivery_attempt_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("api_mutations.id", ondelete="RESTRICT"), primary_key=True
    )
    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    primary_task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    observed_run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_task_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    delivery_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="RESTRICT")
    )
    application_digest: Mapped[str | None] = mapped_column(String(64))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(String(32))


__all__ = ["SubscriptionPrimaryFeedback"]
