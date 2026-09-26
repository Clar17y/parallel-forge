"""A trusted acceptance mismatch starts one bounded repair cycle."""

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case
from test_subscription_usage import _reservation


@pytest.mark.integration
async def test_observed_acceptance_mismatch_reopens_once_and_returns_primary(
    session_factory, tmp_path
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    calls = []

    async def snapshot(proposal):
        calls.append(proposal)
        return GitWorkingTreeSnapshot(
            head_sha="c" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionAcceptanceInspection(factory, snapshot)
    observed = await service.inspect(primary.attempt.attempt_id)
    outcome = await service.reject_mismatch(primary.attempt.attempt_id)
    assert not outcome.accepted and outcome.disposition == "acceptance_repair_queued"
    assert (await service.reject_mismatch(primary.attempt.attempt_id)).replayed
    assert len(calls) == 1
    following = await SubscriptionDecisionExecutor(factory).admit_next(
        "primary-repairs-acceptance", _reservation()
    )
    assert following.task.task_id == primary.task.task_id
    assert following.candidate_epoch == primary.candidate_epoch + 1
    assert (await service.reject_mismatch(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        debit = await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id)
        assert source.application_payload["rejection"]["observation"] == observed.payload()
        assert debit.next_attempt_id == following.attempt.attempt_id
        assert scheduler.candidate_state == "open"
        assert (
            await work.subscription_decisions.review_selection_context(
                primary.task.run_id, primary.task.task_id
            )
            is None
        )
