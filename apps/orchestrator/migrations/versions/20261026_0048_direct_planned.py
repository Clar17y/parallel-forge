"""Direct planned shape: plan fields, PLANNING states, plan session kind.

Revision ID: 20261026_0048
Revises: 20261022_0047
"""

import sqlalchemy as sa
from alembic import op

revision = "20261026_0048"
down_revision = "20261022_0047"
branch_labels = None
depends_on = None

_OLD_STATES = (
    "state IN ('QUEUED','RUNNING','FINALIZING','CHECKING','REVIEWING','REPAIRING',"
    "'PUBLISHING','PR_OPEN','DONE','FAILED','CANCELLED','NEEDS_DECISION')"
)
_NEW_STATES = (
    "state IN ('QUEUED','PLANNING','AWAITING_PLAN_APPROVAL','RUNNING','FINALIZING',"
    "'CHECKING','REVIEWING','REPAIRING','PUBLISHING','PR_OPEN','DONE',"
    "'FAILED','CANCELLED','NEEDS_DECISION')"
)
_OLD_KINDS = "kind IN ('implement','repair','review')"
_NEW_KINDS = "kind IN ('implement','repair','review','plan')"


def upgrade() -> None:
    op.add_column(
        "direct_runs",
        sa.Column("shape", sa.String(16), nullable=False, server_default="quick"),
    )
    op.add_column(
        "direct_runs",
        sa.Column("review_plan_first", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("direct_runs", sa.Column("plan_text", sa.Text(), nullable=True))
    op.add_column(
        "direct_runs",
        sa.Column("plan_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "direct_runs_shape", "direct_runs", "shape IN ('quick','planned')"
    )
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _NEW_STATES)
    op.drop_constraint("direct_sessions_kind", "direct_sessions", type_="check")
    op.create_check_constraint("direct_sessions_kind", "direct_sessions", _NEW_KINDS)


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM direct_runs WHERE shape <> 'quick' "
                "OR plan_text IS NOT NULL OR plan_approved OR review_plan_first "
                "OR state IN ('PLANNING','AWAITING_PLAN_APPROVAL') LIMIT 1"
            )
        )
        .scalar()
    )
    if retained is not None:
        raise RuntimeError("durable direct plan data must not be discarded")
    plan_sessions = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM direct_sessions WHERE kind = 'plan' LIMIT 1"))
        .scalar()
    )
    if plan_sessions is not None:
        raise RuntimeError("durable direct plan data must not be discarded")
    op.drop_constraint("direct_sessions_kind", "direct_sessions", type_="check")
    op.create_check_constraint("direct_sessions_kind", "direct_sessions", _OLD_KINDS)
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _OLD_STATES)
    op.drop_constraint("direct_runs_shape", "direct_runs", type_="check")
    op.drop_column("direct_runs", "plan_approved")
    op.drop_column("direct_runs", "plan_text")
    op.drop_column("direct_runs", "review_plan_first")
    op.drop_column("direct_runs", "shape")
