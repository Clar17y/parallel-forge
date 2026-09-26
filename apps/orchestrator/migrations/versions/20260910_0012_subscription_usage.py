"""Persist separate attempt reservations and immutable usage.

Revision ID: 20260910_0012
Revises: 20260910_0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260910_0012"
down_revision = "20260910_0011"
branch_labels = depends_on = None


def _timestamps():
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def upgrade():
    op.create_table(
        "subscription_attempt_reservations",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("budget_payload", postgresql.JSONB(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ("run_id", "task_id", "attempt_id"),
            (
                "subscription_attempts.run_id",
                "subscription_attempts.task_row_id",
                "subscription_attempts.id",
            ),
            ondelete="CASCADE",
            name="fk_subscription_attempt_reservation_lineage",
        ),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_subscription_attempt_reservation_key"
        ),
    )
    op.create_table(
        "subscription_attempt_consumption",
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempt_reservations.attempt_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("telemetry_payload", postgresql.JSONB(none_as_null=True)),
        sa.Column("observed", postgresql.JSONB(), nullable=False),
        sa.Column("charged", postgresql.JSONB(), nullable=False),
        sa.Column("unknown_fields", postgresql.JSONB(), nullable=False),
        sa.Column("exceeded_fields", postgresql.JSONB(), nullable=False),
        sa.Column("policy_violations", postgresql.JSONB(), nullable=False),
        sa.Column("uncertain", sa.Boolean(), nullable=False),
        *_timestamps(),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM subscription_attempt_reservations)"))
        .scalar_one()
    ):
        raise RuntimeError("cannot discard admitted subscription usage")
    op.drop_table("subscription_attempt_consumption")
    op.drop_table("subscription_attempt_reservations")
