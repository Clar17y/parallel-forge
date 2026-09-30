"""Bounded advisory worker model catalogs.

Revision ID: 20260929_0030
Revises: 20260929_0029
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260929_0030"
down_revision = "20260929_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0028 used already-prefixed names under the metadata naming convention.
    # Rename without rebuilding the constraints or changing Jev evidence.
    for suffix in ("status", "usage_nonnegative"):
        op.execute(
            f"ALTER TABLE jev_evaluations RENAME CONSTRAINT "
            f"ck_jev_evaluations_ck_jev_evaluations_{suffix} "
            f"TO ck_jev_evaluations_{suffix}"
        )
    op.add_column(
        "subscription_worker_status",
        sa.Column("model_catalogs", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.create_check_constraint(
        "runtime_model_catalogs_bound",
        "subscription_worker_status",
        "jsonb_typeof(model_catalogs) = 'array' AND jsonb_array_length(model_catalogs) <= 8",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_subscription_worker_status_runtime_model_catalogs_bound"),
        "subscription_worker_status",
    )
    op.drop_column("subscription_worker_status", "model_catalogs")
    for suffix in ("status", "usage_nonnegative"):
        op.execute(
            f"ALTER TABLE jev_evaluations RENAME CONSTRAINT "
            f"ck_jev_evaluations_{suffix} "
            f"TO ck_jev_evaluations_ck_jev_evaluations_{suffix}"
        )
