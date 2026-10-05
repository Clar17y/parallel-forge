"""Bind saved epic snapshots to immutable task/run attempts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261004_0034"
down_revision = "20261004_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "epic_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("epic_id", sa.Uuid(), nullable=False),
        sa.Column("brief_revision_id", sa.Uuid(), nullable=False),
        sa.Column("brief_digest", sa.String(64), nullable=False),
        sa.Column("graph_revision_id", sa.Uuid(), nullable=False),
        sa.Column("graph_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["epic_id"], ["epics.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["epic_id", "brief_revision_id", "brief_digest"],
            [
                "epic_brief_revisions.epic_id",
                "epic_brief_revisions.id",
                "epic_brief_revisions.content_digest",
            ],
            name="fk_epic_executions_brief",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["epic_id", "graph_revision_id", "graph_digest"],
            [
                "epic_graph_revisions.epic_id",
                "epic_graph_revisions.id",
                "epic_graph_revisions.graph_digest",
            ],
            name="fk_epic_executions_graph",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "id",
            "epic_id",
            "brief_revision_id",
            "brief_digest",
            "graph_revision_id",
            "graph_digest",
            name="uq_epic_executions_source",
        ),
    )
    op.create_table(
        "epic_item_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("epic_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("item_disposition", sa.String(8), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("expected_epic_version", sa.Integer(), nullable=False),
        sa.Column("actual_epic_version", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("brief_revision_id", sa.Uuid(), nullable=False),
        sa.Column("brief_digest", sa.String(64), nullable=False),
        sa.Column("graph_revision_id", sa.Uuid(), nullable=False),
        sa.Column("graph_digest", sa.String(64), nullable=False),
        sa.Column("item_digest", sa.String(64), nullable=False),
        sa.Column("context_digest", sa.String(64), nullable=False),
        sa.Column("task_digest", sa.String(64), nullable=False),
        sa.Column("base_ref", sa.String(512), nullable=False),
        sa.Column("base_sha", sa.String(40), nullable=False),
        sa.Column("owner_override", sa.Boolean(), nullable=False),
        sa.Column("override_note", sa.String(2048)),
        sa.Column("blocker_codes", postgresql.JSONB(), nullable=False),
        sa.Column("dependency_evidence", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["epic_id"], ["epics.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            [
                "execution_id",
                "epic_id",
                "brief_revision_id",
                "brief_digest",
                "graph_revision_id",
                "graph_digest",
            ],
            [
                "epic_executions.id",
                "epic_executions.epic_id",
                "epic_executions.brief_revision_id",
                "epic_executions.brief_digest",
                "epic_executions.graph_revision_id",
                "epic_executions.graph_digest",
            ],
            name="fk_epic_item_attempts_execution_source",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", name="uq_epic_item_attempts_run"),
        sa.UniqueConstraint("task_id", name="uq_epic_item_attempts_task"),
        sa.UniqueConstraint(
            "execution_id", "item_id", "attempt_number", name="uq_epic_item_attempts_number"
        ),
        sa.CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        sa.CheckConstraint(
            "expected_epic_version >= 1 AND actual_epic_version >= 1", name="epic_versions_positive"
        ),
        sa.CheckConstraint("item_disposition IN ('required', 'deferred')", name="item_disposition"),
        sa.CheckConstraint("context_digest ~ '^[0-9a-f]{64}$'", name="context_digest"),
        sa.CheckConstraint("task_digest ~ '^[0-9a-f]{64}$'", name="task_digest"),
        sa.CheckConstraint("item_digest ~ '^[0-9a-f]{64}$'", name="item_digest"),
        sa.CheckConstraint("base_sha ~ '^[0-9a-f]{40}$'", name="base_sha"),
        sa.CheckConstraint(
            "jsonb_typeof(blocker_codes) = 'array' AND jsonb_typeof(dependency_evidence) = 'array'",
            name="evidence_shape",
        ),
    )
    op.create_index(
        "ix_epic_item_attempts_execution", "epic_item_attempts", ["execution_id", "created_at"]
    )
    op.execute(
        """
        CREATE FUNCTION forge_epic_execution_source_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'epic execution sources are retained' USING ERRCODE = '23514';
            END IF;
            IF ROW(NEW.id, NEW.epic_id, NEW.brief_revision_id, NEW.brief_digest,
                   NEW.graph_revision_id, NEW.graph_digest, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.epic_id, OLD.brief_revision_id, OLD.brief_digest,
                   OLD.graph_revision_id, OLD.graph_digest, OLD.created_at) THEN
                RAISE EXCEPTION 'epic execution sources are immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER epic_execution_source_immutable BEFORE UPDATE OR DELETE "
        "ON epic_executions FOR EACH ROW EXECUTE FUNCTION forge_epic_execution_source_immutable()"
    )
    op.execute(
        """
        CREATE FUNCTION forge_epic_item_attempt_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'epic item attempts are immutable' USING ERRCODE = '23514';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER epic_item_attempt_immutable BEFORE UPDATE OR DELETE "
        "ON epic_item_attempts FOR EACH ROW EXECUTE FUNCTION forge_epic_item_attempt_immutable()"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE epic_item_attempts, epic_executions IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM epic_item_attempts)
               OR EXISTS (SELECT 1 FROM epic_executions) THEN
                RAISE EXCEPTION 'cannot downgrade retained epic execution data' USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER epic_item_attempt_immutable ON epic_item_attempts")
    op.execute("DROP FUNCTION forge_epic_item_attempt_immutable()")
    op.execute("DROP TRIGGER epic_execution_source_immutable ON epic_executions")
    op.execute("DROP FUNCTION forge_epic_execution_source_immutable()")
    op.drop_table("epic_item_attempts")
    op.drop_table("epic_executions")
