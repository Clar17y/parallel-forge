"""Durable subscription task leases and scheduler admission."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260910_0009"
down_revision = "20260910_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_scheduler_capacity_policies",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("global_limit", sa.Integer(), nullable=False),
        sa.Column("run_limit", sa.Integer(), nullable=False),
        sa.Column("provider_limit", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "global_limit > 0 AND run_limit > 0 AND provider_limit > 0",
            name="scheduler_capacity_positive",
        ),
    )
    op.create_table(
        "subscription_scheduled_effects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("lease_owner", sa.String(255), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("candidate_epoch", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="admitted"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "state IN ('admitted','reconciling','settled','rejected')",
            name="scheduled_effect_state",
        ),
    )
    op.create_table(
        "subscription_scheduler_runs",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("admitted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("candidate_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("capacity_policy_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("effective_run_limit", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_claimed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "subscription_scheduled_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("parent_task_id", sa.Uuid()),
        sa.Column("worktree_id", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(96), nullable=False),
        sa.Column(
            "owned_paths", postgresql.ARRAY(sa.String(1024)), nullable=False, server_default="{}"
        ),
        sa.Column(
            "dependency_task_ids", postgresql.ARRAY(sa.Uuid()), nullable=False, server_default="{}"
        ),
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_repairs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("repairs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("pause_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("run_id", "task_id", name="uq_scheduled_task"),
        sa.CheckConstraint(
            "state IN ('queued','leased','blocked','reconciling','terminal')",
            name="scheduled_task_state",
        ),
    )
    op.create_index(
        "ix_subscription_scheduled_tasks_run_id", "subscription_scheduled_tasks", ["run_id"]
    )
    op.create_index(
        "ix_subscription_scheduled_tasks_state", "subscription_scheduled_tasks", ["state"]
    )


def downgrade() -> None:
    op.drop_table("subscription_scheduled_tasks")
    op.drop_table("subscription_scheduler_runs")
    op.drop_table("subscription_scheduled_effects")
    op.drop_table("subscription_scheduler_capacity_policies")
