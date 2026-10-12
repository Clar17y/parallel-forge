"""Fence active usage checkpoints and allow an explicit owner stop.

Revision ID: 20261011_0040
Revises: 20261010_0039
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0040"
down_revision = "20261010_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for dimension in ("input", "output", "cached_input"):
        op.add_column("model_usage", sa.Column(f"{dimension}_tokens_complete", sa.Boolean(), nullable=True))
    op.execute(
        """CREATE TABLE task_usage_subject_admissions (
            project_id UUID NOT NULL,
            subject_kind VARCHAR(24) NOT NULL,
            subject_id UUID NOT NULL,
            run_id UUID,
            authoring_job_id UUID,
            policy_revision BIGINT,
            configured_route_digest VARCHAR(64) NOT NULL,
            configured_route_members JSONB NOT NULL,
            admitted_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT pk_task_usage_subject_admissions PRIMARY KEY (project_id, subject_kind, subject_id),
            CONSTRAINT fk_task_usage_subject_admissions_project_id_projects FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
            CONSTRAINT fk_task_usage_subject_admissions_run_id_runs FOREIGN KEY (run_id, project_id) REFERENCES runs (id, project_id) ON DELETE RESTRICT,
            CONSTRAINT fk_task_usage_subject_admissions_authoring_job_id_epic_brainstorm_jobs FOREIGN KEY (authoring_job_id, project_id) REFERENCES epic_brainstorm_jobs (id, project_id) ON DELETE RESTRICT,
            CONSTRAINT fk_task_usage_subject_admissions_project_id_task_usage_policy_revisions FOREIGN KEY (project_id, policy_revision) REFERENCES task_usage_policy_revisions (project_id, revision) ON DELETE RESTRICT,
            CONSTRAINT ck_task_usage_subject_admissions_subject_shape CHECK ((subject_kind = 'run' AND run_id = subject_id AND authoring_job_id IS NULL) OR (subject_kind = 'authoring_job' AND authoring_job_id = subject_id AND run_id IS NULL)),
            CONSTRAINT ck_task_usage_subject_admissions_snapshot_shape CHECK ((policy_revision IS NULL OR policy_revision > 0) AND configured_route_digest ~ '^[0-9a-f]{64}$' AND jsonb_typeof(configured_route_members) = 'array')
        )"""
    )
    op.execute(
        "CREATE TRIGGER trg_task_usage_subject_admissions_immutable "
        "BEFORE UPDATE OR DELETE ON task_usage_subject_admissions FOR EACH ROW "
        "EXECUTE FUNCTION task_usage_reject_record_change()"
    )
    op.add_column("task_usage_checkpoints", sa.Column("generation", sa.BigInteger(), nullable=True))
    op.execute(
        "WITH numbered AS (SELECT id, row_number() OVER "
        "(PARTITION BY project_id, subject_kind, subject_id ORDER BY created_at, id) AS ordinal "
        "FROM task_usage_checkpoints) UPDATE task_usage_checkpoints AS checkpoint "
        "SET generation = numbered.ordinal FROM numbered WHERE checkpoint.id = numbered.id"
    )
    op.alter_column("task_usage_checkpoints", "generation", nullable=False)
    op.alter_column("task_usage_checkpoints", "generation", server_default=sa.text("1"))
    op.execute(
        "CREATE FUNCTION task_usage_reject_generation_change() RETURNS trigger AS $$ "
        "BEGIN IF OLD.generation IS DISTINCT FROM NEW.generation THEN "
        "RAISE EXCEPTION 'checkpoint generation is immutable'; END IF; RETURN NEW; "
        "END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_task_usage_checkpoint_generation BEFORE UPDATE OF generation "
        "ON task_usage_checkpoints FOR EACH ROW EXECUTE FUNCTION task_usage_reject_generation_change()"
    )
    op.drop_constraint(op.f("ck_task_usage_checkpoints_state_shape"), "task_usage_checkpoints", type_="check")
    op.create_check_constraint(
        op.f("ck_task_usage_checkpoints_state_shape"),
        "task_usage_checkpoints",
        "version >= 0 AND generation > 0 AND state IN ('checkpoint_requested','pausing','paused','resumed','resolved') "
        "AND (state NOT IN ('paused','resumed') OR process_settled)",
    )
    op.add_column("task_usage_checkpoints", sa.Column("next_checkpoint_threshold_tokens", sa.BigInteger(), nullable=True))
    op.add_column("task_usage_checkpoints", sa.Column("usage_pauses_disabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("task_usage_checkpoints", sa.Column("monitoring_policy_revision", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_task_usage_checkpoints_continuation_shape"),
        "task_usage_checkpoints",
        "(next_checkpoint_threshold_tokens IS NULL OR next_checkpoint_threshold_tokens > 0) "
        "AND (monitoring_policy_revision IS NULL OR monitoring_policy_revision > 0) "
        "AND (NOT usage_pauses_disabled OR next_checkpoint_threshold_tokens IS NULL)",
    )
    op.drop_constraint(
        op.f("ck_task_usage_owner_commands_payload_shape"), "task_usage_owner_commands", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_task_usage_owner_commands_payload_shape"),
        "task_usage_owner_commands",
        "action IN ('continue','resume','resolve','set_policy','override_checkpoint','stop') "
        "AND jsonb_typeof(command_payload) = 'object' AND jsonb_typeof(warnings) = 'array' "
        "AND affected_snapshot_digest ~ '^[0-9a-f]{64}$'",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_task_usage_checkpoints_active_subject "
        "ON task_usage_checkpoints (project_id, subject_kind, subject_id) "
        "WHERE state IN ('checkpoint_requested','pausing','paused')"
    )


def downgrade() -> None:
    if op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM model_usage WHERE input_tokens_complete IS NOT NULL "
        "OR output_tokens_complete IS NOT NULL OR cached_input_tokens_complete IS NOT NULL)"
    ).scalar():
        raise RuntimeError("explicit provider telemetry evidence must not be discarded")
    if op.get_bind().exec_driver_sql("SELECT EXISTS (SELECT 1 FROM task_usage_subject_admissions)").scalar():
        raise RuntimeError("durable subject admission snapshots must not be discarded")
    if op.get_bind().exec_driver_sql("SELECT EXISTS (SELECT 1 FROM task_usage_owner_commands WHERE action = 'stop')").scalar():
        raise RuntimeError("durable stop commands must not be discarded")
    if op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM task_usage_checkpoints WHERE generation > 1 "
        "OR next_checkpoint_threshold_tokens IS NOT NULL OR usage_pauses_disabled "
        "OR monitoring_policy_revision IS NOT NULL)"
    ).scalar():
        raise RuntimeError("durable checkpoint continuation must not be discarded")
    op.execute("DROP INDEX uq_task_usage_checkpoints_active_subject")
    op.drop_constraint(
        op.f("ck_task_usage_owner_commands_payload_shape"), "task_usage_owner_commands", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_task_usage_owner_commands_payload_shape"),
        "task_usage_owner_commands",
        "action IN ('continue','resume','resolve','set_policy','override_checkpoint') "
        "AND jsonb_typeof(command_payload) = 'object' AND jsonb_typeof(warnings) = 'array' "
        "AND affected_snapshot_digest ~ '^[0-9a-f]{64}$'",
    )
    op.drop_constraint(op.f("ck_task_usage_checkpoints_continuation_shape"), "task_usage_checkpoints", type_="check")
    op.drop_column("task_usage_checkpoints", "monitoring_policy_revision")
    op.drop_column("task_usage_checkpoints", "usage_pauses_disabled")
    op.drop_column("task_usage_checkpoints", "next_checkpoint_threshold_tokens")
    op.execute("DROP TRIGGER trg_task_usage_checkpoint_generation ON task_usage_checkpoints")
    op.execute("DROP FUNCTION task_usage_reject_generation_change()")
    op.drop_constraint(op.f("ck_task_usage_checkpoints_state_shape"), "task_usage_checkpoints", type_="check")
    op.create_check_constraint(
        op.f("ck_task_usage_checkpoints_state_shape"),
        "task_usage_checkpoints",
        "version >= 0 AND state IN ('checkpoint_requested','pausing','paused','resumed','resolved') "
        "AND (state NOT IN ('paused','resumed') OR process_settled)",
    )
    op.drop_column("task_usage_checkpoints", "generation")
    op.drop_table("task_usage_subject_admissions")
    for dimension in ("cached_input", "output", "input"):
        op.drop_column("model_usage", f"{dimension}_tokens_complete")
