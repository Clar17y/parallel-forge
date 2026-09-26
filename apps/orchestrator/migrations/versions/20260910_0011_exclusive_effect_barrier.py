"""Persist whole-worktree effect barriers.

Revision ID: 20260910_0011
Revises: 20260910_0010
"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0011"
down_revision = "20260910_0010"
branch_labels = depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_scheduled_effects",
        sa.Column(
            "whole_worktree_exclusive", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # Pre-0011 rows cannot record whether their already-admitted provider
    # callback was whole-worktree exclusive.  Preserve safety for uncertain
    # effects; terminal evidence needs no barrier.
    op.execute(
        "UPDATE subscription_scheduled_effects SET whole_worktree_exclusive = true "
        "WHERE state IN ('admitted', 'reconciling')"
    )
    op.alter_column(
        "subscription_scheduled_effects", "whole_worktree_exclusive", server_default=None
    )


def downgrade() -> None:
    unsettled = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM subscription_scheduled_effects "
                "WHERE whole_worktree_exclusive AND state IN ('admitted', 'reconciling'))"
            )
        )
        .scalar_one()
    )
    if unsettled:
        raise RuntimeError("cannot discard unsettled exclusive effect barriers")
    op.drop_column("subscription_scheduled_effects", "whole_worktree_exclusive")
