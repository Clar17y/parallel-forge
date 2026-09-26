"""Persist safe current capability-probe diagnostics.

Revision ID: 20260920_0024
Revises: 20260916_0023
"""

import sqlalchemy as sa
from alembic import op

revision = "20260920_0024"
down_revision = "20260916_0023"
branch_labels = None
depends_on = None

_REASONS = (
    "'ready','missing_executable','executable_digest_mismatch','version_mismatch',"
    "'unsupported_model_or_effort','signed_out','account_authentication_unproved',"
    "'subscription_route_unbound','isolation_unproved','evidence_stale_or_invalid',"
    "'configuration_invalid','unknown'"
)


def upgrade() -> None:
    op.create_table(
        "capability_probe_diagnostics",
        sa.Column("identity_digest", sa.String(64), primary_key=True),
        sa.Column("reason", sa.String(48), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "identity_digest ~ '^[0-9a-f]{64}$'",
            name="identity",
        ),
        sa.CheckConstraint("revision >= 1", name="revision"),
        sa.CheckConstraint(f"reason IN ({_REASONS})", name="reason"),
        sa.CheckConstraint("expires_at > observed_at", name="window"),
    )


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM capability_probe_diagnostics LIMIT 1"))
        .scalar()
    )
    if retained is not None:
        raise RuntimeError("capability probe diagnostics must not be discarded")
    op.drop_table("capability_probe_diagnostics")
