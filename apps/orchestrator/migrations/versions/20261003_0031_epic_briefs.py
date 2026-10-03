"""Project epics and immutable requirements brief revisions.

Revision ID: 20261003_0031
Revises: 20260929_0030
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_0031"
down_revision = "20260929_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "epics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("draft_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("draft", postgresql.JSONB(), nullable=False),
        sa.Column("accepted_brief_revision_id", sa.Uuid(), nullable=True),
        sa.Column("accepted_brief_digest", sa.String(64), nullable=True),
        sa.Column("accepted_graph_revision_id", sa.Uuid(), nullable=True),
        sa.Column("accepted_graph_digest", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("btrim(title) <> ''", name="title_nonblank"),
        sa.CheckConstraint("octet_length(title) <= 256", name="title_bounded"),
        sa.CheckConstraint("draft_schema_version = 1", name="draft_schema_version"),
        sa.CheckConstraint(
            "jsonb_typeof(draft) = 'object' AND octet_length(draft::text) <= 262144",
            name="draft_bounded",
        ),
        sa.CheckConstraint(
            "(accepted_brief_revision_id IS NULL) = (accepted_brief_digest IS NULL)",
            name="accepted_brief_pair",
        ),
        sa.CheckConstraint(
            "(accepted_graph_revision_id IS NULL) = (accepted_graph_digest IS NULL)",
            name="accepted_graph_pair",
        ),
        sa.CheckConstraint(
            "accepted_brief_digest IS NULL OR accepted_brief_digest ~ '^[0-9a-f]{64}$'",
            name="accepted_brief_digest",
        ),
        sa.CheckConstraint(
            "accepted_graph_digest IS NULL OR accepted_graph_digest ~ '^[0-9a-f]{64}$'",
            name="accepted_graph_digest",
        ),
    )
    op.create_index("ix_epics_project_id", "epics", ["project_id"])
    op.create_table(
        "epic_brief_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("epic_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("epic_version", sa.Integer(), nullable=False),
        sa.Column("document_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("source_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["epic_id"], ["epics.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("epic_id", "revision_number", name="uq_epic_brief_revisions_number"),
        sa.UniqueConstraint(
            "epic_id", "id", "content_digest", name="uq_epic_brief_revisions_binding"
        ),
        sa.CheckConstraint("revision_number >= 1", name="revision_number_positive"),
        sa.CheckConstraint("epic_version >= 1", name="epic_version_positive"),
        sa.CheckConstraint("document_schema_version = 1", name="document_schema_version"),
        sa.CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="content_digest"),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object' AND octet_length(content::text) <= 262144",
            name="content_bounded",
        ),
    )
    op.create_foreign_key(
        "fk_epics_accepted_brief",
        "epics",
        "epic_brief_revisions",
        ["id", "accepted_brief_revision_id", "accepted_brief_digest"],
        ["epic_id", "id", "content_digest"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        CREATE FUNCTION forge_epic_brief_revision_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'epic brief revisions are immutable' USING ERRCODE = '23514';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER epic_brief_revision_immutable
        BEFORE UPDATE OR DELETE ON epic_brief_revisions
        FOR EACH ROW EXECUTE FUNCTION forge_epic_brief_revision_immutable()
        """
    )


def downgrade() -> None:
    # Saved requirements must survive a mistaken binary rollback. An operator
    # must reconcile retained records before choosing a destructive reset.
    op.execute("LOCK TABLE epics, epic_brief_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM epics)
               OR EXISTS (SELECT 1 FROM epic_brief_revisions) THEN
                RAISE EXCEPTION 'cannot downgrade retained epic data' USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint("fk_epics_accepted_brief", "epics", type_="foreignkey")
    op.execute("DROP TRIGGER epic_brief_revision_immutable ON epic_brief_revisions")
    op.execute("DROP FUNCTION forge_epic_brief_revision_immutable()")
    op.drop_table("epic_brief_revisions")
    op.drop_index("ix_epics_project_id", table_name="epics")
    op.drop_table("epics")
