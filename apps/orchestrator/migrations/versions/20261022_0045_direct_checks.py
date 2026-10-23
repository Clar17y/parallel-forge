"""Direct checks and session kind (Slice B).

Revision ID: 20261022_0045
Revises: 20261022_0044
"""

import sqlalchemy as sa
from alembic import op

revision = "20261022_0045"
down_revision = "20261022_0044"
branch_labels = None
depends_on = None

_OLD_STATES = "state IN ('QUEUED','RUNNING','FINALIZING','DONE','FAILED','CANCELLED','NEEDS_DECISION')"
_NEW_STATES = (
    "state IN ('QUEUED','RUNNING','FINALIZING','CHECKING','REPAIRING',"
    "'DONE','FAILED','CANCELLED','NEEDS_DECISION')"
)


def upgrade() -> None:
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _NEW_STATES)
    op.add_column(
        "direct_sessions",
        sa.Column("kind", sa.String(16), nullable=False, server_default="implement"),
    )
    op.create_check_constraint(
        "direct_sessions_kind", "direct_sessions", "kind IN ('implement','repair')"
    )
    op.create_table(
        "direct_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("direct_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("command_name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("tree_sha", sa.String(64), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("failure_excerpt", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('PASSED','FAILED','ERROR','CACHED')", name="direct_checks_status"
        ),
    )
    op.create_index(
        "ix_direct_checks_run_round", "direct_checks", ["run_id", "round", "created_at"]
    )
    op.create_index(
        "ix_direct_checks_cache",
        "direct_checks",
        ["run_id", "tree_sha", "command_digest", "policy_version"],
    )


def downgrade() -> None:
    retained = (
        op.get_bind().execute(sa.text("SELECT 1 FROM direct_checks LIMIT 1")).scalar()
    )
    if retained is not None:
        raise RuntimeError("durable direct checks must not be discarded")
    op.drop_table("direct_checks")
    op.drop_constraint("direct_sessions_kind", "direct_sessions", type_="check")
    op.drop_column("direct_sessions", "kind")
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _OLD_STATES)
