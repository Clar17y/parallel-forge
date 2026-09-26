"""Final validation admission fences drift, corruption and partial queue publication."""

from dataclasses import replace

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitSnapshotFile
from forge.domain.operation import canonical_digest
from forge.persistence.models import RunCommand
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionOperationBinding, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.runs import PostgresRunRepository
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_acceptance_dispatch import dispatch_case


@pytest.mark.integration
async def test_dispatch_rolls_back_queue_and_transition_then_retries(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    transition = PostgresRunRepository.transition

    async def crash(self, *args, **kwargs):
        await transition(self, *args, **kwargs)
        raise RuntimeError("crash after transition")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresRunRepository, "transition", crash)
        with pytest.raises(RuntimeError, match="crash after transition"):
            await service.apply(proposal.attempt_id)
    async with factory() as work:
        run = await work.runs.get(proposal.decision.run_id)
        assert run.state.value == "IMPLEMENTING" and run.version == proposal.run_version
        assert await work.commands.get_by_idempotency_key(f"{run.id}:validate:1") is None
        assert not [
            event
            for event in await work.events.list_after(run.id, 0)
            if event.event_type == "run.subscription_validation_requested"
        ]
    assert (await service.apply(proposal.attempt_id)).disposition == "acceptance_validation_queued"


@pytest.mark.integration
@pytest.mark.parametrize("observation", [1, 2])
async def test_candidate_drift_reopens_for_repair_without_validation(
    session_factory, tmp_path, observation
):
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    capture, calls = service._snapshot, 0

    async def drift(current):
        nonlocal calls
        calls += 1
        snapshot = await capture(current)
        return (
            replace(
                snapshot,
                files=(
                    GitSnapshotFile(
                        path="changed.py",
                        mode="100644",
                        content_digest="e" * 64,
                        byte_count=1,
                    ),
                ),
                changed_paths=("changed.py",),
            )
            if calls == observation
            else snapshot
        )

    service._snapshot = drift
    outcome = await service.apply(proposal.attempt_id)
    assert not outcome.accepted and outcome.disposition == "acceptance_repair_queued"
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, proposal.decision.run_id)
        assert (
            scheduler.candidate_state == "open"
            and scheduler.candidate_epoch == proposal.review.candidate_epoch + 1
        )
        assert (
            await work.commands.get_by_idempotency_key(f"{proposal.decision.run_id}:validate:1")
            is None
        )


@pytest.mark.integration
@pytest.mark.parametrize("change", ["epoch", "pause", "task_cancel", "callback"])
async def test_dispatch_reproves_authority_after_receipt_and_snapshot_io(
    session_factory, tmp_path, change
):
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    capture, calls = service._snapshot, 0

    async def changed(current):
        nonlocal calls
        calls += 1
        snapshot = await capture(current)
        if calls == 2:
            async with factory() as work:
                if change == "epoch":
                    (
                        await work.session.get(SubscriptionSchedulerRun, proposal.decision.run_id)
                    ).candidate_epoch += 1
                elif change == "pause":
                    run = await work.runs.get(proposal.decision.run_id)
                    await work.runs.pause(run.id, run.version, "test.paused", {})
                elif change == "task_cancel":
                    (
                        await work.session.get(SubscriptionTask, proposal.decision.task_id)
                    ).cancel_requested = True
                else:
                    binding = await work.session.scalar(
                        select(SubscriptionOperationBinding).where(
                            SubscriptionOperationBinding.attempt_id == proposal.attempt_id,
                        )
                    )
                    binding.receipt_payload = {**binding.receipt_payload, "accepted": False}
                await work.commit()
        return snapshot

    service._snapshot = changed
    with pytest.raises((SubscriptionDecisionError, ValueError)):
        await service.apply(proposal.attempt_id)
    async with factory() as work:
        assert (
            await work.commands.get_by_idempotency_key(f"{proposal.decision.run_id}:validate:1")
            is None
        )


@pytest.mark.integration
@pytest.mark.parametrize("change", ["actor", "version", "payload", "proof"])
async def test_dispatch_replay_refuses_changed_retained_authority(
    session_factory, tmp_path, change
):
    factory, proposal, service, observations = await dispatch_case(session_factory, tmp_path)
    await service.apply(proposal.attempt_id)
    count = len(observations)
    async with factory() as work:
        command = await work.session.scalar(
            select(RunCommand).where(
                RunCommand.run_id == proposal.decision.run_id,
                RunCommand.command_type == "validate",
            )
        )
        if change == "actor":
            command.actor_id = None
        elif change == "version":
            command.expected_run_version += 1
        elif change == "payload":
            command.payload = {"semantic_attempt": 1}
        else:
            result = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
            result.application_payload = {
                **result.application_payload,
                "receipt_verification": {
                    **result.application_payload["receipt_verification"],
                    "result_digest": "f" * 64,
                },
            }
            result.application_digest = canonical_digest(result.application_payload)
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await service.apply(proposal.attempt_id)
    assert len(observations) == count
