"""Reopening respects outstanding observation and concurrent application."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_candidate_redelegation import redelegation_case


@pytest.mark.integration
async def test_active_snapshot_fence_prevents_candidate_reopening(session_factory, tmp_path):
    factory, primary, _ = await redelegation_case(session_factory, tmp_path)
    async with factory() as work:
        task = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        work.session.add(
            SubscriptionHandoffFence(
                worktree_id=task.worktree_id,
                run_id=primary.task.run_id,
                attempt_id=primary.attempt.attempt_id,
                token=uuid4(),
                result_digest="a" * 64,
                expires_at=datetime.now(UTC) + timedelta(seconds=60),
            )
        )
        await work.commit()
    with pytest.raises(SubscriptionDecisionError, match="quiescent"):
        await SubscriptionDecisionApplication(factory).apply_delegation(primary.attempt.attempt_id)
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert (
            scheduler.candidate_state == "closed"
            and scheduler.candidate_epoch == primary.candidate_epoch
        )


@pytest.mark.integration
async def test_concurrent_redelegation_advances_candidate_once(session_factory, tmp_path):
    factory, primary, _ = await redelegation_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    results = await asyncio.gather(
        *(service.apply_delegation(primary.attempt.attempt_id) for _ in range(2))
    )
    assert all(result.accepted for result in results)
    assert sum(result.replayed for result in results) == 1
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert scheduler.candidate_epoch == primary.candidate_epoch + 1
