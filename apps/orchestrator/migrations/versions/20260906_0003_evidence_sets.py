"""immutable canonical evidence sets and reviewer input bindings

Revision ID: 20260906_0003
Revises: 20260822_0002
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260906_0003"
down_revision: str | None = "20260822_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _immutable_trigger(table: str) -> None:
    function = f"forge_reject_{table}_mutation"
    op.execute(
        f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'immutable evidence record'; END; $$"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()"
    )


def upgrade() -> None:
    op.create_unique_constraint("uq_steps_id_run", "steps", ["id", "run_id"])
    op.create_unique_constraint(
        "uq_agent_executions_id_run_step_role", "agent_executions", ["id", "run_id", "step_id", "role"]
    )
    op.create_table(
        "evidence_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("producer_execution_id", postgresql.UUID(as_uuid=True)),
        sa.Column("producer_step_id", postgresql.UUID(as_uuid=True)),
        sa.Column("producer_role", sa.String(24)),
        sa.Column("manifest_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("validation_evidence_set_id", postgresql.UUID(as_uuid=True)),
        sa.Column("validation_parent_policy_version", sa.Integer()),
        sa.Column("validation_parent_kind", sa.String(16)),
        sa.Column("validation_parent_head_sha", sa.String(40)),
        sa.Column("prior_review_evidence_set_id", postgresql.UUID(as_uuid=True)),
        sa.Column("prior_review_parent_policy_version", sa.Integer()),
        sa.Column("prior_review_parent_kind", sa.String(16)),
        sa.Column("review_finding_ids", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["step_id", "run_id"], ["steps.id", "steps.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["manifest_artifact_id"], ["artifacts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["producer_execution_id", "run_id", "producer_step_id", "producer_role"],
            ["agent_executions.id", "agent_executions.run_id", "agent_executions.step_id", "agent_executions.role"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_evidence_set_id", "run_id", "validation_parent_policy_version", "validation_parent_kind", "validation_parent_head_sha"],
            ["evidence_sets.id", "evidence_sets.run_id", "evidence_sets.policy_version", "evidence_sets.kind", "evidence_sets.head_sha"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prior_review_evidence_set_id", "run_id", "prior_review_parent_policy_version", "prior_review_parent_kind"],
            ["evidence_sets.id", "evidence_sets.run_id", "evidence_sets.policy_version", "evidence_sets.kind"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "run_id", name="uq_evidence_sets_id_run"),
        sa.UniqueConstraint("id", "run_id", "policy_version", "kind", name="uq_evidence_sets_parent"),
        sa.UniqueConstraint("id", "run_id", "policy_version", "kind", "head_sha", name="uq_evidence_sets_head_parent"),
        sa.UniqueConstraint("run_id", "manifest_artifact_id", name="uq_evidence_sets_run_manifest"),
        sa.CheckConstraint("kind IN ('validation','review')", name="kind"),
        sa.CheckConstraint("policy_version >= 1", name="policy"),
        sa.CheckConstraint("head_sha ~ '^[0-9a-f]{40}$'", name="head"),
        sa.CheckConstraint("(validation_evidence_set_id IS NULL OR id <> validation_evidence_set_id) AND (prior_review_evidence_set_id IS NULL OR id <> prior_review_evidence_set_id)", name="not_self_parent"),
        sa.CheckConstraint("(kind = 'validation' AND producer_execution_id IS NULL AND producer_step_id IS NULL AND producer_role IS NULL AND validation_evidence_set_id IS NULL AND validation_parent_policy_version IS NULL AND validation_parent_kind IS NULL AND validation_parent_head_sha IS NULL AND ((prior_review_evidence_set_id IS NULL AND prior_review_parent_policy_version IS NULL AND prior_review_parent_kind IS NULL) OR (prior_review_evidence_set_id IS NOT NULL AND prior_review_parent_policy_version IS NOT NULL AND prior_review_parent_kind = 'review')) AND review_finding_ids IS NULL) OR (kind = 'review' AND producer_execution_id IS NOT NULL AND producer_step_id IS NOT NULL AND producer_role = 'reviewer' AND validation_evidence_set_id IS NOT NULL AND validation_parent_policy_version IS NOT NULL AND validation_parent_kind = 'validation' AND validation_parent_head_sha IS NOT NULL AND prior_review_evidence_set_id IS NULL AND prior_review_parent_policy_version IS NULL AND prior_review_parent_kind IS NULL AND review_finding_ids IS NOT NULL)", name="shape"),
    )
    op.create_index("ix_evidence_sets_scope", "evidence_sets", ["run_id", "step_id", "kind", "head_sha"])
    op.create_index("ix_evidence_sets_producer", "evidence_sets", ["producer_execution_id"])
    op.create_index("ix_evidence_sets_validation_parent", "evidence_sets", ["validation_evidence_set_id"])
    op.create_index("ix_evidence_sets_prior_review_parent", "evidence_sets", ["prior_review_evidence_set_id"])
    op.create_table(
        "agent_execution_evidence_inputs",
        sa.Column("consumer_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("evidence_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("consumer_execution_id", "purpose"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["consumer_execution_id", "run_id"], ["agent_executions.id", "agent_executions.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_set_id", "run_id"], ["evidence_sets.id", "evidence_sets.run_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("(purpose = 'validation_results' AND evidence_kind = 'validation') OR (purpose = 'prior_review' AND evidence_kind = 'review')", name="purpose_kind"),
    )
    op.create_index("ix_agent_execution_evidence_inputs_set", "agent_execution_evidence_inputs", ["evidence_set_id", "run_id"])
    _immutable_trigger("evidence_sets")
    _immutable_trigger("agent_execution_evidence_inputs")


def downgrade() -> None:
    for table in ("agent_execution_evidence_inputs", "evidence_sets"):
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON {table}")
        op.execute(f"DROP FUNCTION forge_reject_{table}_mutation()")
    op.drop_table("agent_execution_evidence_inputs")
    op.drop_table("evidence_sets")
    op.drop_constraint("uq_agent_executions_id_run_step_role", "agent_executions", type_="unique")
    op.drop_constraint("uq_steps_id_run", "steps", type_="unique")
