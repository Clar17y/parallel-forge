"""Additive task usage monitoring foundation.

Revision ID: 20261010_0038
Revises: 20261007_0037
"""

from alembic import op

revision = "20261010_0038"
down_revision = "20261007_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_runs_id_project", "runs", ["id", "project_id"])
    op.create_unique_constraint(
        "uq_epic_brainstorm_jobs_id_project", "epic_brainstorm_jobs", ["id", "project_id"]
    )
    op.create_unique_constraint(
        "uq_epic_brainstorm_attempts_id_job", "epic_brainstorm_attempts", ["id", "job_id"]
    )
    op.execute(
        """CREATE TABLE task_usage_work_units (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	subject_kind VARCHAR(24) NOT NULL,
	subject_id UUID NOT NULL,
	run_id UUID,
	authoring_job_id UUID,
	admitted_phase_id VARCHAR(96) NOT NULL,
	configured_route_digest VARCHAR(64) NOT NULL,
	configured_route_members JSONB NOT NULL,
	effective_route_digest VARCHAR(64) NOT NULL,
	effective_route_members JSONB NOT NULL,
	source_kind VARCHAR(24) NOT NULL,
	source_id UUID NOT NULL,
	agent_execution_id UUID,
	subscription_attempt_id UUID,
	subscription_task_id UUID,
	authoring_attempt_id UUID,
	schema_version INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_task_usage_work_units PRIMARY KEY (id),
	CONSTRAINT uq_task_usage_work_units_id_project UNIQUE (id, project_id),
	CONSTRAINT uq_task_usage_work_units_subject_identity UNIQUE (id, project_id, subject_kind, subject_id),
	CONSTRAINT uq_task_usage_work_units_source UNIQUE (project_id, subject_kind, subject_id, source_kind, source_id),
	CONSTRAINT fk_task_usage_work_units_run_id_runs FOREIGN KEY(run_id, project_id) REFERENCES runs (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_task_usage_work_units_authoring_job_id_epic_brainstorm_jobs FOREIGN KEY(authoring_job_id, project_id) REFERENCES epic_brainstorm_jobs (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_task_usage_work_units_agent_execution_id_agent_executions FOREIGN KEY(agent_execution_id, run_id) REFERENCES agent_executions (id, run_id) ON DELETE RESTRICT,
	CONSTRAINT fk_task_usage_work_units_subscription_attempt_id_subscr_d644 FOREIGN KEY(subscription_attempt_id, run_id, subscription_task_id) REFERENCES subscription_attempts (id, run_id, task_row_id) ON DELETE RESTRICT,
	CONSTRAINT fk_task_usage_work_units_authoring_attempt_id_epic_brai_671a FOREIGN KEY(authoring_attempt_id, authoring_job_id) REFERENCES epic_brainstorm_attempts (id, job_id) ON DELETE RESTRICT,
	CONSTRAINT ck_task_usage_work_units_subject_shape CHECK ((subject_kind = 'run' AND run_id IS NOT NULL AND run_id = subject_id AND authoring_job_id IS NULL) OR (subject_kind = 'authoring_job' AND authoring_job_id IS NOT NULL AND authoring_job_id = subject_id AND run_id IS NULL)),
	CONSTRAINT ck_task_usage_work_units_source_shape CHECK ((source_kind = 'agent_execution' AND agent_execution_id IS NOT NULL AND agent_execution_id = source_id AND run_id IS NOT NULL AND subscription_attempt_id IS NULL AND subscription_task_id IS NULL AND authoring_attempt_id IS NULL) OR (source_kind = 'subscription_attempt' AND subscription_attempt_id IS NOT NULL AND subscription_attempt_id = source_id AND subscription_task_id IS NOT NULL AND run_id IS NOT NULL AND agent_execution_id IS NULL AND authoring_attempt_id IS NULL) OR (source_kind = 'authoring_attempt' AND authoring_attempt_id IS NOT NULL AND authoring_attempt_id = source_id AND authoring_job_id IS NOT NULL AND agent_execution_id IS NULL AND subscription_attempt_id IS NULL AND subscription_task_id IS NULL)),
	CONSTRAINT ck_task_usage_work_units_version_phase CHECK (schema_version = 1 AND length(admitted_phase_id) > 0 AND configured_route_digest ~ '^[0-9a-f]{64}$' AND effective_route_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_task_usage_work_units_route_members_array CHECK (jsonb_typeof(configured_route_members) = 'array' AND jsonb_array_length(configured_route_members) > 0 AND jsonb_typeof(effective_route_members) = 'array' AND jsonb_array_length(effective_route_members) > 0)
)
        """
    )
    op.execute(
        """CREATE TABLE task_usage_policy_revisions (
	project_id UUID NOT NULL,
	revision BIGINT NOT NULL,
	mode VARCHAR(16) NOT NULL,
	warning_multiplier FLOAT NOT NULL,
	checkpoint_multiplier FLOAT NOT NULL,
	history_limit BIGINT NOT NULL,
	minimum_comparables BIGINT NOT NULL,
	hard_token_cap BIGINT,
	hard_context_cap BIGINT,
	actor_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_task_usage_policy_revisions PRIMARY KEY (project_id, revision),
	CONSTRAINT fk_task_usage_policy_revisions_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
	CONSTRAINT ck_task_usage_policy_revisions_sampling_shape CHECK (revision >= 1 AND history_limit >= minimum_comparables AND minimum_comparables >= 1),
	CONSTRAINT ck_task_usage_policy_revisions_mode CHECK (mode IN ('report_only','warn','checkpoint')),
	CONSTRAINT ck_task_usage_policy_revisions_multipliers CHECK (warning_multiplier >= 1 AND warning_multiplier < 'Infinity'::float8 AND checkpoint_multiplier >= warning_multiplier AND checkpoint_multiplier < 'Infinity'::float8),
	CONSTRAINT ck_task_usage_policy_revisions_token_cap CHECK (hard_token_cap IS NULL OR hard_token_cap > 0),
	CONSTRAINT ck_task_usage_policy_revisions_context_cap CHECK (hard_context_cap IS NULL OR hard_context_cap > 0)
)
        """
    )
    op.execute(
        """CREATE TABLE task_usage_observations (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	work_unit_id UUID NOT NULL,
	event_id VARCHAR(256) NOT NULL,
	sequence BIGINT NOT NULL,
	counter_kind VARCHAR(16) NOT NULL,
	scope VARCHAR(16) NOT NULL,
	input_tokens BIGINT,
	output_tokens BIGINT,
	cached_input_tokens BIGINT,
	reasoning_output_tokens BIGINT,
	context_occupied_tokens BIGINT,
	context_capacity_tokens BIGINT,
	compaction_count BIGINT,
	unknown_dimensions JSONB DEFAULT '[]'::jsonb NOT NULL,
	final BOOLEAN NOT NULL,
	final_status VARCHAR(32),
	reconciles_event_id VARCHAR(256),
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	schema_version INTEGER NOT NULL,
	CONSTRAINT pk_task_usage_observations PRIMARY KEY (id),
	CONSTRAINT fk_task_usage_observations_work_unit_id_task_usage_work_units FOREIGN KEY(work_unit_id, project_id) REFERENCES task_usage_work_units (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_task_usage_observations_work_unit_id_task_usage_observations FOREIGN KEY(work_unit_id, reconciles_event_id) REFERENCES task_usage_observations (work_unit_id, event_id) ON DELETE RESTRICT,
	CONSTRAINT uq_task_usage_observations_event UNIQUE (work_unit_id, event_id),
	CONSTRAINT uq_task_usage_observations_sequence UNIQUE (work_unit_id, sequence),
	CONSTRAINT ck_task_usage_observations_identity_shape CHECK (sequence >= 1 AND schema_version = 1 AND length(event_id) > 0 AND counter_kind IN ('cumulative','delta') AND scope = 'source'),
	CONSTRAINT ck_task_usage_observations_input_nonnegative CHECK (input_tokens IS NULL OR input_tokens >= 0),
	CONSTRAINT ck_task_usage_observations_output_nonnegative CHECK (output_tokens IS NULL OR output_tokens >= 0),
	CONSTRAINT ck_task_usage_observations_cached_subset CHECK (cached_input_tokens IS NULL OR (cached_input_tokens >= 0 AND (input_tokens IS NULL OR cached_input_tokens <= input_tokens))),
	CONSTRAINT ck_task_usage_observations_reasoning_subset CHECK (reasoning_output_tokens IS NULL OR (reasoning_output_tokens >= 0 AND (output_tokens IS NULL OR reasoning_output_tokens <= output_tokens))),
	CONSTRAINT ck_task_usage_observations_context_nonnegative CHECK (context_occupied_tokens IS NULL OR context_occupied_tokens >= 0),
	CONSTRAINT ck_task_usage_observations_context_capacity CHECK (context_capacity_tokens IS NULL OR (context_capacity_tokens >= 0 AND (context_occupied_tokens IS NULL OR context_occupied_tokens <= context_capacity_tokens))),
	CONSTRAINT ck_task_usage_observations_compaction_nonnegative CHECK (compaction_count IS NULL OR compaction_count >= 0),
	CONSTRAINT ck_task_usage_observations_unknown_dimensions_array CHECK (jsonb_typeof(unknown_dimensions) = 'array'),
	CONSTRAINT ck_task_usage_observations_final_shape CHECK ((final AND final_status IS NOT NULL AND length(final_status) > 0) OR (NOT final AND final_status IS NULL AND reconciles_event_id IS NULL)),
	CONSTRAINT ck_task_usage_observations_not_self_reconciliation CHECK (reconciles_event_id IS NULL OR reconciles_event_id <> event_id)
)
        """
    )
    op.execute(
        """CREATE TABLE task_usage_baselines (
	work_unit_id UUID NOT NULL,
	project_id UUID NOT NULL,
	subject_kind VARCHAR(24) NOT NULL,
	subject_id UUID NOT NULL,
	policy_revision BIGINT NOT NULL,
	history_digest VARCHAR(64) NOT NULL,
	snapshot_digest VARCHAR(64) NOT NULL,
	estimator_version VARCHAR(64) NOT NULL,
	window_limit BIGINT NOT NULL,
	minimum_comparables BIGINT NOT NULL,
	cohort_digest VARCHAR(64) NOT NULL,
	window_started_at TIMESTAMP WITH TIME ZONE,
	window_ended_at TIMESTAMP WITH TIME ZONE,
	frozen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	schema_version INTEGER NOT NULL,
	sample_count BIGINT NOT NULL,
	status VARCHAR(24) NOT NULL,
	reference_tokens FLOAT,
	median_tokens FLOAT,
	p90_tokens FLOAT,
	mean_tokens FLOAT,
	warning_threshold_tokens FLOAT,
	checkpoint_threshold_tokens FLOAT,
	frozen_payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_task_usage_baselines PRIMARY KEY (work_unit_id),
	CONSTRAINT fk_task_usage_baselines_work_unit_id_task_usage_work_units FOREIGN KEY(work_unit_id, project_id) REFERENCES task_usage_work_units (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_task_usage_baselines_work_unit_id_task_usage_work_units_subject FOREIGN KEY(work_unit_id, project_id, subject_kind, subject_id) REFERENCES task_usage_work_units (id, project_id, subject_kind, subject_id) ON DELETE RESTRICT,
	CONSTRAINT uq_task_usage_baselines_subject UNIQUE (project_id, subject_kind, subject_id),
	CONSTRAINT uq_task_usage_baselines_subject_identity UNIQUE (work_unit_id, project_id, subject_kind, subject_id, policy_revision, snapshot_digest),
	CONSTRAINT fk_task_usage_baselines_project_id_task_usage_policy_revisions FOREIGN KEY(project_id, policy_revision) REFERENCES task_usage_policy_revisions (project_id, revision) ON DELETE RESTRICT,
	CONSTRAINT ck_task_usage_baselines_sample_shape CHECK (sample_count >= 0 AND status IN ('ready','insufficient_history') AND history_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_task_usage_baselines_reference_shape CHECK ((status = 'ready' AND reference_tokens IS NOT NULL AND reference_tokens >= 0) OR (status = 'insufficient_history' AND reference_tokens IS NULL))
	, CONSTRAINT ck_task_usage_baselines_summary_shape CHECK ((reference_tokens IS NULL OR reference_tokens < 'Infinity'::float8) AND (median_tokens IS NULL OR (median_tokens >= 0 AND median_tokens < 'Infinity'::float8)) AND (p90_tokens IS NULL OR (p90_tokens >= 0 AND p90_tokens < 'Infinity'::float8)) AND (mean_tokens IS NULL OR (mean_tokens >= 0 AND mean_tokens < 'Infinity'::float8)) AND jsonb_typeof(frozen_payload) = 'object'),
	CONSTRAINT ck_task_usage_baselines_snapshot_shape CHECK (schema_version = 1 AND snapshot_digest ~ '^[0-9a-f]{64}$' AND cohort_digest ~ '^[0-9a-f]{64}$' AND length(estimator_version) > 0 AND window_limit >= minimum_comparables AND minimum_comparables >= 1 AND sample_count <= window_limit AND (window_started_at IS NULL) = (window_ended_at IS NULL) AND (window_started_at IS NULL OR (window_started_at <= window_ended_at AND window_ended_at <= frozen_at))),
	CONSTRAINT ck_task_usage_baselines_threshold_shape CHECK ((warning_threshold_tokens IS NULL OR (warning_threshold_tokens >= 0 AND warning_threshold_tokens < 'Infinity'::float8)) AND (checkpoint_threshold_tokens IS NULL OR (checkpoint_threshold_tokens >= 0 AND checkpoint_threshold_tokens < 'Infinity'::float8)) AND (warning_threshold_tokens IS NULL OR checkpoint_threshold_tokens IS NULL OR checkpoint_threshold_tokens >= warning_threshold_tokens) AND ((status = 'insufficient_history' AND warning_threshold_tokens IS NULL AND checkpoint_threshold_tokens IS NULL) OR (status = 'ready' AND sample_count >= minimum_comparables AND window_started_at IS NOT NULL AND median_tokens IS NOT NULL AND p90_tokens IS NOT NULL AND mean_tokens IS NOT NULL AND warning_threshold_tokens IS NOT NULL AND checkpoint_threshold_tokens IS NOT NULL)))
)
        """
    )
    op.execute(
        """CREATE TABLE task_usage_checkpoints (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	work_unit_id UUID NOT NULL,
	baseline_work_unit_id UUID NOT NULL,
	subject_kind VARCHAR(24) NOT NULL,
	subject_id UUID NOT NULL,
	policy_revision BIGINT NOT NULL,
	reference_digest VARCHAR(64) NOT NULL,
	trigger_event_id VARCHAR(256) NOT NULL,
	state VARCHAR(32) NOT NULL,
	version BIGINT NOT NULL,
	process_settled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_task_usage_checkpoints PRIMARY KEY (id),
	CONSTRAINT fk_task_usage_checkpoints_work_unit_id_task_usage_work_units FOREIGN KEY(work_unit_id, project_id, subject_kind, subject_id) REFERENCES task_usage_work_units (id, project_id, subject_kind, subject_id) ON DELETE RESTRICT,
	CONSTRAINT fk_task_usage_checkpoints_baseline_work_unit_id_baselines FOREIGN KEY(baseline_work_unit_id, project_id, subject_kind, subject_id, policy_revision, reference_digest) REFERENCES task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, snapshot_digest) ON DELETE RESTRICT,
	CONSTRAINT fk_task_usage_checkpoints_work_unit_id_task_usage_observations FOREIGN KEY(work_unit_id, trigger_event_id) REFERENCES task_usage_observations (work_unit_id, event_id) ON DELETE RESTRICT,
	CONSTRAINT uq_task_usage_checkpoints_id_project UNIQUE (id, project_id),
	CONSTRAINT uq_task_usage_checkpoints_trigger UNIQUE (work_unit_id, trigger_event_id),
	CONSTRAINT ck_task_usage_checkpoints_state_shape CHECK (version >= 0 AND state IN ('checkpoint_requested','pausing','paused','resumed','resolved') AND (state NOT IN ('paused','resumed') OR process_settled))
)
        """
    )
    op.execute(
        """CREATE TABLE task_usage_owner_commands (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	checkpoint_id UUID NOT NULL,
	idempotency_key VARCHAR(255) NOT NULL,
	action VARCHAR(32) NOT NULL,
	expected_version BIGINT NOT NULL,
	result_version BIGINT NOT NULL,
	actor_id UUID NOT NULL,
	state VARCHAR(32) NOT NULL,
	command_payload JSONB NOT NULL,
	warnings JSONB NOT NULL,
	affected_snapshot_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_task_usage_owner_commands PRIMARY KEY (id),
	CONSTRAINT fk_task_usage_owner_commands_checkpoint_id_task_usage_c_1ebe FOREIGN KEY(checkpoint_id, project_id) REFERENCES task_usage_checkpoints (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT uq_task_usage_owner_commands_key UNIQUE (checkpoint_id, idempotency_key),
	CONSTRAINT ck_task_usage_owner_commands_version_shape CHECK (expected_version >= 0 AND result_version >= expected_version),
	CONSTRAINT ck_task_usage_owner_commands_payload_shape CHECK (action IN ('continue','resume','resolve','set_policy','override_checkpoint') AND jsonb_typeof(command_payload) = 'object' AND jsonb_typeof(warnings) = 'array' AND affected_snapshot_digest ~ '^[0-9a-f]{64}$')
)
        """
    )

    op.execute(
        """CREATE FUNCTION task_usage_reject_record_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'task usage record is immutable';
END;
$$ LANGUAGE plpgsql"""
    )
    for table in (
        "task_usage_work_units",
        "task_usage_policy_revisions",
        "task_usage_observations",
        "task_usage_baselines",
        "task_usage_owner_commands",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION task_usage_reject_record_change()"
        )
    op.execute(
        """CREATE FUNCTION task_usage_check_checkpoint_identity() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'task usage checkpoint identity is immutable';
    END IF;
    IF OLD.id IS DISTINCT FROM NEW.id OR OLD.project_id IS DISTINCT FROM NEW.project_id
       OR OLD.work_unit_id IS DISTINCT FROM NEW.work_unit_id
       OR OLD.baseline_work_unit_id IS DISTINCT FROM NEW.baseline_work_unit_id
       OR OLD.subject_kind IS DISTINCT FROM NEW.subject_kind
       OR OLD.subject_id IS DISTINCT FROM NEW.subject_id
       OR OLD.policy_revision IS DISTINCT FROM NEW.policy_revision
       OR OLD.reference_digest IS DISTINCT FROM NEW.reference_digest
       OR OLD.trigger_event_id IS DISTINCT FROM NEW.trigger_event_id
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'task usage checkpoint identity is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql"""
    )
    op.execute(
        "CREATE TRIGGER trg_task_usage_checkpoint_identity BEFORE UPDATE OR DELETE ON task_usage_checkpoints "
        "FOR EACH ROW EXECUTE FUNCTION task_usage_check_checkpoint_identity()"
    )


def downgrade() -> None:
    tables = (
        "task_usage_work_units",
        "task_usage_policy_revisions",
        "task_usage_observations",
        "task_usage_baselines",
        "task_usage_checkpoints",
        "task_usage_owner_commands",
    )
    op.execute("LOCK TABLE " + ", ".join(tables) + " IN ACCESS EXCLUSIVE MODE")
    bind = op.get_bind()
    for table in tables:
        if bind.exec_driver_sql(f"SELECT EXISTS (SELECT 1 FROM {table})").scalar():
            raise RuntimeError("durable task usage evidence must not be discarded")
    op.drop_table("task_usage_owner_commands")
    op.drop_table("task_usage_checkpoints")
    op.drop_table("task_usage_baselines")
    op.drop_table("task_usage_observations")
    op.drop_table("task_usage_policy_revisions")
    op.drop_table("task_usage_work_units")
    op.execute("DROP FUNCTION task_usage_check_checkpoint_identity()")
    op.execute("DROP FUNCTION task_usage_reject_record_change()")
    op.drop_constraint(
        "uq_epic_brainstorm_attempts_id_job", "epic_brainstorm_attempts", type_="unique"
    )
    op.drop_constraint("uq_epic_brainstorm_jobs_id_project", "epic_brainstorm_jobs", type_="unique")
    op.drop_constraint("uq_runs_id_project", "runs", type_="unique")
