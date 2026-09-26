"""Final inspection retains the actual selected review attempt and its verdict."""

from uuid import UUID, uuid4

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.ports.subscription_handoff import (
    HandoffCallProof,
    VerifiedSubscriptionHandoff,
)
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.agent import ReviewDecision
from forge.domain.operation import canonical_digest
from forge.domain.subscription import AcceptDecision, TaskBudget, encode_subscription_record
from forge.persistence.repositories.subscription_handoff_evidence import (
    PostgresSubscriptionHandoffEvidence,
)
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_review_handoff import review_handoff_case
from test_subscription_review_report import reviewed
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
@pytest.mark.parametrize("verdict", list(ReviewDecision))
async def test_final_acceptance_inspection_requires_selected_approving_review(
    session_factory, tmp_path, monkeypatch, verdict
):
    factory, child, handoff = await review_handoff_case(
        session_factory,
        tmp_path,
        primary_budget=TaskBudget(max_provider_attempts=8),
        mutate=lambda value: reviewed(value, verdict),
    )
    application = SubscriptionDecisionApplication(factory)
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    identity = UUID(handoff.evidence_receipt_ids[0])
    proof = VerifiedSubscriptionHandoff(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id,
        snapshot_call_id=identity,
        manifest_digest="d" * 64,
        candidate_tree_digest=handoff.candidate_tree_digest,
        policy_version=child.envelope.safety_policy_version,
        task_digest=canonical_digest(encode_subscription_record(child.task)),
        handoff_digest=canonical_digest(encode_subscription_record(handoff)),
        call_proofs=(HandoffCallProof(identity, "e" * 64, "f" * 64),),
        artifact_proofs=(("d" * 64, "1" * 64),),
        checks_match_snapshot=True,
        output_digest="2" * 64,
        current_tree_digest=handoff.candidate_tree_digest,
    )

    async def verified(self, value):
        return value == proof

    monkeypatch.setattr(PostgresSubscriptionHandoffEvidence, "verify", verified)
    await application.apply_handoff(observation, proof)
    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("primary-final-acceptance", _reservation())
    decision = AcceptDecision(
        run_id=primary.task.run_id,
        task_id=primary.task.task_id,
        candidate_commit=handoff.candidate_commit,
        candidate_tree_digest=handoff.candidate_tree_digest,
        evidence_receipt_ids=handoff.evidence_receipt_ids,
        rationale="Accept reviewed candidate",
    )
    launch = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt,
                decision=decision,
                telemetry=_known(),
                launch_proof=launch,
            ),
        )
    ).disposition == "decision_pending"
    result = await application.prepare_acceptance(primary.attempt.attempt_id)
    if verdict is not ReviewDecision.APPROVE:
        assert not result.accepted
        return
    assert result.accepted and result.disposition == "acceptance_prepared"
    inspected = []

    async def snapshot(proposal):
        assert proposal.review.review_attempt_id == child.attempt.attempt_id
        assert proposal.review.review_handoff == handoff
        inspected.append(proposal)
        return GitWorkingTreeSnapshot(
            head_sha=handoff.candidate_commit,
            base_sha=proposal.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    observed = await SubscriptionAcceptanceInspection(factory, snapshot).inspect(
        primary.attempt.attempt_id
    )
    assert observed.tree_digest == decision.candidate_tree_digest and len(inspected) == 1
