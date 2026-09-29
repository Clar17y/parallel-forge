"""Add durable subscription contract provenance and recovery diagnostics.

Revision ID: 20260929_0029
Revises: 20260926_0028
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260929_0029"
down_revision = "20260926_0028"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def upgrade() -> None:
    op.create_table(
        "subscription_contract_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "source_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey("approvals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("original_contract_digest", sa.String(64), nullable=False),
        sa.Column("original_contract_payload", JSONB(), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("contract_payload", JSONB(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("task_id", "revision", name="uq_subscription_contract_revision"),
    )
    op.execute("""
        CREATE FUNCTION forge_require_recovery_contract_worker() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE acquiring boolean;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                acquiring := NEW.state = 'leased';
            ELSE
                acquiring := NEW.state = 'leased' AND
                    (OLD.state <> 'leased' OR OLD.lease_generation <> NEW.lease_generation
                     OR OLD.lease_owner IS DISTINCT FROM NEW.lease_owner);
            END IF;
            IF acquiring
               AND (EXISTS (SELECT 1 FROM subscription_contract_revisions
                            WHERE task_id = NEW.task_id)
                    OR EXISTS (SELECT 1 FROM subscription_recovery_receipts
                               WHERE task_id = NEW.task_id)
                    OR EXISTS (
                        SELECT 1 FROM subscription_attempt_results r
                        JOIN subscription_attempts a ON a.id = r.attempt_id
                        WHERE a.task_row_id = NEW.task_id
                          AND r.disposition = 'role_correction_queued'
                    ))
               AND current_setting('forge.recovery_contract_version', true) IS DISTINCT FROM '1'
            THEN
                RAISE EXCEPTION 'subscription contract requires compatible worker'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_subscription_recovery_contract_worker
        BEFORE INSERT OR UPDATE OF state, lease_owner, lease_generation
        ON subscription_scheduled_tasks
        FOR EACH ROW EXECUTE FUNCTION forge_require_recovery_contract_worker();
    """)
    op.create_table(
        "subscription_application_diagnostics",
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="CASCADE"),
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
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=False),
        sa.Column("failed_applications", sa.Integer(), nullable=False),
        sa.Column("first_failure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index(
        "ix_subscription_application_diagnostics_run_id",
        "subscription_application_diagnostics",
        ["run_id"],
    )
    op.create_table(
        "subscription_recovery_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("subscription_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "run_id", "actor_id", "idempotency_key", name="uq_subscription_recovery_idempotency"
        ),
    )
    op.create_table(
        "subscription_recovery_signing_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key_hex", sa.String(64), nullable=False),
    )
    op.execute("""
        INSERT INTO subscription_recovery_signing_keys (id, key_hex)
        VALUES (1, replace(gen_random_uuid()::text, '-', '') ||
                   replace(gen_random_uuid()::text, '-', ''))
    """)
    op.create_table(
        "subscription_recovery_workers",
        sa.Column("worker_id", sa.String(255), primary_key=True),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_subscription_recovery_contract_worker ON subscription_scheduled_tasks"
    )
    op.execute("DROP FUNCTION forge_require_recovery_contract_worker()")
    op.drop_table("subscription_recovery_receipts")
    op.drop_table("subscription_recovery_workers")
    op.drop_table("subscription_recovery_signing_keys")
    op.drop_index(
        "ix_subscription_application_diagnostics_run_id",
        table_name="subscription_application_diagnostics",
    )
    op.drop_table("subscription_application_diagnostics")
    op.drop_table("subscription_contract_revisions")
