"""evaluation baseline schema version

Revision ID: 20260909_0006
Revises: 20260909_0005
"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0006"
down_revision = "20260909_0005"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "evaluation_baselines",
        sa.Column(
            "snapshot_schema_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        "evaluation_baseline_snapshot_version",
        "evaluation_baselines",
        "snapshot_schema_version = 1",
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION forge_reject_evaluation_baseline_identity_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'evaluation baseline is immutable'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.suite_id <> OLD.suite_id OR NEW.name <> OLD.name "
        "OR NEW.fixture_version <> OLD.fixture_version OR NEW.metric_version <> OLD.metric_version "
        "OR NEW.cases <> OLD.cases OR NEW.floors <> OLD.floors OR NEW.ceilings <> OLD.ceilings "
        "OR NEW.promoted_by <> OLD.promoted_by OR NEW.promoted_at <> OLD.promoted_at "
        "OR NEW.snapshot_schema_version <> OLD.snapshot_schema_version "
        "THEN RAISE EXCEPTION 'evaluation baseline identity is immutable'; END IF; "
        "RETURN NEW; END; $$"
    )


def downgrade():
    op.execute(
        "CREATE OR REPLACE FUNCTION forge_reject_evaluation_baseline_identity_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'evaluation baseline is immutable'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.suite_id <> OLD.suite_id OR NEW.name <> OLD.name "
        "OR NEW.fixture_version <> OLD.fixture_version OR NEW.metric_version <> OLD.metric_version "
        "OR NEW.cases <> OLD.cases OR NEW.floors <> OLD.floors OR NEW.ceilings <> OLD.ceilings "
        "OR NEW.promoted_by <> OLD.promoted_by OR NEW.promoted_at <> OLD.promoted_at "
        "THEN RAISE EXCEPTION 'evaluation baseline identity is immutable'; END IF; "
        "RETURN NEW; END; $$"
    )
    op.drop_constraint(
        "evaluation_baseline_snapshot_version",
        "evaluation_baselines",
        type_="check",
    )
    op.drop_column("evaluation_baselines", "snapshot_schema_version")
