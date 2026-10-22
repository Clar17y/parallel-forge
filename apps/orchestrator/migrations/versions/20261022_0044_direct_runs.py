"""Direct autonomous runs and sessions (Slice A).

Revision ID: 20261022_0044
Revises: 20261021_0043
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261022_0044"
down_revision = "20261021_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "direct_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("client", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("effort", sa.String(32)),
        sa.Column("state", sa.String(24), nullable=False, server_default="QUEUED"),
        sa.Column("branch", sa.String(255)),
        sa.Column("worktree_path", sa.Text()),
        sa.Column("base_sha", sa.String(64)),
        sa.Column("head_sha", sa.String(64)),
        sa.Column("summary", sa.Text()),
        sa.Column("question", sa.Text()),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("failure_reason", sa.String(512)),
        sa.Column("result_degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('QUEUED','RUNNING','FINALIZING','DONE','FAILED','CANCELLED','NEEDS_DECISION')",
            name="direct_runs_state",
        ),
        sa.CheckConstraint("btrim(task_text) <> ''", name="direct_runs_task_nonblank"),
    )
    op.create_index("ix_direct_runs_state_created", "direct_runs", ["state", "created_at"])
    op.create_table(
        "direct_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("direct_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(24), nullable=False, server_default="RUNNING"),
        sa.Column("client", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("effort", sa.String(32)),
        sa.Column("pid", sa.Integer()),
        sa.Column("transcript_path", sa.Text()),
        sa.Column("last_output_at", sa.DateTime(timezone=True)),
        sa.Column("output_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.String(512)),
        sa.CheckConstraint(
            "state IN ('RUNNING','SUCCEEDED','FAILED','KILLED','TIMED_OUT')",
            name="direct_sessions_state",
        ),
    )
    op.create_index("ix_direct_sessions_run", "direct_sessions", ["run_id", "started_at"])


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM direct_runs LIMIT 1"))
        .scalar()
    )
    if retained is not None:
        raise RuntimeError("durable direct runs must not be discarded")
    op.drop_table("direct_sessions")
    op.drop_table("direct_runs")
