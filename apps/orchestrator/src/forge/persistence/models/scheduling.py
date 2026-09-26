"""Scheduling rows kept separate from legacy command leases."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionScheduledTask(Base, TimestampMixin):
    __tablename__ = "subscription_scheduled_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "task_id", name="uq_scheduled_task"),
        CheckConstraint(
            "state IN ('queued','leased','blocked','reconciling','terminal')",
            name="scheduled_task_state",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    parent_task_id: Mapped[UUID | None] = mapped_column(Uuid)
    worktree_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(96), nullable=False)
    owned_paths: Mapped[list[str]] = mapped_column(
        ARRAY(String(1024)), nullable=False, default=list
    )
    dependency_task_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_repairs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repairs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SubscriptionSchedulerRun(Base):
    __tablename__ = "subscription_scheduler_runs"
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    admitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    candidate_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    capacity_policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_run_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriptionSchedulerCapacityPolicy(Base, TimestampMixin):
    __tablename__ = "subscription_scheduler_capacity_policies"
    __table_args__ = (
        CheckConstraint(
            "global_limit > 0 AND run_limit > 0 AND provider_limit > 0",
            # Preserve the PostgreSQL identifier from migration 0009.
            name=conv("ck_subscription_scheduler_capacity_policies_scheduler_c_ef14"),
        ),
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    global_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    run_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_limit: Mapped[int] = mapped_column(Integer, nullable=False)


class SubscriptionScheduledEffect(Base, TimestampMixin):
    __tablename__ = "subscription_scheduled_effects"
    __table_args__ = (
        CheckConstraint(
            "state IN ('admitted','reconciling','settled','rejected')",
            name="scheduled_effect_state",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String(255), nullable=False)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    whole_worktree_exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="admitted")
