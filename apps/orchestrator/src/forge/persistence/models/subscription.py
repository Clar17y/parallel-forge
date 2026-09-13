"""Additive v0.2 subscription runtime tables."""

from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class SubscriptionProfileVersion(Base, TimestampMixin):
    __tablename__ = "subscription_profile_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version", name="uq_subscription_profile_version"),
        CheckConstraint("version >= 1", name="version_positive"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class ProjectSubscriptionProfile(Base, TimestampMixin):
    __tablename__ = "project_subscription_profiles"
    __table_args__ = (
        ForeignKeyConstraint(
            ("profile_id", "profile_version"),
            ("subscription_profile_versions.profile_id", "subscription_profile_versions.version"),
            name="fk_project_subscription_profile_version",
            ondelete="RESTRICT",
        ),
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)


class SubscriptionEnvelope(Base, TimestampMixin):
    __tablename__ = "subscription_envelopes"
    __table_args__ = (
        ForeignKeyConstraint(
            ("profile_id", "profile_version"),
            ("subscription_profile_versions.profile_id", "subscription_profile_versions.version"),
            name="fk_subscription_envelope_profile_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("safety_policy_version >= 1", name="safety_policy_positive"),
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    safety_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class SubscriptionTask(Base, TimestampMixin):
    __tablename__ = "subscription_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_subscription_task_run_id"),
        UniqueConstraint("run_id", "task_id", name="uq_subscription_task_run_task"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_subscription_task_idempotency"),
        ForeignKeyConstraint(
            ("run_id", "parent_task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="RESTRICT",
            name="fk_subscription_task_parent",
        ),
        CheckConstraint(
            "state IN ('queued','running','blocked','reconciling','terminal')", name="state"
        ),
        CheckConstraint("version >= 0", name="version_nonnegative"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    parent_task_id: Mapped[UUID | None] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    pause_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class SubscriptionTaskDependency(Base):
    __tablename__ = "subscription_task_dependencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ("run_id", "task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_task_dep_task",
        ),
        ForeignKeyConstraint(
            ("run_id", "dependency_task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="RESTRICT",
            name="fk_subscription_task_dep_dependency",
        ),
    )
    run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    dependency_task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)


class SubscriptionAttempt(Base, TimestampMixin):
    __tablename__ = "subscription_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ("run_id", "task_row_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_attempt_task",
        ),
        UniqueConstraint("run_id", "task_row_id", "id", name="uq_subscription_attempt_run_task_id"),
        UniqueConstraint("task_row_id", "attempt_number", name="uq_subscription_attempt_number"),
        UniqueConstraint(
            "task_row_id", "idempotency_key", name="uq_subscription_attempt_idempotency"
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_generation IS NULL AND candidate_epoch IS NULL AND envelope_digest IS NULL AND task_digest IS NULL) OR (lease_owner IS NOT NULL AND lease_generation IS NOT NULL AND lease_generation > 0 AND candidate_epoch IS NOT NULL AND candidate_epoch >= 0 AND envelope_digest IS NOT NULL AND task_digest IS NOT NULL)",
            name="execution_binding",
        ),
        CheckConstraint(
            "status IN ('queued','running','blocked','reconciling','terminal')", name="status"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    task_row_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    route_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    telemetry_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_generation: Mapped[int | None] = mapped_column(Integer)
    task_version: Mapped[int | None] = mapped_column(Integer)
    candidate_epoch: Mapped[int | None] = mapped_column(Integer)
    envelope_digest: Mapped[str | None] = mapped_column(String(64))
    task_digest: Mapped[str | None] = mapped_column(String(64))


class SubscriptionOperationBinding(Base):
    __tablename__ = "subscription_operation_bindings"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "provider_call_key", name="uq_subscription_operation_provider_key"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("subscription_attempts.id", ondelete="CASCADE"), nullable=False
    )
    provider_call_key: Mapped[str] = mapped_column(String(255), nullable=False)
    durable_operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    receipt_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)


class SubscriptionClientLaunch(Base, TimestampMixin):
    """Credential-free durable lifecycle for one official-client launch."""

    __tablename__ = "subscription_client_launches"
    __table_args__ = (
        ForeignKeyConstraint(
            ("attempt_id",),
            ("subscription_attempts.id",),
            ondelete="CASCADE",
            name="fk_subscription_client_launch_attempt",
        ),
        UniqueConstraint("attempt_id", "launch_id", name="uq_subscription_client_launch"),
        CheckConstraint("state IN ('intent','started','terminal','uncertain')", name="state"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    launch_id: Mapped[str] = mapped_column(String(255), nullable=False)
    worker_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="intent")
    pid: Mapped[int | None] = mapped_column(Integer)
    process_start_token: Mapped[str | None] = mapped_column(String(255))
    terminal_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)


class SubscriptionBudgetPool(Base, TimestampMixin):
    __tablename__ = "subscription_budget_pools"
    __table_args__ = (
        ForeignKeyConstraint(
            ("run_id", "task_row_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_budget_pool_task",
        ),
        UniqueConstraint(
            "run_id",
            "task_row_id",
            postgresql_nulls_not_distinct=True,
            name="uq_subscription_budget_scope",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_row_id: Mapped[UUID | None] = mapped_column(Uuid)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class SubscriptionBudgetReservation(Base, TimestampMixin):
    """Idempotent debit with an immutable settlement outcome."""

    __tablename__ = "subscription_budget_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ("run_id", "task_row_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_budget_reservation_task",
        ),
        UniqueConstraint(
            "run_id", "idempotency_key", name="uq_subscription_budget_reservation_key"
        ),
        CheckConstraint("status IN ('reserved','released','consumed')", name="status"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_row_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    budget_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")


class SubscriptionDecisionRecord(Base, TimestampMixin):
    """Append-only typed handoffs, primary decisions, and review selections."""

    __tablename__ = "subscription_decision_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ("run_id", "task_row_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_decision_task",
        ),
        ForeignKeyConstraint(
            ("run_id", "task_row_id", "attempt_id"),
            (
                "subscription_attempts.run_id",
                "subscription_attempts.task_row_id",
                "subscription_attempts.id",
            ),
            ondelete="CASCADE",
            name="fk_subscription_decision_attempt",
        ),
        UniqueConstraint("run_id", "idempotency_key", name="uq_subscription_decision_idempotency"),
        CheckConstraint(
            "attempt_id IS NULL OR task_row_id IS NOT NULL",
            name="subscription_decision_attempt_requires_task",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_row_id: Mapped[UUID | None] = mapped_column(Uuid)
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
