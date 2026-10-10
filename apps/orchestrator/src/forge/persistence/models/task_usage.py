"""Durable, project-scoped monitoring facts. No usage is inferred from these rows."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class TaskUsageWorkUnit(Base):
    __tablename__ = "task_usage_work_units"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_task_usage_work_units_id_project"),
        UniqueConstraint(
            "id",
            "project_id",
            "subject_kind",
            "subject_id",
            name="uq_task_usage_work_units_subject_identity",
        ),
        UniqueConstraint(
            "project_id",
            "subject_kind",
            "subject_id",
            "source_kind",
            "source_id",
            name="uq_task_usage_work_units_source",
        ),
        ForeignKeyConstraint(
            ("run_id", "project_id"), ("runs.id", "runs.project_id"), ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ("authoring_job_id", "project_id"),
            ("epic_brainstorm_jobs.id", "epic_brainstorm_jobs.project_id"),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("agent_execution_id", "run_id"),
            ("agent_executions.id", "agent_executions.run_id"),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("subscription_attempt_id", "run_id", "subscription_task_id"),
            (
                "subscription_attempts.id",
                "subscription_attempts.run_id",
                "subscription_attempts.task_row_id",
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("authoring_attempt_id", "authoring_job_id"),
            ("epic_brainstorm_attempts.id", "epic_brainstorm_attempts.job_id"),
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(subject_kind = 'run' AND run_id IS NOT NULL AND run_id = subject_id AND authoring_job_id IS NULL) OR (subject_kind = 'authoring_job' AND authoring_job_id IS NOT NULL AND authoring_job_id = subject_id AND run_id IS NULL)",
            name="subject_shape",
        ),
        CheckConstraint(
            "(source_kind = 'agent_execution' AND agent_execution_id IS NOT NULL AND agent_execution_id = source_id AND run_id IS NOT NULL AND subscription_attempt_id IS NULL AND subscription_task_id IS NULL AND authoring_attempt_id IS NULL) OR (source_kind = 'subscription_attempt' AND subscription_attempt_id IS NOT NULL AND subscription_attempt_id = source_id AND subscription_task_id IS NOT NULL AND run_id IS NOT NULL AND agent_execution_id IS NULL AND authoring_attempt_id IS NULL) OR (source_kind = 'authoring_attempt' AND authoring_attempt_id IS NOT NULL AND authoring_attempt_id = source_id AND authoring_job_id IS NOT NULL AND agent_execution_id IS NULL AND subscription_attempt_id IS NULL AND subscription_task_id IS NULL)",
            name="source_shape",
        ),
        CheckConstraint(
            "schema_version = 1 AND length(admitted_phase_id) > 0 AND configured_route_digest ~ '^[0-9a-f]{64}$' AND effective_route_digest ~ '^[0-9a-f]{64}$'",
            name="version_phase",
        ),
        CheckConstraint(
            "jsonb_typeof(configured_route_members) = 'array' AND jsonb_array_length(configured_route_members) > 0 AND jsonb_typeof(effective_route_members) = 'array' AND jsonb_array_length(effective_route_members) > 0",
            name="route_members_array",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(Uuid)
    authoring_job_id: Mapped[UUID | None] = mapped_column(Uuid)
    admitted_phase_id: Mapped[str] = mapped_column(String(96), nullable=False)
    configured_route_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    configured_route_members: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    effective_route_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_route_members: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    agent_execution_id: Mapped[UUID | None] = mapped_column(Uuid)
    subscription_attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    subscription_task_id: Mapped[UUID | None] = mapped_column(Uuid)
    authoring_attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskUsageObservation(Base):
    __tablename__ = "task_usage_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ("work_unit_id", "project_id"),
            ("task_usage_work_units.id", "task_usage_work_units.project_id"),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("work_unit_id", "reconciles_event_id"),
            ("task_usage_observations.work_unit_id", "task_usage_observations.event_id"),
            ondelete="RESTRICT",
        ),
        UniqueConstraint("work_unit_id", "event_id", name="uq_task_usage_observations_event"),
        UniqueConstraint("work_unit_id", "sequence", name="uq_task_usage_observations_sequence"),
        CheckConstraint(
            "sequence >= 1 AND schema_version = 1 AND length(event_id) > 0 AND counter_kind IN ('cumulative','delta') AND scope = 'source'",
            name="identity_shape",
        ),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_nonnegative"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="output_nonnegative"),
        CheckConstraint(
            "cached_input_tokens IS NULL OR (cached_input_tokens >= 0 AND (input_tokens IS NULL OR cached_input_tokens <= input_tokens))",
            name="cached_subset",
        ),
        CheckConstraint(
            "reasoning_output_tokens IS NULL OR (reasoning_output_tokens >= 0 AND (output_tokens IS NULL OR reasoning_output_tokens <= output_tokens))",
            name="reasoning_subset",
        ),
        CheckConstraint(
            "context_occupied_tokens IS NULL OR context_occupied_tokens >= 0",
            name="context_nonnegative",
        ),
        CheckConstraint(
            "context_capacity_tokens IS NULL OR (context_capacity_tokens >= 0 AND (context_occupied_tokens IS NULL OR context_occupied_tokens <= context_capacity_tokens))",
            name="context_capacity",
        ),
        CheckConstraint(
            "compaction_count IS NULL OR compaction_count >= 0", name="compaction_nonnegative"
        ),
        CheckConstraint(
            "jsonb_typeof(unknown_dimensions) = 'array'", name="unknown_dimensions_array"
        ),
        CheckConstraint(
            "(final AND final_status IS NOT NULL AND length(final_status) > 0) OR (NOT final AND final_status IS NULL AND reconciles_event_id IS NULL)",
            name="final_shape",
        ),
        CheckConstraint(
            "reconciles_event_id IS NULL OR reconciles_event_id <> event_id",
            name="not_self_reconciliation",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    work_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    counter_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="source")
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_output_tokens: Mapped[int | None] = mapped_column(Integer)
    context_occupied_tokens: Mapped[int | None] = mapped_column(Integer)
    context_capacity_tokens: Mapped[int | None] = mapped_column(Integer)
    compaction_count: Mapped[int | None] = mapped_column(Integer)
    unknown_dimensions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    final: Mapped[bool] = mapped_column(nullable=False, default=False)
    final_status: Mapped[str | None] = mapped_column(String(32))
    reconciles_event_id: Mapped[str | None] = mapped_column(String(256))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TaskUsagePolicyRevision(Base):
    __tablename__ = "task_usage_policy_revisions"
    __table_args__ = (
        ForeignKeyConstraint(("project_id",), ("projects.id",), ondelete="RESTRICT"),
        CheckConstraint(
            "revision >= 1 AND history_limit >= minimum_comparables AND minimum_comparables >= 1",
            name="sampling_shape",
        ),
        CheckConstraint("mode IN ('report_only','warn','checkpoint')", name="mode"),
        CheckConstraint(
            "warning_multiplier >= 1 AND warning_multiplier < 'Infinity'::float8 AND checkpoint_multiplier >= warning_multiplier AND checkpoint_multiplier < 'Infinity'::float8",
            name="multipliers",
        ),
        CheckConstraint("hard_token_cap IS NULL OR hard_token_cap > 0", name="token_cap"),
        CheckConstraint("hard_context_cap IS NULL OR hard_context_cap > 0", name="context_cap"),
    )
    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    warning_multiplier: Mapped[float] = mapped_column(nullable=False)
    checkpoint_multiplier: Mapped[float] = mapped_column(nullable=False)
    history_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_comparables: Mapped[int] = mapped_column(Integer, nullable=False)
    hard_token_cap: Mapped[int | None] = mapped_column(Integer)
    hard_context_cap: Mapped[int | None] = mapped_column(Integer)
    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskUsageBaseline(Base):
    __tablename__ = "task_usage_baselines"
    __table_args__ = (
        ForeignKeyConstraint(
            ("work_unit_id", "project_id"),
            ("task_usage_work_units.id", "task_usage_work_units.project_id"),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("work_unit_id", "project_id", "subject_kind", "subject_id"),
            (
                "task_usage_work_units.id",
                "task_usage_work_units.project_id",
                "task_usage_work_units.subject_kind",
                "task_usage_work_units.subject_id",
            ),
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "project_id", "subject_kind", "subject_id", name="uq_task_usage_baselines_subject"
        ),
        UniqueConstraint(
            "work_unit_id",
            "project_id",
            "subject_kind",
            "subject_id",
            "policy_revision",
            "snapshot_digest",
            name="uq_task_usage_baselines_subject_identity",
        ),
        ForeignKeyConstraint(
            ("project_id", "policy_revision"),
            ("task_usage_policy_revisions.project_id", "task_usage_policy_revisions.revision"),
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "sample_count >= 0 AND status IN ('ready','insufficient_history') AND history_digest ~ '^[0-9a-f]{64}$'",
            name="sample_shape",
        ),
        CheckConstraint(
            "(status = 'ready' AND reference_tokens IS NOT NULL AND reference_tokens >= 0) OR (status = 'insufficient_history' AND reference_tokens IS NULL)",
            name="reference_shape",
        ),
        CheckConstraint(
            "(reference_tokens IS NULL OR reference_tokens < 'Infinity'::float8) AND (median_tokens IS NULL OR (median_tokens >= 0 AND median_tokens < 'Infinity'::float8)) AND (p90_tokens IS NULL OR (p90_tokens >= 0 AND p90_tokens < 'Infinity'::float8)) AND (mean_tokens IS NULL OR (mean_tokens >= 0 AND mean_tokens < 'Infinity'::float8)) AND jsonb_typeof(frozen_payload) = 'object'",
            name="summary_shape",
        ),
        CheckConstraint(
            "schema_version = 1 AND snapshot_digest ~ '^[0-9a-f]{64}$' AND cohort_digest ~ '^[0-9a-f]{64}$' AND length(estimator_version) > 0 AND window_limit >= minimum_comparables AND minimum_comparables >= 1 AND sample_count <= window_limit AND (window_started_at IS NULL) = (window_ended_at IS NULL) AND (window_started_at IS NULL OR (window_started_at <= window_ended_at AND window_ended_at <= frozen_at))",
            name="snapshot_shape",
        ),
        CheckConstraint(
            "(warning_threshold_tokens IS NULL OR (warning_threshold_tokens >= 0 AND warning_threshold_tokens < 'Infinity'::float8)) AND (checkpoint_threshold_tokens IS NULL OR (checkpoint_threshold_tokens >= 0 AND checkpoint_threshold_tokens < 'Infinity'::float8)) AND (warning_threshold_tokens IS NULL OR checkpoint_threshold_tokens IS NULL OR checkpoint_threshold_tokens >= warning_threshold_tokens) AND ((status = 'insufficient_history' AND warning_threshold_tokens IS NULL AND checkpoint_threshold_tokens IS NULL) OR (status = 'ready' AND sample_count >= minimum_comparables AND window_started_at IS NOT NULL AND median_tokens IS NOT NULL AND p90_tokens IS NOT NULL AND mean_tokens IS NOT NULL AND warning_threshold_tokens IS NOT NULL AND checkpoint_threshold_tokens IS NOT NULL))",
            name="threshold_shape",
        ),
    )
    work_unit_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    history_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    estimator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    window_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_comparables: Mapped[int] = mapped_column(Integer, nullable=False)
    cohort_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reference_tokens: Mapped[float | None] = mapped_column(Float)
    median_tokens: Mapped[float | None] = mapped_column(Float)
    p90_tokens: Mapped[float | None] = mapped_column(Float)
    mean_tokens: Mapped[float | None] = mapped_column(Float)
    warning_threshold_tokens: Mapped[float | None] = mapped_column(Float)
    checkpoint_threshold_tokens: Mapped[float | None] = mapped_column(Float)
    frozen_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskUsageCheckpoint(Base):
    __tablename__ = "task_usage_checkpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ("work_unit_id", "project_id", "subject_kind", "subject_id"),
            (
                "task_usage_work_units.id",
                "task_usage_work_units.project_id",
                "task_usage_work_units.subject_kind",
                "task_usage_work_units.subject_id",
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            (
                "baseline_work_unit_id",
                "project_id",
                "subject_kind",
                "subject_id",
                "policy_revision",
                "reference_digest",
            ),
            (
                "task_usage_baselines.work_unit_id",
                "task_usage_baselines.project_id",
                "task_usage_baselines.subject_kind",
                "task_usage_baselines.subject_id",
                "task_usage_baselines.policy_revision",
                "task_usage_baselines.snapshot_digest",
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("work_unit_id", "trigger_event_id"),
            ("task_usage_observations.work_unit_id", "task_usage_observations.event_id"),
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "project_id", name="uq_task_usage_checkpoints_id_project"),
        UniqueConstraint(
            "work_unit_id", "trigger_event_id", name="uq_task_usage_checkpoints_trigger"
        ),
        CheckConstraint(
            "version >= 0 AND state IN ('checkpoint_requested','pausing','paused','resumed','resolved') AND (state NOT IN ('paused','resumed') OR process_settled)",
            name="state_shape",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    work_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    baseline_work_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    process_settled: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskUsageOwnerCommand(Base):
    __tablename__ = "task_usage_owner_commands"
    __table_args__ = (
        ForeignKeyConstraint(
            ("checkpoint_id", "project_id"),
            ("task_usage_checkpoints.id", "task_usage_checkpoints.project_id"),
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "checkpoint_id", "idempotency_key", name="uq_task_usage_owner_commands_key"
        ),
        CheckConstraint(
            "expected_version >= 0 AND result_version >= expected_version", name="version_shape"
        ),
        CheckConstraint(
            "action IN ('continue','resume','resolve','set_policy','override_checkpoint') AND jsonb_typeof(command_payload) = 'object' AND jsonb_typeof(warnings) = 'array' AND affected_snapshot_digest ~ '^[0-9a-f]{64}$'",
            name="payload_shape",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    checkpoint_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    command_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    affected_snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
