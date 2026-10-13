"""Retain bounded content-free diagnostics per authoring attempt.

Revision ID: 20261012_0041
Revises: 20261011_0040
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261012_0041"
down_revision = "20261011_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "epic_brainstorm_attempts",
        sa.Column(
            "diagnostics", JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE epic_brainstorm_attempts IN ACCESS EXCLUSIVE MODE")
    retained = op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM epic_brainstorm_attempts "
        "WHERE jsonb_array_length(coalesce(diagnostics->'events', '[]'::jsonb)) > 0 "
        "OR coalesce((diagnostics->>'dropped_events')::integer, 0) > 0)"
    )).scalar()
    if retained:
        raise RuntimeError("durable brainstorm diagnostics must not be discarded")
    op.drop_column("epic_brainstorm_attempts", "diagnostics")
