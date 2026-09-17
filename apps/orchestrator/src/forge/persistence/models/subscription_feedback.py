"""Durable operator feedback awaiting primary forwarding and worker delivery."""

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionTaskFeedback(Base, TimestampMixin):
    __tablename__ = "subscription_task_feedback"
    __table_args__ = (
        ForeignKeyConstraint(
            ("run_id", "primary_task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_feedback_primary",
        ),
        ForeignKeyConstraint(
            ("run_id", "task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_feedback_target",
        ),
        CheckConstraint(
            "state IN ('pending_primary','forwarded','delivered','closed')",
            name="subscription_feedback_state",
        ),
        CheckConstraint(
            "observed_run_version >= 0 AND observed_task_version >= 0 "
            "AND observed_primary_version >= 0",
            name="subscription_feedback_versions",
        ),
        CheckConstraint(
            "feedback_bytes BETWEEN 1 AND 4096 AND feedback_bytes = octet_length(feedback)",
            name="subscription_feedback_size",
        ),
        CheckConstraint(
            "feedback_digest ~ '^[0-9a-f]{64}$' "
            "AND request_digest ~ '^[0-9a-f]{64}$' "
            "AND observed_task_digest ~ '^[0-9a-f]{64}$' "
            "AND observed_primary_digest ~ '^[0-9a-f]{64}$' "
            "AND envelope_digest ~ '^[0-9a-f]{64}$' "
            "AND (application_digest IS NULL OR application_digest ~ '^[0-9a-f]{64}$')",
            name="subscription_feedback_digests",
        ),
        CheckConstraint("primary_task_id <> task_id", name="feedback_distinct_tasks"),
        CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('accepted','cancelled','budget_exhausted')",
            name="feedback_closed_reason",
        ),
        CheckConstraint(
            "(state = 'pending_primary' AND application_digest IS NULL "
            "AND delivery_attempt_id IS NULL AND delivered_at IS NULL AND closed_reason IS NULL) OR "
            "(state = 'forwarded' AND primary_attempt_id IS NOT NULL "
            "AND application_digest IS NOT NULL AND delivered_at IS NULL "
            "AND closed_reason IS NULL) OR "
            "(state = 'delivered' AND primary_attempt_id IS NOT NULL "
            "AND application_digest IS NOT NULL AND delivery_attempt_id IS NOT NULL "
            "AND delivered_at IS NOT NULL AND closed_reason IS NULL) OR "
            "(state = 'closed' "
            "AND (primary_attempt_id IS NOT NULL "
            "OR closed_reason IN ('cancelled','budget_exhausted')) "
            "AND application_digest IS NOT NULL AND delivery_attempt_id IS NULL "
            "AND delivered_at IS NULL AND closed_reason IS NOT NULL)",
            name="subscription_feedback_lifecycle",
        ),
        Index("ix_subscription_feedback_primary_state", "run_id", "primary_task_id", "state"),
        Index("ix_subscription_feedback_target_state", "run_id", "task_id", "state"),
        Index(
            "uq_subscription_feedback_pending_primary",
            "run_id",
            "primary_task_id",
            unique=True,
            postgresql_where=text("state = 'pending_primary'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("api_mutations.id", ondelete="RESTRICT"), primary_key=True
    )
    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    primary_task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    observed_run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_primary_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_task_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_primary_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_primary")
    primary_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="RESTRICT")
    )
    delivery_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="RESTRICT")
    )
    application_digest: Mapped[str | None] = mapped_column(String(64))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(String(32))


__all__ = ["SubscriptionTaskFeedback"]
