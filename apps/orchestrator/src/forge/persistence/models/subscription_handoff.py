"""Short-lived durable exclusion for a stopped handoff's snapshot observation."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class SubscriptionHandoffFence(Base):
    __tablename__ = "subscription_handoff_fences"

    worktree_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
