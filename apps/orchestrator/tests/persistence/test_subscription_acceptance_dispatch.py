"""A verified primary acceptance atomically queues candidate-bound validation."""

import asyncio
from functools import partial
from types import SimpleNamespace

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance_dispatch import (
    SubscriptionAcceptanceDispatch,
)
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.run import RunState
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_acceptance_preparation import acceptance_case
from test_subscription_acceptance_receipt_sources import receipt_case


async def dispatch_case(session_factory, tmp_path, *, plan_scope=None, acceptance_factory=None):
    factory, proposal, _, _, data = await receipt_case(
        session_factory,
        tmp_path,
        acceptance_factory=acceptance_factory or partial(acceptance_case, plan_scope=plan_scope),
    )
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    await store.put_bytes(data, media_type="application/json")
    observations = []

    async def snapshot(current):
        async with factory() as work:
            await asyncio.wait_for(work.runs.get_for_update(current.decision.run_id), 5)
        observations.append(current)
        return GitWorkingTreeSnapshot(
            head_sha=current.review.candidate.head_sha,
            base_sha=current.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    service = SubscriptionAcceptanceDispatch(
        factory,
        store,
        snapshot,
        SubscriptionAcceptanceReceiptVerification(factory, store, SimpleNamespace()),
    )
    return factory, proposal, service, observations


@pytest.mark.integration
async def test_acceptance_queues_validation_once_without_opening_a_human_gate(
    session_factory, tmp_path
):
    factory, proposal, service, observations = await dispatch_case(session_factory, tmp_path)
    outcome = await service.apply(proposal.attempt_id)
    assert outcome.accepted and outcome.disposition == "acceptance_validation_queued"
    async with factory() as work:
        run = await work.runs.get(proposal.decision.run_id)
        assert run.state is RunState.VALIDATING and run.version == proposal.run_version + 1
        assert run.pending_gate is None
        queued = await work.commands.get_by_idempotency_key(f"{run.id}:validate:1")
        assert queued is not None
        assert queued.payload == {
            "semantic_attempt": 1,
            "acceptance_attempt_id": str(proposal.attempt_id),
        }
        assert queued.expected_run_version == run.version and queued.actor_id is not None
        scheduler = await work.session.get(SubscriptionSchedulerRun, run.id)
        task = await work.session.get(SubscriptionTask, proposal.decision.task_id)
        assert scheduler.candidate_state == "closed" and task.state == "blocked"
        assert scheduler.candidate_epoch == proposal.review.candidate_epoch
        events = [
            event
            for event in await work.events.list_after(run.id, 0)
            if event.event_type == "run.subscription_validation_requested"
        ]
        assert len(events) == 1
        assert events[0].payload["binding"]["candidate"] == proposal.review.candidate.payload()
        assert events[0].payload["binding"]["source_result_digest"] == proposal.result_digest
    count = len(observations)
    assert (await service.apply(proposal.attempt_id)).replayed
    assert len(observations) == count


@pytest.mark.integration
async def test_concurrent_dispatch_replays_if_another_caller_finishes_during_receipt_io(
    session_factory, tmp_path
):
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    both_read, first_finished = asyncio.Event(), asyncio.Event()
    snapshot, verify = service._snapshot, service._receipts.verify
    observations = verifications = 0

    async def rendezvous(current):
        nonlocal observations
        observations += 1
        if observations <= 2:
            if observations == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), 5)
        return await snapshot(current)

    async def delayed(attempt_id):
        nonlocal verifications
        verifications += 1
        if verifications == 2:
            await asyncio.wait_for(first_finished.wait(), 5)
        return await verify(attempt_id)

    service._snapshot, service._receipts.verify = rendezvous, delayed

    async def apply():
        result = await service.apply(proposal.attempt_id)
        first_finished.set()
        return result

    outcomes = await asyncio.gather(apply(), apply())
    assert all(outcome.accepted for outcome in outcomes)
    assert sum(outcome.replayed for outcome in outcomes) == 1
    async with factory() as work:
        assert (
            len(
                [
                    event
                    for event in await work.events.list_after(proposal.decision.run_id, 0)
                    if event.event_type == "run.subscription_validation_requested"
                ]
            )
            == 1
        )
