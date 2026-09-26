"""Add bounded worktree observation fences for settled handoffs.

Revision ID: 20260911_0016
Revises: 20260910_0015
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260911_0016"
down_revision = "20260910_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_handoff_fences",
        sa.Column("worktree_id", sa.String(255), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["subscription_attempts.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    active = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM subscription_handoff_fences WHERE expires_at > clock_timestamp() LIMIT 1"
            )
        )
        .scalar()
    )
    if active is not None:
        raise RuntimeError("active handoff observation fences must not be discarded")
    op.drop_table("subscription_handoff_fences")
