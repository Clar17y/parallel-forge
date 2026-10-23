"""Direct reviews and REVIEWING state (Slice C).

Revision ID: 20261022_0046
Revises: 20261022_0045
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261022_0046"
down_revision = "20261022_0045"
branch_labels = None
depends_on = None

_OLD_STATES = (
    "state IN ('QUEUED','RUNNING','FINALIZING','CHECKING','REPAIRING',"
    "'DONE','FAILED','CANCELLED','NEEDS_DECISION')"
)
_NEW_STATES = (
    "state IN ('QUEUED','RUNNING','FINALIZING','CHECKING','REVIEWING','REPAIRING',"
    "'DONE','FAILED','CANCELLED','NEEDS_DECISION')"
)


def upgrade() -> None:
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _NEW_STATES)
    op.drop_constraint("direct_sessions_kind", "direct_sessions", type_="check")
    op.create_check_constraint(
        "direct_sessions_kind", "direct_sessions", "kind IN ('implement','repair','review')"
    )
    op.create_table(
        "direct_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("direct_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id", sa.Uuid(),
            sa.ForeignKey("direct_sessions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("client", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("summary", sa.Text()),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "reviewer_modified_worktree", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("verdict IN ('clean','blocking')", name="direct_reviews_verdict"),
    )
    op.create_index(
        "ix_direct_reviews_run_round", "direct_reviews", ["run_id", "round", "created_at"]
    )


def downgrade() -> None:
    retained = (
        op.get_bind().execute(sa.text("SELECT 1 FROM direct_reviews LIMIT 1")).scalar()
    )
    if retained is not None:
        raise RuntimeError("durable direct reviews must not be discarded")
    reviewing = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM direct_runs WHERE state='REVIEWING' LIMIT 1"))
        .scalar()
    )
    if reviewing is not None:
        raise RuntimeError("runs in REVIEWING state must not be discarded")
    op.drop_table("direct_reviews")
    op.drop_constraint("direct_sessions_kind", "direct_sessions", type_="check")
    op.create_check_constraint(
        "direct_sessions_kind", "direct_sessions", "kind IN ('implement','repair')"
    )
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _OLD_STATES)
