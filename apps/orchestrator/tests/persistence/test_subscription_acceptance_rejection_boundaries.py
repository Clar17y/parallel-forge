"""Acceptance recovery retains evidence, authority, and bounded repair accounting."""

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case


@pytest.mark.integration
async def test_later_git_mismatch_retains_first_observation_and_rejects_current_candidate(
    session_factory, tmp_path
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    head = "b" * 40

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha=head, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionAcceptanceInspection(factory, snapshot)
    original = await service.inspect(primary.attempt.attempt_id)
    head = "c" * 40
    outcome = await service.reject_mismatch(primary.attempt.attempt_id)
    assert not outcome.accepted and outcome.disposition == "acceptance_repair_queued"
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert source.application_payload["observation"] == original.payload()
        assert source.application_payload["rejection"]["observation"]["head_sha"] == head


@pytest.mark.integration
async def test_matching_candidate_cannot_consume_acceptance_repair(session_factory, tmp_path):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    with pytest.raises(SubscriptionDecisionError, match="no observed candidate mismatch"):
        await SubscriptionAcceptanceInspection(factory, snapshot).reject_mismatch(
            primary.attempt.attempt_id
        )
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert source.disposition == "acceptance_prepared"
        assert await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is None


@pytest.mark.integration
async def test_exhausted_acceptance_repair_is_terminal_and_replayable(session_factory, tmp_path):
    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from forge.persistence.models.subscription import SubscriptionTask

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="c" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionAcceptanceInspection(factory, snapshot)
    await service.inspect(primary.attempt.attempt_id)
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        scheduled.repairs = scheduled.max_repairs
        await work.commit()
    result = await service.reject_mismatch(primary.attempt.attempt_id)
    assert result.disposition == "acceptance_rejected" and not result.accepted
    assert (await service.reject_mismatch(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, primary.task.task_id)
        assert task.state == "terminal"
        assert await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is None


@pytest.mark.integration
async def test_rejection_revalidates_cancellation_after_new_git_observation(
    session_factory, tmp_path
):
    from forge.persistence.models.subscription import SubscriptionTask

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def snapshot(proposal):
        async with factory() as work:
            task = await work.session.get(SubscriptionTask, primary.task.task_id)
            task.cancel_requested = True
            await work.commit()
        return GitWorkingTreeSnapshot(
            head_sha="c" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionAcceptanceInspection(factory, snapshot).reject_mismatch(
            primary.attempt.attempt_id
        )
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert (
            source.disposition == "acceptance_prepared"
            and "rejection" not in source.application_payload
        )


@pytest.mark.integration
async def test_concurrent_acceptance_rejection_debits_one_repair(session_factory, tmp_path):
    import asyncio

    from sqlalchemy import func, select

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="c" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionAcceptanceInspection(factory, snapshot)
    await service.inspect(primary.attempt.attempt_id)
    results = await asyncio.gather(
        *(service.reject_mismatch(primary.attempt.attempt_id) for _ in range(2))
    )
    assert sorted(result.replayed for result in results) == [False, True]
    async with factory() as work:
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionRepairDebit))
            == 1
        )


@pytest.mark.integration
@pytest.mark.parametrize("corruption", ["observation", "epoch", "receipt", "debit"])
async def test_rejection_replay_refuses_corrupt_recovery_evidence(
    session_factory, tmp_path, corruption
):
    from forge.domain.operation import canonical_digest

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="c" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionAcceptanceInspection(factory, snapshot)
    await service.reject_mismatch(primary.attempt.attempt_id)
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        payload = dict(source.application_payload)
        rejection = dict(payload["rejection"])
        if corruption == "observation":
            rejection["observation"] = payload["review_sources"]["candidate"]
        elif corruption == "epoch":
            rejection["reopened_epoch"] += 1
        elif corruption == "debit":
            await work.session.delete(
                await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id)
            )
        else:
            payload.pop("rejection")
        if corruption != "receipt":
            payload["rejection"] = rejection
        source.application_payload = payload
        source.application_digest = canonical_digest(payload)
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await service.reject_mismatch(primary.attempt.attempt_id)
