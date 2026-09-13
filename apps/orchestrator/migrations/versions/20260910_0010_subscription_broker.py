"""Durable broker operation receipts and official-client lifecycle.

Revision ID: 20260910_0010
Revises: 20260910_0009
"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0010"
down_revision = "20260910_0009"
branch_labels = depends_on = None


def upgrade() -> None:
    # v0.1 records retain their execution/step FK pair.  v0.2 records carry
    # the subscription task/attempt pair instead; the XOR prevents a role
    # label from impersonating a legacy AgentExecution.
    op.alter_column("tool_calls", "agent_execution_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("tool_calls", sa.Column("subscription_task_id", sa.Uuid()))
    op.add_column("tool_calls", sa.Column("subscription_attempt_id", sa.Uuid()))
    op.add_column("tool_calls", sa.Column("subscription_purpose", sa.String(64)))
    op.create_foreign_key(
        "fk_tool_call_subscription_task", "tool_calls", "subscription_tasks",
        ["run_id", "subscription_task_id"], ["run_id", "id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_tool_call_subscription_attempt", "tool_calls", "subscription_attempts",
        ["subscription_attempt_id"], ["id"], ondelete="CASCADE",
    )
    op.create_check_constraint(
        "tool_call_authority_lineage_xor", "tool_calls",
        "(agent_execution_id IS NOT NULL AND subscription_task_id IS NULL AND subscription_attempt_id IS NULL AND subscription_purpose IS NULL) OR "
        "(agent_execution_id IS NULL AND subscription_task_id IS NOT NULL AND subscription_attempt_id IS NOT NULL AND subscription_purpose IS NOT NULL)",
    )
    op.create_table(
        "subscription_client_launches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("subscription_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("launch_id", sa.String(255), nullable=False),
        sa.Column("worker_identity", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="intent"),
        sa.Column("pid", sa.Integer()),
        sa.Column("process_start_token", sa.String(255)),
        sa.Column("terminal_payload", sa.dialects.postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("attempt_id", "launch_id", name="uq_subscription_client_launch"),
        sa.CheckConstraint("state IN ('intent','started','terminal','uncertain')", name="state"),
    )


def downgrade() -> None:
    op.drop_constraint("tool_call_authority_lineage_xor", "tool_calls", type_="check")
    op.drop_constraint("fk_tool_call_subscription_attempt", "tool_calls", type_="foreignkey")
    op.drop_constraint("fk_tool_call_subscription_task", "tool_calls", type_="foreignkey")
    op.drop_column("tool_calls", "subscription_purpose")
    op.drop_column("tool_calls", "subscription_attempt_id")
    op.drop_column("tool_calls", "subscription_task_id")
    op.alter_column("tool_calls", "agent_execution_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_table("subscription_client_launches")
