"""Immutable subscription plan proposal bindings."""

from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionPlanGate(Base, TimestampMixin):
    """One settled subscription attempt can propose exactly one approval snapshot."""

    __tablename__ = "subscription_plan_gates"
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscription_attempts.id", ondelete="RESTRICT"), primary_key=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscription_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    route_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
