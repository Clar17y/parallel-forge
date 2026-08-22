"""Authoritative current run state and isolated resource identity."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.domain.run import RunState
from forge.persistence.models.base import Base, TimestampMixin

RUN_STATES = ",".join(f"'{state.value}'" for state in RunState)
DATABASE_STATES = "'DISABLED','PROVISIONING','ACTIVE','FAILED','REMOVED'"


class Run(Base, TimestampMixin):
    """Current-state projection guarded by an optimistic integer version."""

    __tablename__ = "runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ("task_id", "project_id"),
            ("tasks.id", "tasks.project_id"),
            name="fk_runs_task_project_tasks",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("project_id", "policy_version"),
            ("project_policy_versions.project_id", "project_policy_versions.version"),
            name="fk_runs_policy_version_project_policy_versions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"state IN ({RUN_STATES})", name="state"),
        CheckConstraint("version >= 0", name="version_nonnegative"),
        CheckConstraint(
            "local_remediation_count >= 0 AND remote_remediation_count >= 0",
            name="remediation_counts_nonnegative",
        ),
        CheckConstraint(
            "token_budget >= 0 AND cost_budget_minor >= 0 AND duration_budget_seconds >= 0",
            name="budgets_nonnegative",
        ),
        CheckConstraint(f"database_state IN ({DATABASE_STATES})", name="database_state"),
        CheckConstraint(
            "(database_state IN ('DISABLED','REMOVED') "
            "AND database_name IS NULL AND database_role IS NULL AND secret_id IS NULL) "
            "OR database_state IN ('PROVISIONING','FAILED') "
            "OR (database_state = 'ACTIVE' AND database_name IS NOT NULL "
            "AND database_role IS NOT NULL AND secret_id IS NOT NULL)",
            name="database_resource_shape",
        ),
        CheckConstraint(
            "(state NOT IN ('AWAITING_PLAN_APPROVAL','AWAITING_PR_APPROVAL',"
            "'AWAITING_MERGE_APPROVAL') AND pending_gate IS NULL "
            "AND pending_evidence_digest IS NULL) OR "
            "(state = 'AWAITING_PLAN_APPROVAL' AND pending_gate = 'plan' "
            "AND pending_evidence_digest IS NOT NULL) OR "
            "(state = 'AWAITING_PR_APPROVAL' AND pending_gate = 'pr' "
            "AND pending_evidence_digest IS NOT NULL) OR "
            "(state = 'AWAITING_MERGE_APPROVAL' AND pending_gate = 'merge' "
            "AND pending_evidence_digest IS NOT NULL)",
            name="pending_gate_shape",
        ),
        CheckConstraint(
            "(state = 'PAUSED' AND suspended_state IS NOT NULL AND suspension_kind IS NOT NULL "
            "AND suspension_kind = 'PAUSE' AND suspension_context IS NOT NULL "
            "AND suspension_context_schema_version IS NOT NULL AND suspension_context_schema_version >= 1) OR "
            "(state IN ('INTERVENTION_REQUIRED','AWAITING_HUMAN_INTERVENTION') "
            "AND suspended_state IS NOT NULL AND suspension_kind IS NOT NULL "
            "AND suspension_kind = 'INTERVENTION' AND suspension_context IS NOT NULL "
            "AND suspension_context_schema_version IS NOT NULL AND suspension_context_schema_version >= 1) OR "
            "(state NOT IN ('PAUSED','INTERVENTION_REQUIRED','AWAITING_HUMAN_INTERVENTION') "
            "AND suspended_state IS NULL AND suspension_kind IS NULL "
            "AND suspension_context IS NULL AND suspension_context_schema_version IS NULL)",
            name="suspension_state_shape",
        ),
        Index("ix_runs_state", "state"),
        Index("ix_runs_project_id", "project_id"),
        Index("ix_runs_updated_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(48), nullable=False, default=RunState.CREATED.value)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suspended_state: Mapped[str | None] = mapped_column(String(48))
    suspension_kind: Mapped[str | None] = mapped_column(String(24))
    suspension_context_schema_version: Mapped[int | None] = mapped_column(Integer)
    suspension_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    local_remediation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remote_remediation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_budget_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_budget_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worktree_path: Mapped[str | None] = mapped_column(Text)
    branch_name: Mapped[str | None] = mapped_column(String(512))
    base_ref: Mapped[str | None] = mapped_column(String(512))
    base_sha: Mapped[str | None] = mapped_column(String(40))
    candidate_commit: Mapped[str | None] = mapped_column(String(40))
    database_state: Mapped[str] = mapped_column(String(24), nullable=False, default="DISABLED")
    database_name: Mapped[str | None] = mapped_column(String(63))
    database_role: Mapped[str | None] = mapped_column(String(63))
    secret_id: Mapped[str | None] = mapped_column(String(512))
    pending_gate: Mapped[str | None] = mapped_column(String(16))
    pending_evidence_digest: Mapped[str | None] = mapped_column(String(64))
