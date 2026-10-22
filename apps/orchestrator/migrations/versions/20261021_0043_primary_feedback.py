"""Persist operator feedback addressed directly to the root primary task.

Revision ID: 20261021_0043
Revises: 20261020_0042
"""

import sqlalchemy as sa
from alembic import op

revision = "20261021_0043"
down_revision = "20261020_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_primary_feedback",
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
        sa.Column("observed_run_version", sa.Integer(), nullable=False),
        sa.Column("observed_task_version", sa.Integer(), nullable=False),
        sa.Column("observed_task_digest", sa.String(64), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=False),
        sa.Column("feedback_digest", sa.String(64), nullable=False),
        sa.Column("feedback_bytes", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending"),
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
            name="fk_subscription_primary_feedback_primary",
        ),
        sa.CheckConstraint(
            "state IN ('pending','delivered','closed')",
            name="subscription_primary_feedback_state",
        ),
        sa.CheckConstraint(
            "observed_run_version >= 0 AND observed_task_version >= 0",
            name="subscription_primary_feedback_versions",
        ),
        sa.CheckConstraint(
            "feedback_bytes BETWEEN 1 AND 4096 AND feedback_bytes = octet_length(feedback)",
            name="subscription_primary_feedback_size",
        ),
        sa.CheckConstraint(
            "feedback_digest ~ '^[0-9a-f]{64}$' AND request_digest ~ '^[0-9a-f]{64}$' "
            "AND observed_task_digest ~ '^[0-9a-f]{64}$' "
            "AND envelope_digest ~ '^[0-9a-f]{64}$' "
            "AND (application_digest IS NULL OR application_digest ~ '^[0-9a-f]{64}$')",
            name="subscription_primary_feedback_digests",
        ),
        sa.CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('cancelled','budget_exhausted','run_terminal')",
            name="primary_feedback_closed_reason",
        ),
        # Pending rows may carry a delivery_attempt_id binding, settled to
        # delivered only on launch proof (mirrors worker feedback).
        sa.CheckConstraint(
            "(state = 'pending' AND delivered_at IS NULL "
            "AND closed_reason IS NULL AND application_digest IS NULL) OR "
            "(state = 'delivered' AND delivery_attempt_id IS NOT NULL "
            "AND delivered_at IS NOT NULL AND closed_reason IS NULL) OR "
            "(state = 'closed' AND delivery_attempt_id IS NULL AND delivered_at IS NULL "
            "AND closed_reason IS NOT NULL AND application_digest IS NOT NULL)",
            name="subscription_primary_feedback_lifecycle",
        ),
    )
    op.create_index(
        "ix_subscription_primary_feedback_primary_state",
        "subscription_primary_feedback",
        ["run_id", "primary_task_id", "state"],
    )
    op.create_index(
        "ix_subscription_primary_feedback_delivery",
        "subscription_primary_feedback",
        ["delivery_attempt_id"],
    )


def downgrade() -> None:
    retained = (
        op.get_bind().execute(sa.text("SELECT 1 FROM subscription_primary_feedback LIMIT 1")).scalar()
    )
    if retained is not None:
        raise RuntimeError("durable primary feedback must not be discarded")
    op.drop_table("subscription_primary_feedback")
