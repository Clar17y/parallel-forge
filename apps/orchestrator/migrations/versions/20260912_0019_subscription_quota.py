"""Persist shared quota evidence and one recovery probe per account pool.

Revision ID: 20260912_0019
Revises: 20260911_0018
"""

import sqlalchemy as sa
from alembic import op

revision = "20260912_0019"
down_revision = "20260911_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_quota_pools",
        sa.Column("provider", sa.String(96), primary_key=True),
        sa.Column("account", sa.String(96), primary_key=True),
        sa.Column("pool", sa.String(96), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(128)),
        sa.Column("reset_at", sa.DateTime(timezone=True)),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True)),
        sa.Column("retry_basis", sa.String(32)),
        sa.Column("probe_attempt_id", sa.Uuid()),
        sa.Column("recovered_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("revision >= 0", name="quota_revision"),
        sa.CheckConstraint(
            "retry_basis IS NULL OR retry_basis IN ('known_reset','probe_cooldown')",
            name="quota_retry_basis",
        ),
    )
    op.create_table(
        "subscription_quota_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(96), nullable=False),
        sa.Column("account", sa.String(96), nullable=False),
        sa.Column("pool", sa.String(96), nullable=False),
        sa.Column("source_key", sa.String(64), unique=True, nullable=False),
        sa.Column("source_attempt_id", sa.Uuid()),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True)),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_basis", sa.String(32), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ("provider", "account", "pool"),
            (
                "subscription_quota_pools.provider",
                "subscription_quota_pools.account",
                "subscription_quota_pools.pool",
            ),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(source_attempt_id IS NULL) <> (actor_id IS NULL)", name="quota_observation_source"
        ),
    )
    op.create_table(
        "subscription_quota_admissions",
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(96), nullable=False),
        sa.Column("account", sa.String(96), nullable=False),
        sa.Column("pool", sa.String(96), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("probe", sa.Boolean(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ("provider", "account", "pool"),
            (
                "subscription_quota_pools.provider",
                "subscription_quota_pools.account",
                "subscription_quota_pools.pool",
            ),
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM subscription_quota_observations UNION ALL "
                "SELECT 1 FROM subscription_quota_pools WHERE blocked OR probe_attempt_id IS NOT NULL LIMIT 1"
            )
        )
        .scalar()
    )
    if retained is not None:
        raise RuntimeError("durable quota evidence must not be discarded")
    op.drop_table("subscription_quota_admissions")
    op.drop_table("subscription_quota_observations")
    op.drop_table("subscription_quota_pools")
