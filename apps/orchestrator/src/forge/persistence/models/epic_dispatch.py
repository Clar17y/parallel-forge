"""Owner-enabled, versioned automatic dispatch authority for one frozen epoch."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class EpicDispatchSetting(Base):
    __tablename__ = "epic_dispatch_settings"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("(profile_id IS NULL) = (profile_version IS NULL)", name="profile_pair"),
        CheckConstraint("profile_version IS NULL OR profile_version >= 1", name="profile_version_positive"),
        CheckConstraint("(claim_item_id IS NULL) = (claim_token IS NULL)", name="claim_pair"),
        CheckConstraint("(claim_item_id IS NULL) = (claim_expires_at IS NULL)", name="claim_expiry_pair"),
        Index("ix_epic_dispatch_scan", "enabled", "checked_at", "execution_id"),
    )

    execution_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("epic_executions.id", ondelete="RESTRICT"), primary_key=True)
    epic_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("epics.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    session_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    profile_id: Mapped[UUID | None] = mapped_column(Uuid)
    profile_version: Mapped[int | None] = mapped_column(Integer)
    claim_item_id: Mapped[UUID | None] = mapped_column(Uuid)
    claim_token: Mapped[UUID | None] = mapped_column(Uuid)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocker_code: Mapped[str | None] = mapped_column(String(96))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
