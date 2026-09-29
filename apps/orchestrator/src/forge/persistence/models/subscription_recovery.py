"""Additive contract provenance, result diagnostics and operator receipts."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionContractRevision(Base, TimestampMixin):
    __tablename__ = "subscription_contract_revisions"
    __table_args__ = (
        UniqueConstraint("task_id", "revision", name="uq_subscription_contract_revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    approval_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=False
    )
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    original_contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    original_contract_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class SubscriptionApplicationDiagnostic(Base, TimestampMixin):
    __tablename__ = "subscription_application_diagnostics"

    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="CASCADE"), primary_key=True
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_tasks.id", ondelete="CASCADE"), nullable=False
    )
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    resolution: Mapped[str] = mapped_column(String(32), nullable=False)
    failed_applications: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_failure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriptionRecoveryReceipt(Base, TimestampMixin):
    __tablename__ = "subscription_recovery_receipts"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "actor_id", "idempotency_key", name="uq_subscription_recovery_idempotency"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)


class SubscriptionRecoverySigningKey(Base):
    """Database-held shared signing key; preview reads remain side-effect free."""

    __tablename__ = "subscription_recovery_signing_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_hex: Mapped[str] = mapped_column(String(64), nullable=False)


class SubscriptionRecoveryWorker(Base):
    __tablename__ = "subscription_recovery_workers"

    worker_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
