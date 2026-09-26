"""Actual selection and acceptance with a fresh controlled snapshot receipt."""

from dataclasses import replace
from uuid import uuid4

from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_candidate import SubscriptionCandidateApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from subscription_launch_fixture import record_stopped_launch
from test_subscription_acceptance_receipt_sources import record_snapshot_receipt
from test_subscription_usage import _known, _reservation


async def accept_changed_candidate(factory, session_factory, original, dispatch, git):
    executor = SubscriptionDecisionExecutor(factory)
    selecting = await executor.admit_next("primary-selects-changed-candidate", _reservation())
    assert selecting is not None
    candidate = git.working_tree_snapshot(
        original.worktree, secret_paths=original.policy.effective_secret_paths
    )
    selection = replace(
        original.review.selection,
        candidate_commit=candidate.head_sha,
        candidate_tree_digest=candidate.candidate_tree_digest,
        no_review_reason="Reassess the adopted candidate within the approved scope",
    )
    assert (
        await executor.settle(
            selecting,
            SubscriptionInvocationResult(
                attempt=selecting.attempt,
                decision=selection,
                telemetry=_known(),
                launch_proof=await record_stopped_launch(session_factory, selecting),
            ),
        )
    ).disposition == "decision_pending"

    async def capture(current):
        return git.working_tree_snapshot(
            current.worktree, secret_paths=current.policy.effective_secret_paths
        )

    assert (
        await SubscriptionCandidateApplication(factory, capture).apply(selecting.attempt.attempt_id)
    ).disposition == "review_selected"
    accepting = await executor.admit_next("primary-accepts-changed-candidate", _reservation())
    assert accepting is not None and accepting.attempt.attempt_id != original.attempt_id
    call, _, data = await record_snapshot_receipt(
        factory, accepting, uuid4(), candidate, original.worktree, original.policy.version
    )
    await dispatch._store.put_bytes(data, media_type="application/json")
    decision = replace(
        original.decision,
        candidate_commit=candidate.head_sha,
        candidate_tree_digest=candidate.candidate_tree_digest,
        evidence_receipt_ids=(str(call.id),),
        rationale="Accept the newly observed candidate for fresh final checks",
    )
    assert (
        await executor.settle(
            accepting,
            SubscriptionInvocationResult(
                attempt=accepting.attempt,
                decision=decision,
                telemetry=_known(),
                launch_proof=await record_stopped_launch(session_factory, accepting),
            ),
        )
    ).disposition == "decision_pending"
    assert (
        await dispatch.apply(accepting.attempt.attempt_id)
    ).disposition == "acceptance_validation_queued"
    return selecting, accepting
