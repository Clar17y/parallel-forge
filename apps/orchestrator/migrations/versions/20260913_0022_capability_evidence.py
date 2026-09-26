"""Persist trusted official-client capability evidence.

Revision ID: 20260913_0022
Revises: 20260912_0021
"""

import sqlalchemy as sa
from alembic import op

revision = "20260913_0022"
down_revision = "20260912_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("identity_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "artifact_id",
            sa.Uuid(),
            sa.ForeignKey("artifacts.id", ondelete="RESTRICT"),
            unique=True,
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name="capability_evidence_revision_positive"),
        sa.CheckConstraint(
            "identity_digest ~ '^[0-9a-f]{64}$'",
            name="capability_evidence_identity_digest",
        ),
        sa.CheckConstraint("expires_at > observed_at", name="capability_evidence_validity_window"),
        sa.CheckConstraint(
            "invalidated_at IS NULL OR invalidated_at >= observed_at",
            name="capability_evidence_invalidation_order",
        ),
    )
    op.create_index(
        "uq_capability_evidence_active_identity",
        "capability_evidence",
        ["identity_digest"],
        unique=True,
        postgresql_where=sa.text("invalidated_at IS NULL"),
    )
    op.create_index(
        "uq_capability_evidence_identity_revision",
        "capability_evidence",
        ["identity_digest", "revision"],
        unique=True,
    )


def downgrade() -> None:
    retained = op.get_bind().execute(sa.text("SELECT 1 FROM capability_evidence LIMIT 1")).scalar()
    if retained is not None:
        raise RuntimeError("trusted capability evidence must not be discarded")
    op.drop_table("capability_evidence")
