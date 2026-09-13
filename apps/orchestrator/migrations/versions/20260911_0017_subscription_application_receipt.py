"""Retain application evidence separately from the immutable provider result.

Revision ID: 20260911_0017
Revises: 20260911_0016
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260911_0017"
down_revision = "20260911_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscription_attempt_results", sa.Column("application_digest", sa.String(64)))
    op.add_column(
        "subscription_attempt_results", sa.Column("application_payload", postgresql.JSONB())
    )
    op.create_check_constraint(
        "application_receipt_pair",
        "subscription_attempt_results",
        "(application_digest IS NULL) = (application_payload IS NULL)",
    )


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM subscription_attempt_results WHERE application_payload IS NOT NULL LIMIT 1"
            )
        )
        .scalar()
    )
    if retained is not None:
        raise RuntimeError("subscription application evidence must not be discarded")
    op.drop_constraint("application_receipt_pair", "subscription_attempt_results", type_="check")
    op.drop_column("subscription_attempt_results", "application_payload")
    op.drop_column("subscription_attempt_results", "application_digest")
