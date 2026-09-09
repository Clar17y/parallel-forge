"""Durable startup recovery admission barrier."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base

RECOVERY_BARRIER_ID = UUID("00000000-0000-0000-0000-000000000001")


class RecoveryBarrier(Base):
    __tablename__ = "recovery_barrier"
    __table_args__ = (
        CheckConstraint("id = '00000000-0000-0000-0000-000000000001'::uuid", name="singleton"),
        CheckConstraint("generation >= 0", name="generation"),
        CheckConstraint("(owner_id IS NULL) = (expires_at IS NULL)", name="owner_pair"),
        CheckConstraint("owner_id IS NULL OR required", name="owned_required"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    owner_id: Mapped[UUID | None] = mapped_column(Uuid)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
