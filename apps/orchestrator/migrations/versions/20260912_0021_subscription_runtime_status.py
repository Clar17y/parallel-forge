"""Add diagnostic worker registration snapshots without admission authority.

Revision ID: 20260912_0021
Revises: 20260912_0020
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260912_0021"
down_revision = "20260912_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_worker_status",
        sa.Column("worker_instance_id", sa.Uuid(), primary_key=True),
        sa.Column("routes", postgresql.JSONB(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "jsonb_typeof(routes) = 'array' AND jsonb_array_length(routes) <= 64",
            name="runtime_routes_bound",
        ),
        sa.CheckConstraint(
            "stopped_at IS NULL OR stopped_at >= last_seen_at", name="runtime_stop_order"
        ),
    )
    op.create_index(
        "ix_subscription_worker_status_last_seen_at", "subscription_worker_status", ["last_seen_at"]
    )


def downgrade() -> None:
    # Diagnostic observations grant no authority and reference no run evidence.
    # Retained quota, launch and task-state rollback guards remain unchanged.
    op.drop_table("subscription_worker_status")
