"""A failed check requires fresh primary selection and acceptance before publication."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_candidate import SubscriptionCandidateApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.approval import decode_pr_approval_evidence
from forge.domain.evidence import decode_evidence_manifest
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_usage import _known, _reservation
from test_subscription_validation_repair import failed_validation_case


async def accept_reopened_candidate(factory, session_factory, original, dispatch, git):
    """Run the actual selection/acceptance pipeline for an unchanged reopened candidate."""
    executor = SubscriptionDecisionExecutor(factory)
    selection = await executor.admit_next("primary-selects-retry", _reservation())
    assert (
        selection is not None and selection.candidate_epoch == original.review.candidate_epoch + 1
    )
    proposed = replace(
        original.review.selection,
        no_review_reason="Recheck the unchanged candidate within approved scope",
    )
    assert (
        await executor.settle(
            selection,
            SubscriptionInvocationResult(
                attempt=selection.attempt,
                decision=proposed,
                telemetry=_known(),
                launch_proof=await record_stopped_launch(session_factory, selection),
            ),
        )
    ).disposition == "decision_pending"

    async def snapshot(current):
        return git.working_tree_snapshot(
            current.worktree, secret_paths=current.policy.effective_secret_paths
        )

    assert (
        await SubscriptionCandidateApplication(factory, snapshot).apply(
            selection.attempt.attempt_id
        )
    ).disposition == "review_selected"
    accepting = await executor.admit_next("primary-accepts-retry", _reservation())
    assert (
        accepting is not None and accepting.candidate_epoch == original.review.candidate_epoch + 2
    )
    assert accepting.attempt.attempt_id != original.attempt_id
    accepted = replace(
        original.decision,
        rationale="Accept the newly selected candidate for fresh controller validation",
    )
    assert (
        await executor.settle(
            accepting,
            SubscriptionInvocationResult(
                attempt=accepting.attempt,
                decision=accepted,
                telemetry=_known(),
                launch_proof=await record_stopped_launch(session_factory, accepting),
            ),
        )
    ).disposition == "decision_pending"
    assert (
        await dispatch.apply(accepting.attempt.attempt_id)
    ).disposition == "acceptance_validation_queued"
    return selection, accepting


@pytest.mark.integration
async def test_retry_of_transient_check_failure_requires_new_acceptance(session_factory, tmp_path):
    factory, original, dispatch, failed, controller, runner, git = await failed_validation_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        rejection = await controller.validate(failed, work)
    assert rejection.state is RunState.REMEDIATING
    commands = PostgresCommandRepository(session_factory)
    await commands.complete(failed.id, worker_id=failed.lease_owner)
    selection, accepting = await accept_reopened_candidate(
        factory, session_factory, original, dispatch, git
    )
    queued = await commands.claim_next(worker_id="retry-validation", lease_seconds=120)
    assert queued is not None and queued.command_type == "validate"
    assert queued.payload["acceptance_attempt_id"] == str(accepting.attempt.attempt_id)
    assert queued.id != failed.id and queued.payload["semantic_attempt"] == 2
    # The transient controller failure clears; the source is unchanged. Fresh
    # primary acceptance and a new controller receipt are still required.
    runner.runner.terminal = replace(
        runner.runner.terminal,
        result=replace(runner.runner.terminal.result, exit_code=0, started_at=datetime.now(UTC)),
    )
    async with factory() as work:
        publication = await controller.validate(queued, work)
    assert publication.state is RunState.AWAITING_PR_APPROVAL
    evidence = decode_pr_approval_evidence(
        await dispatch._store.open_bytes(publication.pr_evidence_digest)
    )
    acceptance = decode_evidence_manifest(
        await dispatch._store.open_bytes(evidence.acceptance_digest)
    )
    assert acceptance.producer_attempt_id == accepting.attempt.attempt_id
    assert acceptance.candidate_epoch == accepting.candidate_epoch
    assert acceptance.selection_attempt_id == selection.attempt.attempt_id
    async with factory() as work:
        assert (await work.runs.get(queued.run_id)).local_remediation_count == 1
        assert (
            await work.subscription_decisions.acceptance_validation_binding(original.attempt_id)
            is not None
        )
    assert runner.calls == runner.runner.calls == 2
