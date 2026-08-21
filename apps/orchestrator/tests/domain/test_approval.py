from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.approval import (
    ApprovalGate,
    ApprovalRecord,
    MergeApprovalEvidence,
    PlanApprovalEvidence,
    PrApprovalEvidence,
    canonical_digest,
)
from forge.domain.policy import RunnerMode
from pydantic import ValidationError


def merge_evidence(**overrides: object) -> MergeApprovalEvidence:
    values: dict[str, object] = {
        "repository": "Clar17y/Parallel",
        "pull_request_number": 42,
        "head_sha": "a" * 40,
        "base_ref": "main",
        "base_sha": "b" * 40,
        "required_checks": {"test": "success", "typecheck": "success"},
        "unresolved_blocking_findings": 0,
        "validation_digest": "c" * 64,
        "review_digest": "d" * 64,
        "runner_mode": RunnerMode.DOCKER,
        "runner_evidence_digest": "e" * 64,
        "merge_method": "squash",
        "policy_version": 3,
    }
    values.update(overrides)
    return MergeApprovalEvidence(**values)


def plan_evidence(**overrides: object) -> PlanApprovalEvidence:
    values: dict[str, object] = {
        "task_version": 1,
        "plan_digest": "a" * 64,
        "repository": "Clar17y/Parallel",
        "base_ref": "main",
        "base_sha": "b" * 40,
        "policy_version": 3,
        "required_checks": {"test": "success"},
        "runner_mode": RunnerMode.DOCKER,
        "local_remediation_limit": 3,
        "token_budget": 1000,
        "cost_budget_minor": 100,
        "duration_budget_seconds": 600,
    }
    values.update(overrides)
    return PlanApprovalEvidence(**values)


def test_merge_digest_changes_for_head_base_or_runner() -> None:
    evidence = merge_evidence()

    assert canonical_digest(evidence) != canonical_digest(
        evidence.model_copy(update={"head_sha": "c" * 40})
    )
    assert canonical_digest(evidence) != canonical_digest(
        evidence.model_copy(update={"base_sha": "d" * 40})
    )
    assert canonical_digest(evidence) != canonical_digest(
        evidence.model_copy(update={"runner_mode": RunnerMode.TRUSTED_HOST})
    )
    assert canonical_digest(evidence) != canonical_digest(
        evidence.model_copy(update={"runner_evidence_digest": "f" * 64})
    )


def test_canonical_digest_is_stable_for_mapping_and_set_like_order() -> None:
    first = PlanApprovalEvidence(
        task_version=1,
        plan_digest="a" * 64,
        repository="Clar17y/Parallel",
        base_ref="main",
        base_sha="b" * 40,
        policy_version=3,
        dependency_changes=frozenset({"pydantic", "pytest"}),
        required_checks={"test": "success", "lint": "success"},
        runner_mode=RunnerMode.DOCKER,
        local_remediation_limit=3,
        token_budget=1000,
        cost_budget_minor=100,
        duration_budget_seconds=600,
    )
    second = first.model_copy(
        update={
            "dependency_changes": frozenset({"pytest", "pydantic"}),
            "required_checks": {"lint": "success", "test": "success"},
        }
    )

    assert canonical_digest(first) == canonical_digest(second)


@pytest.mark.parametrize("evidence", [plan_evidence(), merge_evidence()])
def test_evidence_required_checks_cannot_be_mutated_after_creation(
    evidence: PlanApprovalEvidence | MergeApprovalEvidence,
) -> None:
    with pytest.raises(TypeError):
        evidence.required_checks["lint"] = "success"


def test_canonical_digest_is_sensitive_to_ordered_evidence() -> None:
    first = PrApprovalEvidence(
        candidate_commit="a" * 40,
        diff_digest="b" * 64,
        validation_digest="c" * 64,
        review_digest="d" * 64,
        repository="Clar17y/Parallel",
        base_ref="main",
        base_sha="e" * 40,
        title="Implement policy",
        body_digest="f" * 64,
        runner_mode=RunnerMode.DOCKER,
        runner_evidence_digest="0" * 64,
        remote_remediation_limit=3,
    )
    second = first.model_copy(update={"title": "Implement approvals"})

    assert canonical_digest(first) != canonical_digest(second)


def test_approval_record_is_valid_only_when_every_bound_value_matches() -> None:
    run_id = uuid4()
    actor_id = "operator-1"
    record = ApprovalRecord(
        gate=ApprovalGate.MERGE,
        evidence_digest=canonical_digest(merge_evidence()),
        run_id=run_id,
        run_version=7,
        policy_version=3,
        authenticated_actor_id=actor_id,
        created_at=datetime(2026, 8, 22, tzinfo=UTC),
    )

    assert record.is_valid_for(
        ApprovalGate.MERGE,
        record.evidence_digest,
        run_id,
        7,
        3,
        actor_id,
    )
    assert not record.is_valid_for(
        ApprovalGate.PLAN, record.evidence_digest, run_id, 7, 3, actor_id
    )
    assert not record.is_valid_for(ApprovalGate.MERGE, "0" * 64, run_id, 7, 3, actor_id)
    assert not record.is_valid_for(
        ApprovalGate.MERGE, record.evidence_digest, uuid4(), 7, 3, actor_id
    )
    assert not record.is_valid_for(
        ApprovalGate.MERGE, record.evidence_digest, run_id, 8, 3, actor_id
    )
    assert not record.is_valid_for(
        ApprovalGate.MERGE, record.evidence_digest, run_id, 7, 4, actor_id
    )
    assert not record.is_valid_for(
        ApprovalGate.MERGE, record.evidence_digest, run_id, 7, 3, "operator-2"
    )


def test_invalidated_approval_fails_closed_even_for_matching_evidence() -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    run_id = uuid4()
    record = ApprovalRecord(
        gate=ApprovalGate.PR,
        evidence_digest="a" * 64,
        run_id=run_id,
        run_version=1,
        policy_version=1,
        authenticated_actor_id="operator-1",
        created_at=now,
        invalidated_at=now + timedelta(seconds=1),
    )

    assert not record.is_valid_for(ApprovalGate.PR, "a" * 64, run_id, 1, 1, "operator-1")


def test_evidence_and_approval_models_reject_unknown_fields_and_bad_digests() -> None:
    with pytest.raises(ValidationError):
        merge_evidence(unknown_field="reject-me")
    with pytest.raises(ValidationError):
        merge_evidence(head_sha="not-a-sha")
    with pytest.raises(ValidationError):
        ApprovalRecord(
            gate=ApprovalGate.PLAN,
            evidence_digest="not-a-digest",
            run_id=uuid4(),
            run_version=1,
            policy_version=1,
            authenticated_actor_id="operator-1",
            created_at=datetime.now(UTC),
        )
