"""subscription runtime persistence

Revision ID: 20260910_0008
Revises: 20260909_0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260910_0008"
down_revision = "20260909_0006"
branch_labels = depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    json = postgresql.JSONB()
    op.create_table(
        "subscription_profile_versions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("profile_id", uuid, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("payload", json, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("profile_id", "version", name="uq_subscription_profile_version"),
        sa.CheckConstraint("version >= 1", name="subscription_profile_version_positive"),
    )
    op.create_table(
        "project_subscription_profiles",
        sa.Column(
            "project_id", uuid, sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("profile_id", uuid, nullable=False),
        sa.Column("profile_version", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ("profile_id", "profile_version"),
            ("subscription_profile_versions.profile_id", "subscription_profile_versions.version"),
            ondelete="RESTRICT",
            name="fk_project_subscription_profile_version",
        ),
    )
    op.create_table(
        "subscription_envelopes",
        sa.Column("run_id", uuid, sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_id", uuid, nullable=False),
        sa.Column("profile_version", sa.Integer, nullable=False),
        sa.Column("safety_policy_version", sa.Integer, nullable=False),
        sa.Column("payload", json, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ("profile_id", "profile_version"),
            ("subscription_profile_versions.profile_id", "subscription_profile_versions.version"),
            ondelete="RESTRICT",
            name="fk_subscription_envelope_profile_version",
        ),
        sa.CheckConstraint(
            "safety_policy_version >= 1", name="subscription_envelope_safety_policy_positive"
        ),
    )
    op.create_table(
        "subscription_tasks",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("run_id", uuid, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("parent_task_id", uuid),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("pause_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload", json, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "id", name="uq_subscription_task_run_id"),
        sa.UniqueConstraint("run_id", "task_id", name="uq_subscription_task_run_task"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_subscription_task_idempotency"),
        sa.ForeignKeyConstraint(
            ("run_id", "parent_task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="RESTRICT",
            name="fk_subscription_task_parent",
        ),
        sa.CheckConstraint(
            "state IN ('queued','running','blocked','reconciling','terminal')",
            name="subscription_task_state",
        ),
        sa.CheckConstraint("version >= 0", name="subscription_task_version_nonnegative"),
    )
    op.create_table(
        "subscription_task_dependencies",
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("dependency_task_id", uuid, nullable=False),
        sa.PrimaryKeyConstraint(
            "run_id", "task_id", "dependency_task_id", name="pk_subscription_task_dependencies"
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_task_dep_task",
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "dependency_task_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="RESTRICT",
            name="fk_subscription_task_dep_dependency",
        ),
    )
    op.create_table(
        "subscription_attempts",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("task_row_id", uuid, nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("route_payload", json, nullable=False),
        sa.Column("telemetry_payload", json),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "task_row_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_attempt_task",
        ),
        sa.UniqueConstraint(
            "run_id", "task_row_id", "id", name="uq_subscription_attempt_run_task_id"
        ),
        sa.UniqueConstraint("task_row_id", "attempt_number", name="uq_subscription_attempt_number"),
        sa.UniqueConstraint(
            "task_row_id", "idempotency_key", name="uq_subscription_attempt_idempotency"
        ),
        sa.CheckConstraint("attempt_number >= 1", name="subscription_attempt_number_positive"),
        sa.CheckConstraint(
            "status IN ('queued','running','blocked','reconciling','terminal')",
            name="subscription_attempt_status",
        ),
    )
    op.create_table(
        "subscription_operation_bindings",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "attempt_id",
            uuid,
            sa.ForeignKey("subscription_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_call_key", sa.String(255), nullable=False),
        sa.Column("durable_operation_id", uuid, nullable=False),
        sa.Column("payload", json, nullable=False),
        sa.Column("receipt_payload", json),
        sa.UniqueConstraint(
            "attempt_id", "provider_call_key", name="uq_subscription_operation_provider_key"
        ),
    )
    op.create_table(
        "subscription_budget_pools",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("run_id", uuid, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_row_id", uuid),
        sa.Column("payload", json, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "task_row_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_budget_pool_task",
        ),
        sa.UniqueConstraint(
            "run_id",
            "task_row_id",
            postgresql_nulls_not_distinct=True,
            name="uq_subscription_budget_scope",
        ),
    )
    op.create_table(
        "subscription_budget_reservations",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("run_id", uuid, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_row_id", uuid, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("budget_payload", json, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "task_row_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_budget_reservation_task",
        ),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_subscription_budget_reservation_key"
        ),
        sa.CheckConstraint(
            "status IN ('reserved','released','consumed')",
            name="subscription_budget_reservation_status",
        ),
    )
    op.create_table(
        "subscription_decision_records",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("run_id", uuid, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_row_id", uuid),
        sa.Column("attempt_id", uuid),
        sa.Column("record_type", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload", json, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "task_row_id"),
            ("subscription_tasks.run_id", "subscription_tasks.id"),
            ondelete="CASCADE",
            name="fk_subscription_decision_task",
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "task_row_id", "attempt_id"),
            (
                "subscription_attempts.run_id",
                "subscription_attempts.task_row_id",
                "subscription_attempts.id",
            ),
            ondelete="CASCADE",
            name="fk_subscription_decision_attempt",
        ),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_subscription_decision_idempotency"
        ),
        sa.CheckConstraint(
            "attempt_id IS NULL OR task_row_id IS NOT NULL",
            name="subscription_decision_attempt_requires_task",
        ),
    )


def downgrade() -> None:
    # Refuse a downgrade that would discard v0.2 operator history.
    for table in (
        "subscription_decision_records",
        "subscription_budget_reservations",
        "subscription_budget_pools",
        "subscription_operation_bindings",
        "subscription_attempts",
        "subscription_task_dependencies",
        "subscription_tasks",
        "subscription_envelopes",
        "project_subscription_profiles",
        "subscription_profile_versions",
    ):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table}) THEN RAISE EXCEPTION 'remove v0.2 disposable subscription data before downgrade'; END IF; END $$;"
        )
    for table in (
        "subscription_decision_records",
        "subscription_budget_reservations",
        "subscription_budget_pools",
        "subscription_operation_bindings",
        "subscription_attempts",
        "subscription_task_dependencies",
        "subscription_tasks",
        "subscription_envelopes",
        "project_subscription_profiles",
        "subscription_profile_versions",
    ):
        op.drop_table(table)
