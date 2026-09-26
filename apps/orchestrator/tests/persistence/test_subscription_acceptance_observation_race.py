"""Concurrent observers retain one consistent acceptance observation."""

import asyncio

import pytest
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case


@pytest.mark.integration
@pytest.mark.parametrize("different", [False, True])
async def test_concurrent_acceptance_observation_keeps_one_identity(
    session_factory, tmp_path, different
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    both_ready, arrived = asyncio.Event(), []

    async def snapshot(proposal):
        index = len(arrived)
        arrived.append(proposal)
        if len(arrived) == 2:
            both_ready.set()
        await asyncio.wait_for(both_ready.wait(), 5)
        return GitWorkingTreeSnapshot(
            head_sha=("c" if different and index else "b") * 40,
            base_sha=proposal.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    inspector = SubscriptionAcceptanceInspection(factory, snapshot)
    outcomes = await asyncio.gather(
        *(inspector.inspect(primary.attempt.attempt_id) for _ in range(2)),
        return_exceptions=True,
    )
    observations = [item for item in outcomes if isinstance(item, CandidateInspection)]
    errors = [item for item in outcomes if isinstance(item, SubscriptionDecisionError)]
    assert len(observations) == (1 if different else 2)
    assert len(errors) == int(different)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.application_payload["observation"] == observations[0].payload()
        assert result.disposition == "acceptance_prepared"


@pytest.mark.integration
async def test_cancelled_acceptance_observation_leaves_prepared_source(session_factory, tmp_path):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    observing = asyncio.Event()

    async def snapshot(proposal):
        observing.set()
        await asyncio.Event().wait()

    process = asyncio.create_task(
        SubscriptionAcceptanceInspection(factory, snapshot).inspect(primary.attempt.attempt_id)
    )
    await asyncio.wait_for(observing.wait(), 5)
    process.cancel()
    with pytest.raises(asyncio.CancelledError):
        await process
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.disposition == "acceptance_prepared"
        assert "observation" not in result.application_payload
