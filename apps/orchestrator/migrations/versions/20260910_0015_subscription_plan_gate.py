"""add immutable subscription plan approval proposal bindings

Revision ID: 20260910_0015
Revises: 20260910_0014
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260910_0015"
down_revision = "20260910_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_plan_gates",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("budget_digest", sa.String(64), nullable=False),
        sa.Column("route_digest", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["attempt_id"], ["subscription_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["subscription_tasks.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_subscription_plan_gates_run_id", "subscription_plan_gates", ["run_id"])


def downgrade() -> None:
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM subscription_plan_gates")).scalar()
    if count:
        raise RuntimeError("subscription plan gate evidence is durable and must not be discarded")
    op.drop_table("subscription_plan_gates")
