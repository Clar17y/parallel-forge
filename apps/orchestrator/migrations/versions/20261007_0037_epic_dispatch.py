"""Explicit per-epoch automatic dispatch authority.

Revision ID: 20261007_0037
Revises: 20261007_0036
"""

import sqlalchemy as sa
from alembic import op

revision = "20261007_0037"
down_revision = "20261007_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "epic_dispatch_settings",
        sa.Column("execution_id", sa.Uuid(), sa.ForeignKey("epic_executions.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("epic_id", sa.Uuid(), sa.ForeignKey("epics.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid()),
        sa.Column("profile_version", sa.Integer()),
        sa.Column("claim_item_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("blocker_code", sa.String(96)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("(profile_id IS NULL) = (profile_version IS NULL)", name="profile_pair"),
        sa.CheckConstraint("profile_version IS NULL OR profile_version >= 1", name="profile_version_positive"),
        sa.CheckConstraint("(claim_item_id IS NULL) = (claim_token IS NULL)", name="claim_pair"),
        sa.CheckConstraint("(claim_item_id IS NULL) = (claim_expires_at IS NULL)", name="claim_expiry_pair"),
    )
    op.create_index("ix_epic_dispatch_scan", "epic_dispatch_settings", ["enabled", "checked_at", "execution_id"])


def downgrade() -> None:
    op.execute("LOCK TABLE epic_dispatch_settings IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM epic_dispatch_settings) THEN
                RAISE EXCEPTION 'cannot discard epic dispatch authority' USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.drop_index("ix_epic_dispatch_scan", table_name="epic_dispatch_settings")
    op.drop_table("epic_dispatch_settings")
