"""Add mutable execution authority while preserving immutable source records."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261005_0035"
down_revision = "20261004_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "epic_admission_scan_cursor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_run_id", sa.Uuid()),
    )
    op.add_column(
        "epic_brainstorm_budget_ledgers",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "epic_brainstorm_budget_ledgers",
        sa.Column("disabled_dimensions", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column("epic_brainstorm_jobs", sa.Column("retry_authorized_until", sa.Integer()))
    op.add_column(
        "epic_brainstorm_jobs",
        sa.Column(
            "override_unknown_usage", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_table(
        "epic_execution_controls",
        sa.Column("execution_id", sa.Uuid(), primary_key=True),
        sa.Column("epic_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("blocker_code", sa.String(96)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["epic_executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["epic_id"], ["epics.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("version >= 1", name="ck_epic_execution_controls_version"),
        sa.CheckConstraint(
            "state IN ('ACTIVE','PAUSE_REQUESTED','PAUSED','RESUME_REQUESTED','CANCEL_REQUESTED','BLOCKED','SUCCEEDED','CANCELLED')",
            name="ck_epic_execution_controls_state",
        ),
    )
    op.create_index("ix_epic_execution_controls_epic", "epic_execution_controls", ["epic_id"])
    op.create_table(
        "epic_control_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("control_version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("expected_run_version", sa.Integer(), nullable=False),
        sa.Column("command_id", sa.Uuid()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("refusal", sa.String(512)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["epic_execution_controls.execution_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_id"], ["run_commands.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "execution_id", "control_version", "run_id", name="uq_epic_control_intent_child"
        ),
    )
    op.create_index(
        "ix_epic_control_intents_pending", "epic_control_intents", ["status", "execution_id"]
    )
    op.create_table(
        "epic_child_budget_holds",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("epic_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("budget_payload", postgresql.JSONB(), nullable=False),
        sa.Column("effects_settled", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["attempt_id"], ["epic_item_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["epic_id"], ["epics.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_epic_child_budget_holds_epic", "epic_child_budget_holds", ["epic_id"])
    op.create_table(
        "epic_budget_admission_permits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("epic_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.String(2048)),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("consumed_attempt_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["epic_id"], ["epics.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["consumed_attempt_id"], ["subscription_attempts.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_epic_budget_admission_permits_run",
        "epic_budget_admission_permits",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM epic_budget_admission_permits)"))
        or bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM epic_child_budget_holds)"))
        or bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM epic_execution_controls)"))
        or bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM epic_control_intents)"))
    ):
        raise RuntimeError("cannot drop nonempty epic execution control state")
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM epic_brainstorm_budget_ledgers WHERE version <> 1 OR disabled_dimensions <> '[]'::jsonb)"
        )
    ) or bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM epic_brainstorm_jobs WHERE retry_authorized_until IS NOT NULL OR override_unknown_usage)"
        )
    ):
        raise RuntimeError("cannot discard owner budget or retry actions")
    op.drop_index("ix_epic_control_intents_pending", table_name="epic_control_intents")
    op.drop_table("epic_control_intents")
    op.drop_index("ix_epic_child_budget_holds_epic", table_name="epic_child_budget_holds")
    op.drop_table("epic_child_budget_holds")
    op.drop_index(
        "ix_epic_budget_admission_permits_run", table_name="epic_budget_admission_permits"
    )
    op.drop_table("epic_budget_admission_permits")
    op.drop_index("ix_epic_execution_controls_epic", table_name="epic_execution_controls")
    op.drop_table("epic_execution_controls")
    op.drop_column("epic_brainstorm_jobs", "override_unknown_usage")
    op.drop_column("epic_brainstorm_jobs", "retry_authorized_until")
    op.drop_column("epic_brainstorm_budget_ledgers", "version")
    op.drop_column("epic_brainstorm_budget_ledgers", "disabled_dimensions")
    op.drop_table("epic_admission_scan_cursor")
