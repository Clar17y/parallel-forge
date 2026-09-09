"""Bind managed PR identities to durable external operation receipts.

Revision ID: 20260908_0005
Revises: 20260908_0004
"""

import sqlalchemy as sa
from alembic import op

revision = "20260908_0005"
down_revision = "20260908_0004"
branch_labels = depends_on = None


def upgrade():
    _replace_suspension_constraint(include_pr=True)
    for name, size in (
        ("node_id", 255),
        ("url", 1024),
        ("head_repository", 512),
        ("merge_sha", 40),
        ("candidate_evidence_digest", 64),
    ):
        op.add_column("pull_requests", sa.Column(name, sa.String(size), nullable=True))
    for name in (
        "push_intent_id",
        "publication_intent_id",
        "merge_intent_id",
        "reviewed_push_intent_id",
        "base_update_intent_id",
        "base_adoption_intent_id",
    ):
        op.add_column("pull_requests", sa.Column(name, sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_pull_requests_{name}_operation_intents",
            "pull_requests",
            "operation_intents",
            [name],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_check_constraint(
        op.f("ck_pull_requests_base_update_receipt_pair"),
        "pull_requests",
        "(base_update_intent_id IS NULL) = (base_adoption_intent_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_pull_requests_reviewed_candidate_binding"),
        "pull_requests",
        "(reviewed_push_intent_id IS NULL AND candidate_evidence_digest IS NULL) OR "
        "(reviewed_push_intent_id IS NOT NULL AND candidate_evidence_digest IS NOT NULL "
        "AND candidate_evidence_digest ~ '^[0-9a-f]{64}$')",
    )


def downgrade():
    # Refuse incompatible rows rather than inventing a different suspended phase.
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM runs WHERE state = 'AWAITING_HUMAN_INTERVENTION' "
                "AND suspended_state = 'AWAITING_PR_APPROVAL')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("PR approval intervention must be resolved before downgrade")
    _replace_suspension_constraint(include_pr=False)
    op.drop_constraint(
        op.f("ck_pull_requests_base_update_receipt_pair"), "pull_requests", type_="check"
    )
    op.drop_constraint(
        op.f("ck_pull_requests_reviewed_candidate_binding"), "pull_requests", type_="check"
    )
    for name in (
        "base_adoption_intent_id",
        "base_update_intent_id",
        "reviewed_push_intent_id",
        "merge_intent_id",
        "publication_intent_id",
        "push_intent_id",
    ):
        op.drop_constraint(
            f"fk_pull_requests_{name}_operation_intents", "pull_requests", type_="foreignkey"
        )
        op.drop_column("pull_requests", name)
    for name in ("candidate_evidence_digest", "merge_sha", "head_repository", "url", "node_id"):
        op.drop_column("pull_requests", name)


def _replace_suspension_constraint(*, include_pr):
    paused = "'CREATED','PLANNING','AWAITING_PLAN_APPROVAL','PREPARING_WORKTREE','IMPLEMENTING','VALIDATING','REVIEWING','REMEDIATING','AWAITING_PR_APPROVAL','PUBLISHING_PR','MONITORING_PR','AWAITING_HUMAN_INTERVENTION','AWAITING_MERGE_APPROVAL','MERGING'"
    intervention = "'PLANNING','PREPARING_WORKTREE','IMPLEMENTING','VALIDATING','REVIEWING','REMEDIATING','PUBLISHING_PR','MONITORING_PR','AWAITING_MERGE_APPROVAL','MERGING'"
    if include_pr:
        intervention += ",'AWAITING_PR_APPROVAL'"
    name = op.f("ck_runs_suspension_state_shape")
    op.drop_constraint(name, "runs", type_="check")
    op.create_check_constraint(
        name,
        "runs",
        (
            "(state = 'PAUSED' AND suspended_state IS NOT NULL "
            f"AND suspended_state IN ({paused}) AND suspension_kind IS NOT NULL "
            "AND suspension_kind = 'PAUSE' AND suspension_context IS NOT NULL "
            "AND suspension_context_schema_version IS NOT NULL AND suspension_context_schema_version >= 1) OR "
            "(state = 'AWAITING_HUMAN_INTERVENTION' AND suspended_state IS NOT NULL "
            f"AND suspended_state IN ({intervention}) AND suspension_kind IS NOT NULL "
            "AND suspension_kind = 'INTERVENTION' AND suspension_context IS NULL "
            "AND suspension_context_schema_version IS NULL) OR "
            "(state NOT IN ('PAUSED','AWAITING_HUMAN_INTERVENTION') AND suspended_state IS NULL "
            "AND suspension_kind IS NULL AND suspension_context IS NULL AND suspension_context_schema_version IS NULL)"
        ),
    )
