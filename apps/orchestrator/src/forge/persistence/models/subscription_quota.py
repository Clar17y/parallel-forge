"""Global quota evidence and exact per-attempt admission fences."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class SubscriptionQuotaPool(Base):
    __tablename__ = "subscription_quota_pools"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="quota_revision"),
        CheckConstraint(
            "retry_basis IS NULL OR retry_basis IN ('known_reset','probe_cooldown')",
            name="quota_retry_basis",
        ),
    )
    provider: Mapped[str] = mapped_column(String(96), primary_key=True)
    account: Mapped[str] = mapped_column(String(96), primary_key=True)
    pool: Mapped[str] = mapped_column(String(96), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(128))
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_basis: Mapped[str | None] = mapped_column(String(32))
    probe_attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriptionQuotaObservation(Base):
    __tablename__ = "subscription_quota_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ("provider", "account", "pool"),
            (
                "subscription_quota_pools.provider",
                "subscription_quota_pools.account",
                "subscription_quota_pools.pool",
            ),
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_attempt_id IS NULL) <> (actor_id IS NULL)", name="quota_observation_source"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    provider: Mapped[str] = mapped_column(String(96), nullable=False)
    account: Mapped[str] = mapped_column(String(96), nullable=False)
    pool: Mapped[str] = mapped_column(String(96), nullable=False)
    source_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_eligible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retry_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class SubscriptionQuotaAdmission(Base):
    __tablename__ = "subscription_quota_admissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("provider", "account", "pool"),
            (
                "subscription_quota_pools.provider",
                "subscription_quota_pools.account",
                "subscription_quota_pools.pool",
            ),
            ondelete="RESTRICT",
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(96), nullable=False)
    account: Mapped[str] = mapped_column(String(96), nullable=False)
    pool: Mapped[str] = mapped_column(String(96), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    probe: Mapped[bool] = mapped_column(Boolean, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
