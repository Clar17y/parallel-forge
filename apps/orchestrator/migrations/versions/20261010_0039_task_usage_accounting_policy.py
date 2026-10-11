"""Add explicit counter epochs and policy/reporting snapshot controls.

Revision ID: 20261010_0039
Revises: 20261010_0038
"""

import sqlalchemy as sa
from alembic import op

revision = "20261010_0039"
down_revision = "20261010_0038"
branch_labels = None
depends_on = None


def _baseline_constraints(manual: bool) -> None:
    states = (
        "'ready','insufficient_history','manual'" if manual else "'ready','insufficient_history'"
    )
    ready_states = "'ready','manual'" if manual else "'ready'"
    extra = (
        (
            " OR (status = 'manual' AND sample_count = 0 AND coverage_count = 0 AND outcome_count = 0 "
            "AND window_started_at IS NULL AND window_ended_at IS NULL AND reference_tokens > 0 "
            "AND median_tokens IS NULL AND p90_tokens IS NULL AND mean_tokens IS NULL "
            "AND warning_threshold_tokens IS NOT NULL AND checkpoint_threshold_tokens IS NOT NULL)"
        )
        if manual
        else ""
    )
    for name in ("sample_shape", "reference_shape", "threshold_shape"):
        op.drop_constraint(
            op.f(f"ck_task_usage_baselines_{name}"), "task_usage_baselines", type_="check"
        )
    op.create_check_constraint(
        op.f("ck_task_usage_baselines_sample_shape"),
        "task_usage_baselines",
        f"sample_count >= 0 AND status IN ({states}) AND history_digest ~ '^[0-9a-f]{{64}}$'",
    )
    op.create_check_constraint(
        op.f("ck_task_usage_baselines_reference_shape"),
        "task_usage_baselines",
        f"(status IN ({ready_states}) AND reference_tokens IS NOT NULL AND reference_tokens >= 0) "
        "OR (status = 'insufficient_history' AND reference_tokens IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_task_usage_baselines_threshold_shape"),
        "task_usage_baselines",
        "(warning_threshold_tokens IS NULL OR (warning_threshold_tokens >= 0 AND warning_threshold_tokens < 'Infinity'::float8)) "
        "AND (checkpoint_threshold_tokens IS NULL OR (checkpoint_threshold_tokens >= 0 AND checkpoint_threshold_tokens < 'Infinity'::float8)) "
        "AND (warning_threshold_tokens IS NULL OR checkpoint_threshold_tokens IS NULL OR checkpoint_threshold_tokens >= warning_threshold_tokens) "
        "AND ((status = 'insufficient_history' AND warning_threshold_tokens IS NULL AND checkpoint_threshold_tokens IS NULL) "
        "OR (status = 'ready' AND sample_count >= minimum_comparables AND window_started_at IS NOT NULL "
        "AND median_tokens IS NOT NULL AND p90_tokens IS NOT NULL AND mean_tokens IS NOT NULL "
        "AND warning_threshold_tokens IS NOT NULL AND checkpoint_threshold_tokens IS NOT NULL)"
        + extra
        + ")",
    )


def upgrade() -> None:
    # Existing zero and finite caps retain their meaning; NULL represents uncapped.
    op.alter_column(
        "runs", "token_budget", existing_type=sa.Integer(), type_=sa.BigInteger(), nullable=True
    )
    for name in ("monitoring_policy_revision", "hard_token_cap"):
        op.add_column("agent_executions", sa.Column(name, sa.BigInteger(), nullable=True))
        op.create_check_constraint(
            op.f(f"ck_agent_executions_{name}"), "agent_executions", f"{name} IS NULL OR {name} > 0"
        )
    op.execute("""
        CREATE FUNCTION task_usage_preserve_admission_settings() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
               OLD.monitoring_policy_revision IS DISTINCT FROM NEW.monitoring_policy_revision
               OR OLD.hard_token_cap IS DISTINCT FROM NEW.hard_token_cap) THEN
                RAISE EXCEPTION 'admitted monitoring settings are immutable' USING ERRCODE = '23514';
            END IF;
            IF NEW.monitoring_policy_revision IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM runs r JOIN task_usage_policy_revisions p ON p.project_id = r.project_id
                WHERE r.id = NEW.run_id AND p.revision = NEW.monitoring_policy_revision
            ) THEN
                RAISE EXCEPTION 'admitted policy revision does not belong to the run project' USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER trg_task_usage_admission_settings_immutable BEFORE INSERT OR UPDATE OF monitoring_policy_revision, hard_token_cap ON agent_executions FOR EACH ROW EXECUTE FUNCTION task_usage_preserve_admission_settings()"
    )
    op.add_column(
        "task_usage_observations",
        sa.Column("counter_epoch", sa.String(96), nullable=False, server_default="source"),
    )
    op.create_check_constraint(
        op.f("ck_task_usage_observations_counter_epoch"),
        "task_usage_observations",
        "length(btrim(counter_epoch)) BETWEEN 1 AND 96",
    )
    # Reconciliation identity may arrive before its referenced live event.
    op.drop_constraint(
        op.f("fk_task_usage_observations_work_unit_id_task_usage_observations"),
        "task_usage_observations",
        type_="foreignkey",
    )
    op.add_column(
        "task_usage_policy_revisions",
        sa.Column("manual_estimate_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "task_usage_policy_revisions", sa.Column("window_days", sa.BigInteger(), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_task_usage_policy_revisions_window_days"),
        "task_usage_policy_revisions",
        "window_days IS NULL OR window_days > 0",
    )
    op.add_column(
        "task_usage_policy_revisions",
        sa.Column(
            "allow_cross_project_fallback", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.create_check_constraint(
        op.f("ck_task_usage_policy_revisions_manual_estimate"),
        "task_usage_policy_revisions",
        "manual_estimate_tokens IS NULL OR manual_estimate_tokens > 0",
    )
    for name in ("coverage_count", "outcome_count", "source_count"):
        op.add_column(
            "task_usage_baselines",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
        )
    # Preserve the measured counters in existing frozen payloads, if recorded.
    op.execute(
        "ALTER TABLE task_usage_baselines DISABLE TRIGGER trg_task_usage_baselines_immutable"
    )
    op.execute(
        "UPDATE task_usage_baselines SET coverage_count = COALESCE((frozen_payload->>'coverage_count')::bigint, 0), outcome_count = COALESCE((frozen_payload->>'outcome_count')::bigint, sample_count), source_count = COALESCE((frozen_payload->>'source_count')::bigint, 0)"
    )
    op.execute("ALTER TABLE task_usage_baselines ENABLE TRIGGER trg_task_usage_baselines_immutable")
    op.create_check_constraint(
        op.f("ck_task_usage_baselines_coverage_shape"),
        "task_usage_baselines",
        "coverage_count >= 0 AND outcome_count >= 0 AND source_count >= 0 AND coverage_count <= sample_count AND coverage_count <= outcome_count",
    )
    _baseline_constraints(True)


def downgrade() -> None:
    # Keep evidence and owner choices intact; downgrade only a representable snapshot.
    # Lock before inspecting so a concurrent writer cannot add evidence behind the guard.
    op.execute(
        "LOCK TABLE task_usage_work_units, task_usage_policy_revisions, task_usage_observations, task_usage_baselines, task_usage_checkpoints, task_usage_owner_commands, runs, agent_executions IN ACCESS EXCLUSIVE MODE"
    )
    incompatible = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM task_usage_observations WHERE counter_epoch <> 'source' "
                "OR (reconciles_event_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM task_usage_observations live "
                "WHERE live.work_unit_id = task_usage_observations.work_unit_id AND live.event_id = task_usage_observations.reconciles_event_id))) "
                "OR EXISTS (SELECT 1 FROM task_usage_policy_revisions WHERE manual_estimate_tokens IS NOT NULL OR window_days IS NOT NULL OR NOT allow_cross_project_fallback) "
                "OR EXISTS (SELECT 1 FROM task_usage_baselines WHERE status = 'manual' OR outcome_count <> sample_count OR coverage_count <> sample_count "
                "OR source_count <> COALESCE((frozen_payload->>'source_count')::bigint, 0)) "
                "OR EXISTS (SELECT 1 FROM runs WHERE token_budget IS NULL OR token_budget > 2147483647) "
                "OR EXISTS (SELECT 1 FROM agent_executions WHERE monitoring_policy_revision IS NOT NULL OR hard_token_cap IS NOT NULL)"
            )
        )
        .scalar_one()
    )
    if incompatible:
        raise RuntimeError(
            "Cannot downgrade task usage accounting without losing retained evidence or owner settings"
        )
    _baseline_constraints(False)
    op.drop_constraint(
        op.f("ck_task_usage_baselines_coverage_shape"), "task_usage_baselines", type_="check"
    )
    for name in ("source_count", "outcome_count", "coverage_count"):
        op.drop_column("task_usage_baselines", name)
    op.drop_constraint(
        op.f("ck_task_usage_policy_revisions_manual_estimate"),
        "task_usage_policy_revisions",
        type_="check",
    )
    op.drop_column("task_usage_policy_revisions", "allow_cross_project_fallback")
    op.drop_column("task_usage_policy_revisions", "manual_estimate_tokens")
    op.drop_constraint(
        op.f("ck_task_usage_policy_revisions_window_days"),
        "task_usage_policy_revisions",
        type_="check",
    )
    op.drop_column("task_usage_policy_revisions", "window_days")
    op.create_foreign_key(
        op.f("fk_task_usage_observations_work_unit_id_task_usage_observations"),
        "task_usage_observations",
        "task_usage_observations",
        ["work_unit_id", "reconciles_event_id"],
        ["work_unit_id", "event_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        op.f("ck_task_usage_observations_counter_epoch"), "task_usage_observations", type_="check"
    )
    op.drop_column("task_usage_observations", "counter_epoch")
    op.alter_column(
        "runs", "token_budget", existing_type=sa.BigInteger(), type_=sa.Integer(), nullable=False
    )
    op.execute("DROP TRIGGER trg_task_usage_admission_settings_immutable ON agent_executions")
    op.execute("DROP FUNCTION task_usage_preserve_admission_settings()")
    for name in ("hard_token_cap", "monitoring_policy_revision"):
        op.drop_constraint(op.f(f"ck_agent_executions_{name}"), "agent_executions", type_="check")
        op.drop_column("agent_executions", name)
