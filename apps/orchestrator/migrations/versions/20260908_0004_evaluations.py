"""evaluation persistence

Revision ID: 20260908_0004
Revises: 20260906_0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260908_0004"
down_revision = "20260906_0003"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "evaluation_suites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("fixture_version", sa.String(64), nullable=False),
        sa.Column("metric_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "id", "fixture_version", "metric_version", name="uq_evaluation_suite_binding"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_evaluation_suite_idempotency"),
        sa.CheckConstraint("btrim(name) <> ''", name="evaluation_suite_name_nonempty"),
        sa.CheckConstraint(
            "btrim(fixture_version) <> '' AND btrim(metric_version) <> ''",
            name="evaluation_suite_versions_nonempty",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','passed','failed','cancelled')",
            name="evaluation_suite_status",
        ),
    )
    op.create_table(
        "evaluation_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("suite_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_key", sa.String(255), nullable=False),
        sa.Column("fixture_version", sa.String(64), nullable=False),
        sa.Column("metric_version", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("metrics_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "model_usage_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_usage.id", ondelete="RESTRICT"),
        ),
        sa.Column("input_artifact_digest", sa.String(64)),
        sa.Column("output_artifact_digest", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("suite_id", "case_key", name="uq_evaluation_cases_suite_key"),
        sa.CheckConstraint("btrim(case_key) <> ''", name="evaluation_case_key_nonempty"),
        sa.CheckConstraint(
            "fixture_version <> '' AND metric_version <> ''",
            name="evaluation_case_versions_nonempty",
        ),
        sa.CheckConstraint(
            "role IN ('planner','developer','reviewer')", name="evaluation_case_role"
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','passed','failed','skipped')",
            name="evaluation_case_status",
        ),
        sa.CheckConstraint("metrics_schema_version >= 1", name="evaluation_case_metrics_version"),
        sa.CheckConstraint(
            "input_artifact_digest IS NULL OR input_artifact_digest ~ '^[0-9a-f]{64}$'",
            name="evaluation_case_input_digest",
        ),
        sa.CheckConstraint(
            "output_artifact_digest IS NULL OR output_artifact_digest ~ '^[0-9a-f]{64}$'",
            name="evaluation_case_output_digest",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id", "fixture_version", "metric_version"],
            [
                "evaluation_suites.id",
                "evaluation_suites.fixture_version",
                "evaluation_suites.metric_version",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_case_suite_binding",
        ),
    )
    op.execute(
        "CREATE FUNCTION forge_reject_evaluation_case_identity_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.id <> OLD.id OR NEW.suite_id <> OLD.suite_id OR NEW.case_key <> OLD.case_key "
        "OR NEW.fixture_version <> OLD.fixture_version OR NEW.metric_version <> OLD.metric_version "
        "THEN RAISE EXCEPTION 'evaluation case identity is immutable'; END IF; "
        "RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER trg_evaluation_cases_identity_immutable "
        "BEFORE UPDATE ON evaluation_cases FOR EACH ROW EXECUTE FUNCTION "
        "forge_reject_evaluation_case_identity_mutation()"
    )

    op.execute(
        "CREATE FUNCTION forge_reject_evaluation_suite_identity_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.id <> OLD.id OR NEW.fixture_version <> OLD.fixture_version "
        "OR NEW.metric_version <> OLD.metric_version OR NEW.idempotency_key <> OLD.idempotency_key "
        "THEN RAISE EXCEPTION 'evaluation suite identity is immutable'; END IF; "
        "RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER trg_evaluation_suites_identity_immutable "
        "BEFORE UPDATE ON evaluation_suites FOR EACH ROW EXECUTE FUNCTION "
        "forge_reject_evaluation_suite_identity_mutation()"
    )


def downgrade():
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evaluation_suites_identity_immutable ON evaluation_suites"
    )
    op.execute("DROP FUNCTION IF EXISTS forge_reject_evaluation_suite_identity_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_evaluation_cases_identity_immutable ON evaluation_cases")
    op.execute("DROP FUNCTION IF EXISTS forge_reject_evaluation_case_identity_mutation()")
    op.drop_table("evaluation_cases")
    op.drop_table("evaluation_suites")
