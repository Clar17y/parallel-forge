"""Mismatched candidate proposals reopen only through bounded durable recovery."""

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_candidate import SubscriptionCandidateInspection
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_review_selection import selection_case


@pytest.mark.integration
async def test_mismatched_candidate_reopens_for_bounded_primary_repair_once(
    session_factory, tmp_path
):
    factory, admission, _ = await selection_case(session_factory, tmp_path)

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40,
            base_sha=proposal.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    await SubscriptionCandidateInspection(factory, snapshot).inspect(admission.attempt.attempt_id)
    service = SubscriptionDecisionApplication(factory)
    result = await service.reject_candidate_mismatch(admission.attempt.attempt_id)
    assert not result.accepted and result.disposition == "candidate_repair_queued"
    assert (await service.reject_candidate_mismatch(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, admission.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        source = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert task.state == scheduled.state == "queued"
        assert scheduled.repairs == 1
        assert scheduler.candidate_state == "open"
        assert scheduler.candidate_epoch == admission.candidate_epoch + 2
        assert source.application_payload["rejection"]["reason"] == "candidate_identity_differs"


async def observed_case(session_factory, tmp_path, *, primary_budget=None, matching=False):
    snapshot = GitWorkingTreeSnapshot(
        head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=()
    )
    factory, admission, _ = await selection_case(
        session_factory,
        tmp_path,
        primary_budget=primary_budget,
        tree_digest=snapshot.candidate_tree_digest if matching else "a" * 64,
    )

    async def inspect(proposal):
        assert proposal.worktree.base_sha == snapshot.base_sha
        return snapshot

    await SubscriptionCandidateInspection(factory, inspect).inspect(admission.attempt.attempt_id)
    return factory, admission


@pytest.mark.integration
async def test_matching_candidate_cannot_be_rejected(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError

    factory, admission = await observed_case(session_factory, tmp_path, matching=True)
    with pytest.raises(SubscriptionDecisionError, match="no observed identity mismatch"):
        await SubscriptionDecisionApplication(factory).reject_candidate_mismatch(
            admission.attempt.attempt_id
        )
    async with factory() as work:
        row = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        task = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert row.candidate_state == "closed" and task.repairs == 0


@pytest.mark.integration
async def test_candidate_rejection_exhaustion_is_terminal_without_repair_debit(
    session_factory, tmp_path
):
    from forge.domain.subscription import TaskBudget

    factory, admission = await observed_case(
        session_factory, tmp_path, primary_budget=TaskBudget(max_repairs=0)
    )
    service = SubscriptionDecisionApplication(factory)
    result = await service.reject_candidate_mismatch(admission.attempt.attempt_id)
    assert not result.accepted and result.disposition == "candidate_rejected"
    assert (await service.reject_candidate_mismatch(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        row = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert row.state == "terminal" and row.repairs == 0


@pytest.mark.integration
async def test_candidate_rejection_rollback_concurrent_replay_and_new_invocation(
    session_factory, tmp_path
):
    import asyncio

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from test_subscription_usage import _reservation

    factory, admission = await observed_case(session_factory, tmp_path)
    async with factory() as work:
        await work.subscription_decisions.reject_candidate_mismatch(admission.attempt.attempt_id)
        await work.rollback()
    async with factory() as work:
        row = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        assert row.candidate_state == "closed"
    service = SubscriptionDecisionApplication(factory)
    results = await asyncio.gather(
        *(service.reject_candidate_mismatch(admission.attempt.attempt_id) for _ in range(2))
    )
    assert sum(result.replayed for result in results) == 1
    current = await SubscriptionDecisionExecutor(factory).admit_next(
        "primary-repairs", _reservation()
    )
    assert current is not None and current.task.task_id == admission.task.task_id
    assert (await service.reject_candidate_mismatch(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        row = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert row.state == "leased" and row.repairs == 1


@pytest.mark.integration
@pytest.mark.parametrize("change", ["pause", "epoch", "receipt", "digest"])
async def test_candidate_rejection_source_and_replay_guards(session_factory, tmp_path, change):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.domain.operation import canonical_digest

    factory, admission = await observed_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    if change in {"receipt", "digest"}:
        await service.reject_candidate_mismatch(admission.attempt.attempt_id)
    async with factory() as work:
        if change == "pause":
            (
                await work.session.get(SubscriptionTask, admission.task.task_id)
            ).pause_requested = True
        elif change == "epoch":
            (
                await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
            ).candidate_epoch += 1
        else:
            result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
            if change == "receipt":
                result.application_payload = {
                    **result.application_payload,
                    "rejection": {"reason": "wrong"},
                }
                result.application_digest = canonical_digest(result.application_payload)
            else:
                result.application_digest = "b" * 64
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await service.reject_candidate_mismatch(admission.attempt.attempt_id)


@pytest.mark.integration
@pytest.mark.parametrize("commit", ["b" * 40, "c" * 40])
async def test_candidate_commit_is_checked_even_when_tree_matches(
    session_factory, tmp_path, commit
):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError

    observed = GitWorkingTreeSnapshot(
        head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=()
    )
    factory, admission, _ = await selection_case(
        session_factory,
        tmp_path,
        tree_digest=observed.candidate_tree_digest,
        candidate_commit=commit,
    )

    async def snapshot(proposal):
        return observed

    await SubscriptionCandidateInspection(factory, snapshot).inspect(admission.attempt.attempt_id)
    service = SubscriptionDecisionApplication(factory)
    if commit == observed.head_sha:
        with pytest.raises(SubscriptionDecisionError, match="no observed identity mismatch"):
            await service.reject_candidate_mismatch(admission.attempt.attempt_id)
    else:
        result = await service.reject_candidate_mismatch(admission.attempt.attempt_id)
        assert result.disposition == "candidate_repair_queued"


@pytest.mark.integration
async def test_candidate_without_observation_cannot_reopen(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError

    factory, admission, _ = await selection_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    await service.prepare_review_selection(admission.attempt.attempt_id)
    with pytest.raises(SubscriptionDecisionError, match="no observed identity mismatch"):
        await service.reject_candidate_mismatch(admission.attempt.attempt_id)
    async with factory() as work:
        row = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        task = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert row.candidate_state == "closed" and task.repairs == 0
