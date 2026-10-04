"""Immutable brief-bound epic work-item graphs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_0032"
down_revision = "20261003_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "epic_graph_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("epic_id", sa.Uuid(), nullable=False),
        sa.Column("brief_revision_id", sa.Uuid(), nullable=False),
        sa.Column("brief_digest", sa.String(64), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("epic_version", sa.Integer(), nullable=False),
        sa.Column("document_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", postgresql.JSONB(), nullable=False),
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
            name="fk_epic_graph_revisions_brief",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("epic_id", "revision_number", name="uq_epic_graph_revisions_number"),
        sa.UniqueConstraint(
            "epic_id", "id", "graph_digest", name="uq_epic_graph_revisions_binding"
        ),
        sa.CheckConstraint("revision_number >= 1", name="revision_number_positive"),
        sa.CheckConstraint("epic_version >= 1", name="epic_version_positive"),
        sa.CheckConstraint("document_schema_version = 1", name="document_schema_version"),
        sa.CheckConstraint("brief_digest ~ '^[0-9a-f]{64}$'", name="brief_digest"),
        sa.CheckConstraint("graph_digest ~ '^[0-9a-f]{64}$'", name="graph_digest"),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object' AND octet_length(content::text) <= 524288",
            name="content_bounded",
        ),
    )
    op.create_foreign_key(
        "fk_epics_accepted_graph",
        "epics",
        "epic_graph_revisions",
        ["id", "accepted_graph_revision_id", "accepted_graph_digest"],
        ["epic_id", "id", "graph_digest"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        CREATE FUNCTION forge_epic_graph_revision_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'epic graph revisions are immutable' USING ERRCODE = '23514';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER epic_graph_revision_immutable
        BEFORE UPDATE OR DELETE ON epic_graph_revisions
        FOR EACH ROW EXECUTE FUNCTION forge_epic_graph_revision_immutable()
        """
    )


def downgrade() -> None:
    op.execute("LOCK TABLE epics, epic_graph_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM epic_graph_revisions)
               OR EXISTS (SELECT 1 FROM epics WHERE accepted_graph_revision_id IS NOT NULL) THEN
                RAISE EXCEPTION 'cannot downgrade retained epic graph data'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint("fk_epics_accepted_graph", "epics", type_="foreignkey")
    op.execute("DROP TRIGGER epic_graph_revision_immutable ON epic_graph_revisions")
    op.execute("DROP FUNCTION forge_epic_graph_revision_immutable()")
    op.drop_table("epic_graph_revisions")
