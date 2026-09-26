"""Final-acceptance repair is atomic and tolerates a concurrent completed rejection."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case


async def observed_case(session_factory, tmp_path):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="c" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionAcceptanceInspection(factory, snapshot)
    await service.inspect(primary.attempt.attempt_id)
    return factory, primary, snapshot, service


@pytest.mark.integration
async def test_acceptance_rejection_rolls_back_epoch_receipt_and_budget_together(
    session_factory, tmp_path
):
    factory, primary, _, service = await observed_case(session_factory, tmp_path)
    async with factory() as work:
        proposal = await work.subscription_decisions.acceptance_proposal(primary.attempt.attempt_id)
        assert not (await work.subscription_decisions.reject_acceptance_mismatch(proposal)).accepted
        await work.rollback()
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert (
            source.disposition == "acceptance_prepared"
            and "rejection" not in source.application_payload
        )
        assert (
            scheduler.candidate_state == "closed"
            and scheduler.candidate_epoch == primary.candidate_epoch
        )
        assert await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is None
    assert not (await service.reject_mismatch(primary.attempt.attempt_id)).replayed


@pytest.mark.integration
async def test_rejection_survives_another_application_after_its_first_transaction(
    session_factory, tmp_path
):
    factory, primary, snapshot, service = await observed_case(session_factory, tmp_path)
    committed, resume = asyncio.Event(), asyncio.Event()
    paused = False

    @asynccontextmanager
    async def delayed_factory():
        async with factory() as work:
            original_commit = work.commit

            async def commit():
                nonlocal paused
                await original_commit()
                if not paused:
                    paused = True
                    committed.set()
                    await asyncio.wait_for(resume.wait(), 5)

            work.commit = commit
            yield work

    delayed = SubscriptionAcceptanceInspection(delayed_factory, snapshot)
    task = asyncio.create_task(delayed.reject_mismatch(primary.attempt.attempt_id))
    try:
        await asyncio.wait_for(committed.wait(), 5)
        first = await service.reject_mismatch(primary.attempt.attempt_id)
        assert not first.replayed
    finally:
        resume.set()
    second = await asyncio.wait_for(task, 5)
    assert second.replayed and second.disposition == first.disposition
