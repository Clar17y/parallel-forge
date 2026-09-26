"""Bind admitted subscription attempts to exact lease and frozen context."""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0013"
down_revision = "20260910_0012"
branch_labels = depends_on = None


def upgrade():
    for name, kind in (
        ("lease_owner", sa.String(255)),
        ("lease_generation", sa.Integer()),
        ("candidate_epoch", sa.Integer()),
        ("envelope_digest", sa.String(64)),
        ("task_digest", sa.String(64)),
    ):
        op.add_column("subscription_attempts", sa.Column(name, kind, nullable=True))
    op.create_check_constraint(
        "execution_binding",
        "subscription_attempts",
        "(lease_owner IS NULL AND lease_generation IS NULL AND candidate_epoch IS NULL AND envelope_digest IS NULL AND task_digest IS NULL) OR (lease_owner IS NOT NULL AND lease_generation IS NOT NULL AND lease_generation > 0 AND candidate_epoch IS NOT NULL AND candidate_epoch >= 0 AND envelope_digest IS NOT NULL AND task_digest IS NOT NULL)",
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM subscription_attempts WHERE lease_owner IS NOT NULL)"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("cannot discard admitted subscription execution identity")
    op.drop_constraint("execution_binding", "subscription_attempts", type_="check")
    for name in (
        "task_digest",
        "envelope_digest",
        "candidate_epoch",
        "lease_generation",
        "lease_owner",
    ):
        op.drop_column("subscription_attempts", name)
