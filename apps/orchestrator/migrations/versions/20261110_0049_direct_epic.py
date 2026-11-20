"""Direct epic shape: direct_tasks table, epic shape, DECOMPOSING states, decompose session kind.

Revision ID: 20261110_0049
Revises: 20261026_0048
"""

import sqlalchemy as sa
from alembic import op

revision = "20261110_0049"
down_revision = "20261026_0048"
branch_labels = None
depends_on = None

_OLD_STATES = (
    "state IN ('QUEUED','PLANNING','AWAITING_PLAN_APPROVAL','RUNNING','FINALIZING',"
    "'CHECKING','REVIEWING','REPAIRING','PUBLISHING','PR_OPEN','DONE',"
    "'FAILED','CANCELLED','NEEDS_DECISION')"
)
_NEW_STATES = (
    "state IN ('QUEUED','PLANNING','AWAITING_PLAN_APPROVAL','DECOMPOSING',"
    "'AWAITING_DECOMPOSITION_APPROVAL','RUNNING','FINALIZING',"
    "'CHECKING','REVIEWING','REPAIRING','PUBLISHING','PR_OPEN','DONE',"
    "'FAILED','CANCELLED','NEEDS_DECISION')"
)
_OLD_SHAPES = "shape IN ('quick','planned')"
_NEW_SHAPES = "shape IN ('quick','planned','epic')"
_OLD_KINDS = "kind IN ('implement','repair','review','plan')"
_NEW_KINDS = "kind IN ('implement','repair','review','plan','decompose')"


def upgrade() -> None:
    op.add_column("direct_runs", sa.Column("control_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("direct_runs", sa.Column("approval_task_id", sa.Uuid(), nullable=True))
    op.add_column("direct_runs", sa.Column("owner_actions", sa.dialects.postgresql.JSONB(), nullable=False, server_default="[]"))
    op.alter_column("direct_runs", "state", type_=sa.String(32))
    op.drop_constraint("direct_runs_shape", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_shape", "direct_runs", _NEW_SHAPES)
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _NEW_STATES)
    op.drop_constraint("direct_sessions_kind", "direct_sessions", type_="check")
    op.create_check_constraint("direct_sessions_kind", "direct_sessions", _NEW_KINDS)

    op.create_table(
        "direct_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("direct_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("shape", sa.String(16), nullable=False, server_default="quick"),
        sa.Column("state", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("plan_text", sa.Text(), nullable=True),
        sa.Column("plan_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("shape IN ('quick','planned')", name="direct_tasks_shape"),
        sa.CheckConstraint(
            "state IN ('PENDING','PLANNING','RUNNING','CHECKING','REVIEWING','REPAIRING','DONE','FAILED','CANCELLED','NEEDS_DECISION','AWAITING_PLAN_APPROVAL')",
            name="direct_tasks_state",
        ),
    )
    op.create_index(
        "ix_direct_tasks_run_ordinal", "direct_tasks", ["run_id", "ordinal"]
    )

    op.add_column(
        "direct_sessions",
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("direct_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "direct_checks",
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("direct_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "direct_reviews",
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("direct_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM direct_runs WHERE shape = 'epic' "
                "OR state IN ('DECOMPOSING','AWAITING_DECOMPOSITION_APPROVAL') LIMIT 1"
            )
        )
        .scalar()
    )
    if retained is not None:
        raise RuntimeError("durable direct epic data must not be discarded")
    task_count = (
        op.get_bind().execute(sa.text("SELECT 1 FROM direct_tasks LIMIT 1")).scalar()
    )
    if task_count is not None:
        raise RuntimeError("durable direct epic data must not be discarded")
    decompose_sessions = (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM direct_sessions WHERE kind = 'decompose' LIMIT 1")
        )
        .scalar()
    )
    if decompose_sessions is not None:
        raise RuntimeError("durable direct epic data must not be discarded")

    op.drop_column("direct_runs", "owner_actions")
    op.drop_column("direct_runs", "approval_task_id")
    op.drop_column("direct_runs", "control_version")
    op.drop_column("direct_reviews", "task_id")
    op.drop_column("direct_checks", "task_id")
    op.drop_column("direct_sessions", "task_id")
    op.drop_index("ix_direct_tasks_run_ordinal", table_name="direct_tasks")
    op.drop_table("direct_tasks")
    op.drop_constraint("direct_sessions_kind", "direct_sessions", type_="check")
    op.create_check_constraint("direct_sessions_kind", "direct_sessions", _OLD_KINDS)
    op.drop_constraint("direct_runs_state", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_state", "direct_runs", _OLD_STATES)
    op.drop_constraint("direct_runs_shape", "direct_runs", type_="check")
    op.create_check_constraint("direct_runs_shape", "direct_runs", _OLD_SHAPES)
    op.alter_column("direct_runs", "state", type_=sa.String(24))
