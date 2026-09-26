"""Retain candidate contents and actual subscription evidence producers.

Revision ID: 20260911_0018
Revises: 20260911_0017
"""

import sqlalchemy as sa
from alembic import op

revision = "20260911_0018"
down_revision = "20260911_0017"
branch_labels = None
depends_on = None

_LEGACY_SHAPE = "(kind = 'validation' AND producer_execution_id IS NULL AND producer_step_id IS NULL AND producer_role IS NULL AND validation_evidence_set_id IS NULL AND validation_parent_policy_version IS NULL AND validation_parent_kind IS NULL AND validation_parent_head_sha IS NULL AND ((prior_review_evidence_set_id IS NULL AND prior_review_parent_policy_version IS NULL AND prior_review_parent_kind IS NULL) OR (prior_review_evidence_set_id IS NOT NULL AND prior_review_parent_policy_version IS NOT NULL AND prior_review_parent_kind = 'review')) AND review_finding_ids IS NULL) OR (kind = 'review' AND producer_execution_id IS NOT NULL AND producer_step_id IS NOT NULL AND producer_role = 'reviewer' AND validation_evidence_set_id IS NOT NULL AND validation_parent_policy_version IS NOT NULL AND validation_parent_kind = 'validation' AND validation_parent_head_sha IS NOT NULL AND prior_review_evidence_set_id IS NULL AND prior_review_parent_policy_version IS NULL AND prior_review_parent_kind IS NULL AND review_finding_ids IS NOT NULL)"
_ACCEPTANCE_SHAPE = "(kind = 'acceptance' AND producer_execution_id IS NULL AND producer_step_id IS NULL AND producer_role IS NULL AND validation_evidence_set_id IS NOT NULL AND validation_parent_policy_version IS NOT NULL AND validation_parent_kind IS NOT NULL AND validation_parent_kind = 'validation' AND validation_parent_head_sha IS NOT NULL AND prior_review_evidence_set_id IS NULL AND prior_review_parent_policy_version IS NULL AND prior_review_parent_kind IS NULL)"
_PRODUCER = "((producer_task_id IS NULL) = (producer_attempt_id IS NULL)) AND ((kind = 'acceptance') = (producer_attempt_id IS NOT NULL)) AND (kind <> 'acceptance' OR candidate_tree_digest IS NOT NULL)"
_TREE = "candidate_tree_digest IS NULL OR (kind IN ('validation','acceptance') AND candidate_tree_digest ~ '^[0-9a-f]{64}$')"


def upgrade() -> None:
    op.add_column("evidence_sets", sa.Column("candidate_tree_digest", sa.String(64)))
    op.add_column("evidence_sets", sa.Column("producer_task_id", sa.Uuid()))
    op.add_column("evidence_sets", sa.Column("producer_attempt_id", sa.Uuid()))
    op.drop_constraint("kind", "evidence_sets", type_="check")
    op.drop_constraint("shape", "evidence_sets", type_="check")
    op.create_check_constraint(
        "kind", "evidence_sets", "kind IN ('validation','review','acceptance')"
    )
    op.create_check_constraint(
        "shape", "evidence_sets", f"({_LEGACY_SHAPE}) OR {_ACCEPTANCE_SHAPE}"
    )
    op.create_check_constraint("subscription_producer", "evidence_sets", _PRODUCER)
    op.create_check_constraint("candidate_tree", "evidence_sets", _TREE)
    op.create_foreign_key(
        "fk_evidence_sets_subscription_producer",
        "evidence_sets",
        "subscription_attempts",
        ["run_id", "producer_task_id", "producer_attempt_id"],
        ["run_id", "task_row_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_evidence_sets_subscription_producer", "evidence_sets", ["producer_attempt_id"]
    )


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM evidence_sets WHERE kind = 'acceptance' OR candidate_tree_digest IS NOT NULL LIMIT 1"
            )
        )
        .scalar()
    )
    if retained is not None:
        raise RuntimeError("subscription and candidate-content evidence must not be discarded")
    op.drop_index("ix_evidence_sets_subscription_producer", table_name="evidence_sets")
    op.drop_constraint(
        "fk_evidence_sets_subscription_producer", "evidence_sets", type_="foreignkey"
    )
    for name in ("candidate_tree", "subscription_producer", "shape", "kind"):
        op.drop_constraint(name, "evidence_sets", type_="check")
    op.create_check_constraint("kind", "evidence_sets", "kind IN ('validation','review')")
    op.create_check_constraint("shape", "evidence_sets", _LEGACY_SHAPE)
    for name in ("producer_attempt_id", "producer_task_id", "candidate_tree_digest"):
        op.drop_column("evidence_sets", name)
