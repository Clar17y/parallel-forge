"""Retain frozen per-run workflow preferences.

Revision ID: 20261020_0042
Revises: 20261012_0041
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261020_0042"
down_revision = "20261012_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "workflow",
            JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "workflow")
