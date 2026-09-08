"""Retain historical prompt identity without inventing evidence for legacy rows."""

import sqlalchemy as sa
from alembic import op

revision = "20260908_0007"
down_revision = "20260908_0006"
branch_labels = depends_on = None


def upgrade():
    op.add_column("agent_executions", sa.Column("instruction_digest", sa.String(64), nullable=True))
    op.create_check_constraint(
        op.f("ck_agent_executions_instruction_digest"),
        "agent_executions",
        "instruction_digest IS NULL OR instruction_digest ~ '^[0-9a-f]{64}$'",
    )


def downgrade():
    op.drop_constraint(
        op.f("ck_agent_executions_instruction_digest"), "agent_executions", type_="check"
    )
    op.drop_column("agent_executions", "instruction_digest")
