"""Persist immutable attempt results and scheduling repair debits."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260910_0014"
down_revision = "20260910_0013"
branch_labels = depends_on = None


def _base():
    return [
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.add_column("subscription_attempts", sa.Column("task_version", sa.Integer(), nullable=True))
    op.create_table(
        "subscription_attempt_results",
        *_base(),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "subscription_repair_debits",
        *_base(),
        sa.Column(
            "next_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="RESTRICT"),
            unique=True,
        ),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM subscription_attempt_results) OR EXISTS (SELECT 1 FROM subscription_repair_debits) OR EXISTS (SELECT 1 FROM subscription_attempts WHERE task_version IS NOT NULL)"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("cannot discard subscription result or repair evidence")
    op.drop_table("subscription_repair_debits")
    op.drop_table("subscription_attempt_results")

    op.drop_column("subscription_attempts", "task_version")
