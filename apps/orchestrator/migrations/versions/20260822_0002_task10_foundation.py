"""task 10 durable project, task, mutation, and audit foundations

Revision ID: 20260822_0002
Revises: 20260821_0001
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260822_0002"
down_revision: str | None = "20260821_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Task 10 identity, immutable-source, receipt, and audit state."""

    # Existing 0001 fixture rows receive deterministic safe values before the
    # new non-null constraints are installed.  The insert trigger also keeps
    # older SQL callers working while normalizing the stored identity.
    op.add_column(
        "projects",
        sa.Column(
            "name", sa.String(length=255), nullable=True, server_default=sa.text("'Project'")
        ),
    )
    op.add_column(
        "projects",
        sa.Column("canonical_path_key", sa.Text(), nullable=True, server_default=sa.text("''")),
    )
    op.execute(
        "UPDATE projects SET name = COALESCE(NULLIF(btrim(name), ''), 'Project'), "
        "canonical_path_key = lower(replace(rtrim(canonical_path, '/\\\\'), '\\\\', '/')) "
        "WHERE name IS NULL OR btrim(name) = '' OR canonical_path_key IS NULL OR canonical_path_key = ''"
    )
    op.execute("UPDATE projects SET github_repository = lower(btrim(github_repository))")
    op.alter_column("projects", "name", nullable=False, server_default=sa.text("'Project'"))
    op.alter_column("projects", "canonical_path_key", nullable=False, server_default=sa.text("''"))
    op.create_unique_constraint(
        "uq_projects_canonical_path_key", "projects", ["canonical_path_key"]
    )
    op.create_check_constraint(op.f("ck_projects_name_nonblank"), "projects", "btrim(name) <> ''")
    op.create_check_constraint(
        op.f("ck_projects_canonical_path_key_nonblank"),
        "projects",
        "btrim(canonical_path_key) <> ''",
    )
    op.create_index(
        "ix_projects_canonical_path_key", "projects", ["canonical_path_key"], unique=False
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION forge_normalize_project_identity_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $forge$
        BEGIN
            IF NEW.name IS NULL OR btrim(NEW.name) = '' THEN
                NEW.name := 'Project';
            END IF;
            IF NEW.canonical_path_key IS NULL OR btrim(NEW.canonical_path_key) = '' THEN
                NEW.canonical_path_key := lower(replace(rtrim(NEW.canonical_path, '/\\'), '\\', '/'));
            END IF;
            NEW.github_repository := lower(btrim(NEW.github_repository));
            RETURN NEW;
        END;
        $forge$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_projects_identity_insert_normalize
        BEFORE INSERT ON projects
        FOR EACH ROW EXECUTE FUNCTION forge_normalize_project_identity_insert();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forge_reject_project_identity_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $forge$
        BEGIN
            IF NEW.canonical_path IS DISTINCT FROM OLD.canonical_path
               OR NEW.canonical_path_key IS DISTINCT FROM OLD.canonical_path_key
               OR NEW.github_repository IS DISTINCT FROM OLD.github_repository
               OR NEW.default_branch IS DISTINCT FROM OLD.default_branch THEN
                RAISE EXCEPTION 'project identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $forge$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_projects_identity_immutable
        BEFORE UPDATE ON projects
        FOR EACH ROW EXECUTE FUNCTION forge_reject_project_identity_update();
        """
    )

    op.add_column(
        "tasks",
        sa.Column(
            "title", sa.String(length=512), nullable=True, server_default=sa.text("'Imported task'")
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("body", sa.Text(), nullable=True),
    )
    op.add_column("tasks", sa.Column("source_url", sa.String(length=2048), nullable=True))
    op.add_column(
        "tasks", sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tasks",
        sa.Column(
            "untrusted_external_content",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        "UPDATE tasks SET title = COALESCE(NULLIF(btrim(title), ''), 'Imported task'), "
        "body = COALESCE(body, normalized_text), untrusted_external_content = "
        "(external_source IS NOT NULL OR external_id IS NOT NULL)"
    )
    op.alter_column("tasks", "title", nullable=False, server_default=sa.text("'Imported task'"))
    op.alter_column("tasks", "body", nullable=False, server_default=sa.text("''"))
    op.alter_column(
        "tasks", "untrusted_external_content", nullable=False, server_default=sa.text("false")
    )
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS uq_tasks_external_identity")
    op.create_unique_constraint(
        "uq_tasks_project_external_identity",
        "tasks",
        ["project_id", "external_source", "external_id"],
    )
    op.create_check_constraint(
        op.f("ck_tasks_external_source_trust_shape"),
        "tasks",
        "(external_source IS NULL AND external_id IS NULL AND untrusted_external_content = FALSE) "
        "OR (external_source IS NOT NULL AND external_id IS NOT NULL AND untrusted_external_content = TRUE)",
    )
    op.create_check_constraint(op.f("ck_tasks_title_nonblank"), "tasks", "btrim(title) <> ''")
    op.create_check_constraint(
        op.f("ck_tasks_title_bounded"), "tasks", "octet_length(title) <= 512"
    )
    op.create_check_constraint(
        op.f("ck_tasks_body_bounded"), "tasks", "octet_length(body) <= 1048576"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forge_reject_task_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $forge$
        BEGIN
            RAISE EXCEPTION 'tasks are append-only';
        END;
        $forge$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tasks_immutable
        BEFORE UPDATE OR DELETE ON tasks
        FOR EACH ROW EXECUTE FUNCTION forge_reject_task_update();
        """
    )

    op.create_table(
        "api_mutations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=16), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resource_kind", sa.String(length=96), nullable=True),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
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
        sa.CheckConstraint(
            "lifecycle_state IN ('RESERVED','COMPLETED')",
            name=op.f("ck_api_mutations_lifecycle_state"),
        ),
        sa.CheckConstraint(
            "char_length(key_hash) = 64 AND key_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_api_mutations_key_hash"),
        ),
        sa.CheckConstraint(
            "char_length(request_digest) = 64 AND request_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_api_mutations_request_digest"),
        ),
        sa.CheckConstraint(
            "(lifecycle_state = 'RESERVED' AND response_status IS NULL AND response_payload IS NULL "
            "AND resource_kind IS NULL AND resource_id IS NULL) OR "
            "(lifecycle_state = 'COMPLETED' AND response_status IS NOT NULL AND response_payload IS NOT NULL "
            "AND resource_kind IS NOT NULL AND resource_id IS NOT NULL)",
            name=op.f("ck_api_mutations_completion_shape"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_mutations")),
        sa.UniqueConstraint(
            "actor_id", "action", "key_hash", name="uq_api_mutations_actor_action_key_hash"
        ),
    )
    op.create_index(
        "ix_api_mutations_lifecycle_created_at",
        "api_mutations",
        ["lifecycle_state", "created_at"],
        unique=False,
    )

    op.create_table(
        "operator_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("subject_type", sa.String(length=96), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_operator_audit_events_schema_version_positive")
        ),
        sa.CheckConstraint(
            "char_length(event_type) BETWEEN 1 AND 96",
            name=op.f("ck_operator_audit_events_event_type_bounded"),
        ),
        sa.CheckConstraint(
            "char_length(subject_type) BETWEEN 1 AND 96",
            name=op.f("ck_operator_audit_events_subject_type_bounded"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator_audit_events")),
    )
    op.create_index(
        "ix_operator_audit_events_subject_created_at",
        "operator_audit_events",
        ["subject_type", "subject_id", "created_at"],
        unique=False,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forge_reject_operator_audit_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $forge$
        BEGIN
            RAISE EXCEPTION 'operator audit events are append-only';
        END;
        $forge$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_operator_audit_events_immutable
        BEFORE UPDATE OR DELETE ON operator_audit_events
        FOR EACH ROW EXECUTE FUNCTION forge_reject_operator_audit_update();
        """
    )


def downgrade() -> None:
    """Remove Task 10 foundation while leaving the 0001 schema intact."""

    op.execute(
        """
        DO $forge$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM tasks
                WHERE external_source IS NOT NULL AND external_id IS NOT NULL
                GROUP BY external_source, external_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'cannot downgrade Task 10: duplicate external task identities exist';
            END IF;
        END;
        $forge$;
        """
    )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_operator_audit_events_immutable ON operator_audit_events"
    )
    op.execute("DROP FUNCTION IF EXISTS forge_reject_operator_audit_update()")
    op.drop_index(
        "ix_operator_audit_events_subject_created_at",
        table_name="operator_audit_events",
        if_exists=True,
    )
    op.drop_table("operator_audit_events", if_exists=True)

    op.drop_index(
        "ix_api_mutations_lifecycle_created_at", table_name="api_mutations", if_exists=True
    )
    op.drop_table("api_mutations", if_exists=True)

    op.execute("DROP TRIGGER IF EXISTS trg_tasks_immutable ON tasks")
    op.execute("DROP FUNCTION IF EXISTS forge_reject_task_update()")
    op.drop_constraint("ck_tasks_body_bounded", "tasks", type_="check", if_exists=True)
    op.drop_constraint("ck_tasks_title_bounded", "tasks", type_="check", if_exists=True)
    op.drop_constraint("ck_tasks_title_nonblank", "tasks", type_="check", if_exists=True)
    op.drop_constraint(
        "ck_tasks_external_source_trust_shape", "tasks", type_="check", if_exists=True
    )
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS uq_tasks_project_external_identity")
    op.create_unique_constraint(
        "uq_tasks_external_identity", "tasks", ["external_source", "external_id"]
    )
    for column in (
        "untrusted_external_content",
        "source_updated_at",
        "source_url",
        "body",
        "title",
    ):
        op.drop_column("tasks", column)

    op.execute("DROP TRIGGER IF EXISTS trg_projects_identity_immutable ON projects")
    op.execute("DROP TRIGGER IF EXISTS trg_projects_identity_insert_normalize ON projects")
    op.execute("DROP FUNCTION IF EXISTS forge_reject_project_identity_update()")
    op.execute("DROP FUNCTION IF EXISTS forge_normalize_project_identity_insert()")
    op.drop_index("ix_projects_canonical_path_key", table_name="projects", if_exists=True)
    op.drop_constraint(
        "ck_projects_canonical_path_key_nonblank", "projects", type_="check", if_exists=True
    )
    op.drop_constraint("ck_projects_name_nonblank", "projects", type_="check", if_exists=True)
    op.drop_constraint("uq_projects_canonical_path_key", "projects", type_="unique", if_exists=True)
    op.drop_column("projects", "canonical_path_key")
    op.drop_column("projects", "name")
