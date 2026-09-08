"""Persist recovery ownership and close command admission until recovery succeeds."""

from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision = "20260908_0006"
down_revision = "20260908_0005"
branch_labels = depends_on = None


def upgrade():
    table = op.create_table(
        "recovery_barrier",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000001'::uuid",
            name=op.f("ck_recovery_barrier_singleton"),
        ),
        sa.CheckConstraint("generation >= 0", name=op.f("ck_recovery_barrier_generation")),
        sa.CheckConstraint(
            "(owner_id IS NULL) = (expires_at IS NULL)", name=op.f("ck_recovery_barrier_owner_pair")
        ),
        sa.CheckConstraint(
            "owner_id IS NULL OR required", name=op.f("ck_recovery_barrier_owned_required")
        ),
    )
    op.bulk_insert(
        table,
        [{"id": UUID("00000000-0000-0000-0000-000000000001"), "required": False, "generation": 0}],
    )


def downgrade():
    op.drop_table("recovery_barrier")
