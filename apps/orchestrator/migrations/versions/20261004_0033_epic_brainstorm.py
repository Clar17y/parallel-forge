"""Add durable epic brainstorming after #69 and #70.

Revision ID: 20261004_0033
Revises: 20261003_0032
"""

import sqlalchemy as sa
from alembic import op

revision = "20261004_0033"
down_revision = "20261003_0032"
branch_labels = None
depends_on = None

_DDL = (
    """
    CREATE TABLE epic_brainstorm_audit (
    id UUID NOT NULL,
    epic_id UUID NOT NULL,
    actor_id UUID,
    action VARCHAR(64) NOT NULL,
    subject_id UUID NOT NULL,
    detail JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_epic_brainstorm_audit PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE epic_brainstorm_budget_ledgers (
    epic_id UUID NOT NULL,
    project_id UUID NOT NULL,
    ceiling JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_epic_brainstorm_budget_ledgers PRIMARY KEY (epic_id)
    );
    """,
    """
    CREATE TABLE epic_brainstorm_receipts (
    id UUID NOT NULL,
    epic_id UUID NOT NULL,
    key VARCHAR(255) NOT NULL,
    request_digest VARCHAR(64) NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_epic_brainstorm_receipts PRIMARY KEY (id),
    CONSTRAINT uq_epic_brainstorm_receipts_epic_id UNIQUE (epic_id, key)
    );
    """,
    """
    CREATE TABLE epic_brainstorm_conversations (
    id UUID NOT NULL,
    epic_id UUID NOT NULL,
    project_id UUID NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_epic_brainstorm_conversations PRIMARY KEY (id),
    CONSTRAINT uq_epic_brainstorm_conversations_id UNIQUE (id, epic_id, project_id),
    CONSTRAINT fk_epic_brainstorm_conversations_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
    );
    """,
    """
    CREATE TABLE epic_brainstorm_jobs (
    id UUID NOT NULL,
    epic_id UUID NOT NULL,
    project_id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    version INTEGER NOT NULL,
    state VARCHAR(32) NOT NULL,
    snapshot JSONB NOT NULL,
    proposal JSONB,
    proposal_digest VARCHAR(64),
    failure VARCHAR(64),
    current_attempt_id UUID,
    adopted_revision_id UUID,
    next_eligible_at TIMESTAMP WITH TIME ZONE,
    wait_pool_revision INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_epic_brainstorm_jobs PRIMARY KEY (id),
    CONSTRAINT fk_epic_brainstorm_jobs_conversation_id_epic_brainstorm_d33e FOREIGN KEY(conversation_id, epic_id, project_id) REFERENCES epic_brainstorm_conversations (id, epic_id, project_id) ON DELETE RESTRICT
    );
    """,
    """
    CREATE INDEX ix_epic_brainstorm_jobs_queued ON epic_brainstorm_jobs (state, created_at);
    """,
    """
    CREATE TABLE epic_brainstorm_turns (
    id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    ordinal INTEGER NOT NULL,
    role VARCHAR(16) NOT NULL,
    text TEXT NOT NULL,
    pending BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_epic_brainstorm_turns PRIMARY KEY (id),
    CONSTRAINT uq_epic_brainstorm_turns_conversation_id UNIQUE (conversation_id, ordinal),
    CONSTRAINT fk_epic_brainstorm_turns_conversation_id_epic_brainstor_be28 FOREIGN KEY(conversation_id) REFERENCES epic_brainstorm_conversations (id) ON DELETE RESTRICT
    );
    """,
    """
    CREATE TABLE epic_brainstorm_attempts (
    id UUID NOT NULL,
    job_id UUID NOT NULL,
    number INTEGER NOT NULL,
    state VARCHAR(32) NOT NULL,
    owner VARCHAR(128) NOT NULL,
    fence UUID NOT NULL,
    lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    launch_intent BOOLEAN NOT NULL,
    launch_id VARCHAR(64),
    origin_host VARCHAR(64),
    process_started BOOLEAN NOT NULL,
    process_pid INTEGER,
    process_identity VARCHAR(256),
    process_settled BOOLEAN NOT NULL,
    terminal_proof JSONB,
    usage JSONB,
    reservation JSONB NOT NULL,
    usage_known BOOLEAN,
    tool_calls_used INTEGER NOT NULL,
    failure VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_epic_brainstorm_attempts PRIMARY KEY (id),
    CONSTRAINT uq_epic_brainstorm_attempts_job_id UNIQUE (job_id, number),
    CONSTRAINT fk_epic_brainstorm_attempts_job_id_epic_brainstorm_jobs FOREIGN KEY(job_id) REFERENCES epic_brainstorm_jobs (id) ON DELETE RESTRICT
    );
    """,
    """
    CREATE TABLE epic_brainstorm_quota_admissions (
    attempt_id UUID NOT NULL,
    provider VARCHAR(96) NOT NULL,
    account VARCHAR(96) NOT NULL,
    pool VARCHAR(96) NOT NULL,
    revision INTEGER NOT NULL,
    probe BOOLEAN NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_epic_brainstorm_quota_admissions PRIMARY KEY (attempt_id),
    CONSTRAINT fk_epic_brainstorm_quota_admissions_attempt_id_epic_bra_aecc FOREIGN KEY(attempt_id) REFERENCES epic_brainstorm_attempts (id) ON DELETE RESTRICT
    );
    """,
)

_TABLES = (
    "epic_brainstorm_audit",
    "epic_brainstorm_budget_ledgers",
    "epic_brainstorm_receipts",
    "epic_brainstorm_conversations",
    "epic_brainstorm_jobs",
    "epic_brainstorm_turns",
    "epic_brainstorm_attempts",
    "epic_brainstorm_quota_admissions",
)


def upgrade() -> None:
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("LOCK TABLE " + ", ".join(_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    bind = op.get_bind()
    for table in _TABLES:
        exists = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = :table)"
            ),
            {"table": table},
        ).scalar()
        if exists and bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("durable brainstorm evidence must not be discarded")
    for table in reversed(_TABLES):
        exists = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = :table)"
            ),
            {"table": table},
        ).scalar()
        if exists:
            op.drop_table(table)
