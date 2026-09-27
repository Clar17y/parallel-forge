"""Durable Jev request admission and score-only evidence.

Revision ID: 20260926_0028
Revises: 20260920_0024
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260926_0028"
down_revision = "20260920_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jev_evaluations",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("operation_digest", sa.String(64), primary_key=True),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("requested_mode", sa.String(8), nullable=False),
        sa.Column("effective_mode", sa.String(8), nullable=False),
        sa.Column("requested_model", sa.String(128), nullable=False),
        sa.Column("actual_model", sa.String(128)),
        sa.Column("worktree_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("candidate_digest", sa.String(64)),
        sa.Column("scope_digest", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reserved_input_units", sa.Integer(), nullable=False),
        sa.Column("actual_input_units", sa.Integer(), nullable=False),
        sa.Column("output_units", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("cache_hits", sa.Integer(), nullable=False),
        sa.Column("request_id_digest", sa.String(64)),
        sa.Column("cache_source_digest", sa.String(64)),
        sa.Column("diagnostic", sa.String(64)),
        sa.Column("scores", postgresql.JSONB(none_as_null=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "reserved_input_units >= 0 AND actual_input_units >= 0 AND output_units >= 0 AND duration_ms >= 0 AND cache_hits >= 0",
            name="ck_jev_evaluations_usage_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending','ranked','succeeded','unavailable','unknown','budget_exhausted')",
            name="ck_jev_evaluations_status",
        ),
    )
    op.create_index("ix_jev_evaluations_run_kind", "jev_evaluations", ["run_id", "kind"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM jev_evaluations LIMIT 1")).scalar() is not None:
        raise RuntimeError("Jev usage evidence must not be discarded")
    op.drop_index("ix_jev_evaluations_run_kind", table_name="jev_evaluations")
    op.drop_table("jev_evaluations")
