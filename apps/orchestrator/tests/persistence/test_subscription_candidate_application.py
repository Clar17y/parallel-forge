"""A stopped review selection completes through every retained preparation phase."""

import asyncio

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_candidate import (
    SubscriptionCandidateApplication,
    SubscriptionCandidateInspection,
)
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_review_selection import selection_case


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["pending", "prepared", "observed"])
@pytest.mark.parametrize("review_required", [False, True])
async def test_candidate_application_resumes_selection_without_another_provider_attempt(
    session_factory, tmp_path, phase, review_required
):
    snapshot = GitWorkingTreeSnapshot(head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=())
    factory, admission, _ = await selection_case(
        session_factory, tmp_path, tree_digest=snapshot.candidate_tree_digest,
        candidate_commit=snapshot.head_sha, review_required=review_required,
    )
    if phase == "prepared":
        await SubscriptionDecisionApplication(factory).prepare_review_selection(admission.attempt.attempt_id)
    elif phase == "observed":
        async def initial(proposal):
            return snapshot
        await SubscriptionCandidateInspection(factory, initial).inspect(admission.attempt.attempt_id)
    async with factory() as work:
        before = await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
    captures = []

    async def capture(proposal):
        async with factory() as work:
            await asyncio.wait_for(work.runs.get_for_update(admission.task.run_id), 5)
        captures.append(proposal)
        return snapshot

    service = SubscriptionCandidateApplication(factory, capture)
    result = await service.apply(admission.attempt.attempt_id)
    assert result.accepted and result.disposition == "review_selected"
    assert (await service.apply(admission.attempt.attempt_id)).replayed
    assert len(captures) == (0 if phase == "observed" else 1)
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        parent = await work.session.get(SubscriptionTask, admission.task.task_id)
        assert scheduler.candidate_state == "closed"
        assert scheduler.candidate_epoch == admission.candidate_epoch + 1
        assert parent.state == ("blocked" if review_required else "queued")
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == before
        assert (await work.runs.get(admission.task.run_id)).pending_gate is None
