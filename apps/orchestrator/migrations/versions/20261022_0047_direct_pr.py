"""Direct PR publication fields and PUBLISHING/PR_OPEN states (Slice D).

Revision ID: 20261022_0047
Revises: 20261022_0046
"""

import sqlalchemy as sa
from alembic import op

revision = "20261022_0047"
down_revision = "20261022_0046"
branch_labels = None
depends_on = None

_OLD_STATES = (
    "state IN ('QUEUED','RUNNING','FINALIZING','CHECKING','REVIEWING','REPAIRING',"
    "'DONE','FAILED','CANCELLED','NEEDS_DECISION')"
)
_NEW_STATES = (
    "state IN ('QUEUED','RUNNING','FINALIZING','CHECKING','REVIEWING','REPAIRING',"
    "'PUBLISHING','PR_OPEN','DONE','FAILED','CANCELLED','NEEDS_DECISION')"
)


def upgrade() -> None:
    op.add_column("direct_runs", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("direct_runs", sa.Column("pr_url", sa.String(1024), nullable=True))
    op.add_column(
        "direct_runs",
        sa.Column("pr_rounds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _NEW_STATES)


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM direct_runs WHERE pr_number IS NOT NULL "
                "OR pr_url IS NOT NULL OR pr_rounds <> 0 LIMIT 1"
            )
        )
        .scalar()
    )
    if retained is not None:
        raise RuntimeError("durable direct PR data must not be discarded")
    publishing = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM direct_runs WHERE state IN ('PUBLISHING','PR_OPEN') LIMIT 1"
            )
        )
        .scalar()
    )
    if publishing is not None:
        raise RuntimeError("runs in PUBLISHING/PR_OPEN state must not be discarded")
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _OLD_STATES)
    op.drop_column("direct_runs", "pr_rounds")
    op.drop_column("direct_runs", "pr_url")
    op.drop_column("direct_runs", "pr_number")
