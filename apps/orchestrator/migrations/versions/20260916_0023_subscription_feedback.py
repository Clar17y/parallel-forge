"""Persist worker-specific operator feedback and delivery bindings.

Revision ID: 20260916_0023
Revises: 20260913_0022
"""

import sqlalchemy as sa
from alembic import op

revision = "20260916_0023"
down_revision = "20260913_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_task_feedback",
        sa.Column(
            "id",
            sa.Uuid(),
            sa.ForeignKey("api_mutations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("primary_task_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("observed_run_version", sa.Integer(), nullable=False),
        sa.Column("observed_task_version", sa.Integer(), nullable=False),
        sa.Column("observed_primary_version", sa.Integer(), nullable=False),
        sa.Column("observed_task_digest", sa.String(64), nullable=False),
        sa.Column("observed_primary_digest", sa.String(64), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=False),
        sa.Column("feedback_digest", sa.String(64), nullable=False),
        sa.Column("feedback_bytes", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending_primary"),
        sa.Column(
            "primary_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "delivery_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="RESTRICT"),
        ),
        sa.Column("application_digest", sa.String(64)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("closed_reason", sa.String(32)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "primary_task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_feedback_primary",
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_feedback_target",
        ),
        sa.CheckConstraint(
            "state IN ('pending_primary','forwarded','delivered','closed')",
            name="subscription_feedback_state",
        ),
        sa.CheckConstraint(
            "observed_run_version >= 0 AND observed_task_version >= 0 AND observed_primary_version >= 0",
            name="subscription_feedback_versions",
        ),
        sa.CheckConstraint(
            "feedback_bytes BETWEEN 1 AND 4096 AND feedback_bytes = octet_length(feedback)",
            name="subscription_feedback_size",
        ),
        sa.CheckConstraint(
            "feedback_digest ~ '^[0-9a-f]{64}$' AND request_digest ~ '^[0-9a-f]{64}$' "
            "AND observed_task_digest ~ '^[0-9a-f]{64}$' "
            "AND observed_primary_digest ~ '^[0-9a-f]{64}$' "
            "AND envelope_digest ~ '^[0-9a-f]{64}$' "
            "AND (application_digest IS NULL OR application_digest ~ '^[0-9a-f]{64}$')",
            name="subscription_feedback_digests",
        ),
        sa.CheckConstraint("primary_task_id <> task_id", name="feedback_distinct_tasks"),
        sa.CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('accepted','cancelled','budget_exhausted')",
            name="feedback_closed_reason",
        ),
        sa.CheckConstraint(
            "(state = 'pending_primary' AND application_digest IS NULL "
            "AND delivery_attempt_id IS NULL AND delivered_at IS NULL AND closed_reason IS NULL) OR "
            "(state = 'forwarded' AND primary_attempt_id IS NOT NULL "
            "AND application_digest IS NOT NULL AND delivered_at IS NULL AND closed_reason IS NULL) OR "
            "(state = 'delivered' AND primary_attempt_id IS NOT NULL "
            "AND application_digest IS NOT NULL AND delivery_attempt_id IS NOT NULL "
            "AND delivered_at IS NOT NULL AND closed_reason IS NULL) OR "
            "(state = 'closed' AND primary_attempt_id IS NOT NULL "
            "AND application_digest IS NOT NULL AND delivery_attempt_id IS NULL "
            "AND delivered_at IS NULL AND closed_reason IS NOT NULL)",
            name="subscription_feedback_lifecycle",
        ),
    )
    op.create_index(
        "ix_subscription_feedback_primary_state",
        "subscription_task_feedback",
        ["run_id", "primary_task_id", "state"],
    )
    op.create_index(
        "ix_subscription_feedback_target_state",
        "subscription_task_feedback",
        ["run_id", "task_id", "state"],
    )
    op.create_index(
        "uq_subscription_feedback_pending_primary",
        "subscription_task_feedback",
        ["run_id", "primary_task_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending_primary'"),
    )


def downgrade() -> None:
    retained = (
        op.get_bind().execute(sa.text("SELECT 1 FROM subscription_task_feedback LIMIT 1")).scalar()
    )
    if retained is not None:
        raise RuntimeError("durable subscription feedback must not be discarded")
    op.drop_table("subscription_task_feedback")
