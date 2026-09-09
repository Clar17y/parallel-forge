"""evaluation baselines persistence

Revision ID: 20260909_0005
Revises: 20260908_0007
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260909_0005"
down_revision = "20260908_0007"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "evaluation_baselines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("suite_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("fixture_version", sa.String(64), nullable=False),
        sa.Column("metric_version", sa.String(64), nullable=False),
        sa.Column("cases", postgresql.JSONB(), nullable=False),
        sa.Column("floors", postgresql.JSONB(), nullable=False),
        sa.Column("ceilings", postgresql.JSONB(), nullable=False),
        sa.Column("promoted_by", sa.String(128), nullable=False),
        sa.Column(
            "promoted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "name", "fixture_version", "metric_version", name="uq_evaluation_baseline_name_versions"
        ),
        sa.UniqueConstraint("suite_id", name="uq_evaluation_baseline_suite"),
        sa.CheckConstraint("btrim(name) <> ''", name="evaluation_baseline_name_nonempty"),
        sa.CheckConstraint(
            "btrim(fixture_version) <> '' AND btrim(metric_version) <> ''",
            name="evaluation_baseline_versions_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id", "fixture_version", "metric_version"],
            [
                "evaluation_suites.id",
                "evaluation_suites.fixture_version",
                "evaluation_suites.metric_version",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_baseline_suite_binding",
        ),
    )
    op.execute(
        "CREATE FUNCTION forge_reject_evaluation_baseline_identity_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'evaluation baseline is immutable'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.suite_id <> OLD.suite_id OR NEW.name <> OLD.name "
        "OR NEW.fixture_version <> OLD.fixture_version OR NEW.metric_version <> OLD.metric_version "
        "OR NEW.cases <> OLD.cases OR NEW.floors <> OLD.floors OR NEW.ceilings <> OLD.ceilings "
        "OR NEW.promoted_by <> OLD.promoted_by OR NEW.promoted_at <> OLD.promoted_at "
        "THEN RAISE EXCEPTION 'evaluation baseline identity is immutable'; END IF; "
        "RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER trg_evaluation_baselines_identity_immutable "
        "BEFORE UPDATE OR DELETE ON evaluation_baselines FOR EACH ROW EXECUTE FUNCTION "
        "forge_reject_evaluation_baseline_identity_mutation()"
    )


def downgrade():
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evaluation_baselines_identity_immutable ON evaluation_baselines"
    )
    op.execute("DROP FUNCTION IF EXISTS forge_reject_evaluation_baseline_identity_mutation()")
    op.drop_table("evaluation_baselines")
