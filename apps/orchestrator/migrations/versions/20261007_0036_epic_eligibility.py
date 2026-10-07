"""Immutable verified epic completion handoffs.

Revision ID: 20261007_0036
Revises: 20261005_0035
"""

import sqlalchemy as sa
from alembic import op

revision = "20261007_0036"
down_revision = "20261005_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "epic_completion_handoffs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.Uuid(),
            sa.ForeignKey("epic_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("epic_item_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("run_version", sa.Integer(), nullable=False),
        sa.Column(
            "merge_intent_id",
            sa.Uuid(),
            sa.ForeignKey("operation_intents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("merge_sha", sa.String(40), nullable=False),
        sa.Column("integration_ref", sa.String(512), nullable=False),
        sa.Column("verified_base_sha", sa.String(40), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("execution_id", "item_id", name="uq_epic_completion_item"),
        sa.UniqueConstraint("attempt_id", name="uq_epic_completion_attempt"),
        sa.CheckConstraint(
            "merge_sha ~ '^[0-9a-f]{40}$'", name="ck_epic_completion_handoffs_merge_sha"
        ),
        sa.CheckConstraint(
            "verified_base_sha ~ '^[0-9a-f]{40}$'",
            name="ck_epic_completion_handoffs_verified_base_sha",
        ),
        sa.CheckConstraint(
            "evidence_digest ~ '^[0-9a-f]{64}$'", name="ck_epic_completion_handoffs_evidence_digest"
        ),
    )
    op.execute("""
        CREATE FUNCTION forge_epic_completion_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'epic completion handoffs are immutable' USING ERRCODE = '23514';
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER epic_completion_immutable BEFORE UPDATE OR DELETE
        ON epic_completion_handoffs FOR EACH ROW
        EXECUTE FUNCTION forge_epic_completion_immutable()
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE epic_completion_handoffs IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM epic_completion_handoffs) THEN
                RAISE EXCEPTION 'cannot discard epic completion handoffs' USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER epic_completion_immutable ON epic_completion_handoffs")
    op.execute("DROP FUNCTION forge_epic_completion_immutable()")
    op.drop_table("epic_completion_handoffs")
