"""Retain asynchronous operator task-stop and resumption evidence.

Revision ID: 20260912_0020
Revises: 20260912_0019
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260912_0020"
down_revision = "20260912_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_task_stops",
        sa.Column(
            "id",
            sa.Uuid(),
            sa.ForeignKey("api_mutations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stop_task_version", sa.Integer(), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("settled_task_version", sa.Integer()),
        sa.Column("settlement_payload", postgresql.JSONB()),
        sa.Column("settlement_digest", sa.String(64)),
        sa.Column(
            "resume_receipt_id",
            sa.Uuid(),
            sa.ForeignKey("api_mutations.id", ondelete="RESTRICT"),
            unique=True,
        ),
        sa.Column("resumed_task_version", sa.Integer()),
        sa.Column(
            "superseding_receipt_id",
            sa.Uuid(),
            sa.ForeignKey("api_mutations.id", ondelete="RESTRICT"),
            unique=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("task_id", "stop_task_version", name="uq_task_stop_version"),
        sa.CheckConstraint(
            "state IN ('requested','paused','cancelled','resumed','superseded')",
            name="task_stop_state",
        ),
        sa.CheckConstraint(
            "(state = 'superseded') = (superseding_receipt_id IS NOT NULL)",
            name="task_stop_superseded",
        ),
        sa.CheckConstraint(
            "(settled_task_version IS NULL) = (settlement_payload IS NULL)",
            name="task_stop_settlement_version",
        ),
        sa.CheckConstraint(
            "stop_task_version >= 1 AND lease_generation >= 1", name="task_stop_versions"
        ),
        sa.CheckConstraint(
            "(settlement_payload IS NULL) = (settlement_digest IS NULL)",
            name="task_stop_proof_pair",
        ),
        sa.CheckConstraint(
            "(state = 'superseded' AND resume_receipt_id IS NULL AND resumed_task_version IS NULL) OR "
            "(state = 'requested' AND settled_task_version IS NULL AND settlement_payload IS NULL "
            "AND resume_receipt_id IS NULL AND resumed_task_version IS NULL) OR "
            "(state IN ('paused','cancelled') AND settled_task_version IS NOT NULL AND settled_task_version >= stop_task_version "
            "AND settlement_payload IS NOT NULL AND resume_receipt_id IS NULL "
            "AND resumed_task_version IS NULL) OR "
            "(state = 'resumed' AND settled_task_version IS NOT NULL AND settled_task_version >= stop_task_version "
            "AND settlement_payload IS NOT NULL AND resume_receipt_id IS NOT NULL "
            "AND resumed_task_version IS NOT NULL AND resumed_task_version = settled_task_version + 1)",
            name="task_stop_lifecycle",
        ),
    )
    op.create_index("ix_task_stop_pending", "subscription_task_stops", ["state", "id"])
    op.create_index(
        "ix_task_stop_attempt_version",
        "subscription_task_stops",
        ["attempt_id", "stop_task_version"],
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM subscription_task_stops)"))
        .scalar()
    ):
        raise RuntimeError("cannot discard retained subscription task-stop evidence")
    op.drop_table("subscription_task_stops")
