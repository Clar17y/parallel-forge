"""Immutable provider result identity and scheduling repair debits."""

from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionAttemptResult(Base, TimestampMixin):
    __tablename__ = "subscription_attempt_results"
    __table_args__ = (
        CheckConstraint(
            "(application_digest IS NULL) = (application_payload IS NULL)",
            name="application_receipt_pair",
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="CASCADE"), primary_key=True
    )
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    application_digest: Mapped[str | None] = mapped_column(String(64))
    application_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)


class SubscriptionRepairDebit(Base, TimestampMixin):
    __tablename__ = "subscription_repair_debits"
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="CASCADE"), primary_key=True
    )
    next_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="RESTRICT"), unique=True
    )
