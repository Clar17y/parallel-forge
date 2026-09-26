"""Attempt ceilings and append-only observed consumption."""

from uuid import UUID

from sqlalchemy import ForeignKey, ForeignKeyConstraint, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionAttemptReservation(Base, TimestampMixin):
    __tablename__ = "subscription_attempt_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ("run_id", "task_id", "attempt_id"),
            (
                "subscription_attempts.run_id",
                "subscription_attempts.task_row_id",
                "subscription_attempts.id",
            ),
            ondelete="CASCADE",
            name="fk_subscription_attempt_reservation_lineage",
        ),
        UniqueConstraint(
            "run_id", "idempotency_key", name="uq_subscription_attempt_reservation_key"
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    budget_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class SubscriptionAttemptConsumption(Base, TimestampMixin):
    __tablename__ = "subscription_attempt_consumption"
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("subscription_attempt_reservations.attempt_id", ondelete="CASCADE"),
        primary_key=True,
    )
    telemetry_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    observed: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    charged: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    unknown_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    exceeded_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    policy_violations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    uncertain: Mapped[bool] = mapped_column(nullable=False)
