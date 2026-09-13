"""Candidate application preserves control, rollback and concurrent replay boundaries."""

import asyncio
from dataclasses import replace

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_candidate import SubscriptionCandidateApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import TaskBudget
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.subscription_decisions import (
    PostgresSubscriptionDecisionRepository,
)
from sqlalchemy import func, select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401
    _route,
)
from test_subscription_review_selection import selection_case
from test_subscription_usage import _reservation


def snapshot():
    return GitWorkingTreeSnapshot(head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=())


@pytest.mark.integration
@pytest.mark.parametrize("mismatch", ["tree", "commit", "exhausted"])
async def test_candidate_application_routes_observed_mismatch_to_bounded_repair(
    session_factory, tmp_path, mismatch
):
    observed = snapshot()
    factory, primary, _ = await selection_case(
        session_factory, tmp_path,
        tree_digest=observed.candidate_tree_digest if mismatch == "commit" else "a" * 64,
        candidate_commit="c" * 40 if mismatch == "commit" else None,
        primary_budget=TaskBudget(max_repairs=0) if mismatch == "exhausted" else None,
    )

    async def capture(proposal):
        return observed

    service = SubscriptionCandidateApplication(factory, capture)
    result = await service.apply(primary.attempt.attempt_id)
    assert not result.accepted
    assert result.disposition == (
        "candidate_rejected" if mismatch == "exhausted" else "candidate_repair_queued"
    )
    assert (await service.apply(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        task = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        assert scheduler.candidate_state == "open"
        assert scheduler.candidate_epoch == primary.candidate_epoch + 2
        assert task.repairs == (0 if mismatch == "exhausted" else 1)
        assert task.state == ("terminal" if mismatch == "exhausted" else "queued")


@pytest.mark.integration
@pytest.mark.parametrize("matching", [True, False])
async def test_observation_and_candidate_application_rollback_together(
    session_factory, tmp_path, monkeypatch, matching
):
    observed = snapshot()
    factory, primary, _ = await selection_case(
        session_factory, tmp_path,
        tree_digest=observed.candidate_tree_digest if matching else "a" * 64,
        review_required=matching,
    )
    captures = []

    async def capture(proposal):
        captures.append(proposal)
        return observed

    method = "finalize_review_selection" if matching else "reject_candidate_mismatch"
    original = getattr(PostgresSubscriptionDecisionRepository, method)

    async def fail_after_application(self, attempt_id):
        await original(self, attempt_id)
        raise RuntimeError("interrupted before commit")

    service = SubscriptionCandidateApplication(factory, capture)
    with monkeypatch.context() as patch:
        patch.setattr(PostgresSubscriptionDecisionRepository, method, fail_after_application)
        with pytest.raises(RuntimeError, match="interrupted before commit"):
            await service.apply(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        task = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        assert result.disposition == "candidate_prepared"
        assert "observation" not in result.application_payload
        assert scheduler.candidate_state == "closed"
        assert scheduler.candidate_epoch == primary.candidate_epoch + 1
        assert task.state == "blocked" and task.repairs == 0
    result = await service.apply(primary.attempt.attempt_id)
    assert result.disposition == ("review_selected" if matching else "candidate_repair_queued")
    assert len(captures) == 2


@pytest.mark.integration
@pytest.mark.parametrize("matching", [True, False])
async def test_concurrent_candidate_applications_settle_once(
    session_factory, tmp_path, matching
):
    observed = snapshot()
    factory, primary, _ = await selection_case(
        session_factory, tmp_path,
        tree_digest=observed.candidate_tree_digest if matching else "a" * 64,
        review_required=matching,
    )
    captures, both_reading = [], asyncio.Event()

    async def capture(proposal):
        captures.append(proposal)
        if len(captures) == 2:
            both_reading.set()
        await both_reading.wait()
        return observed

    service = SubscriptionCandidateApplication(factory, capture)
    outcomes = await asyncio.wait_for(asyncio.gather(
        service.apply(primary.attempt.attempt_id), service.apply(primary.attempt.attempt_id)
    ), 20)
    assert sum(result.replayed for result in outcomes) == 1
    assert all(result.disposition == (
        "review_selected" if matching else "candidate_repair_queued"
    ) for result in outcomes)
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        task = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        assert scheduler.candidate_epoch == primary.candidate_epoch + (1 if matching else 2)
        assert task.repairs == (0 if matching else 1)


@pytest.mark.integration
@pytest.mark.parametrize("change", ["pause", "epoch"])
async def test_candidate_application_rechecks_authority_after_git_io(
    session_factory, tmp_path, change
):
    observed = snapshot()
    factory, primary, _ = await selection_case(
        session_factory, tmp_path, tree_digest=observed.candidate_tree_digest
    )

    async def capture(proposal):
        async with factory() as work:
            run = await work.runs.get_for_update(primary.task.run_id)
            if change == "pause":
                await work.runs.pause(run.id, run.version, "run.paused", {})
            else:
                scheduler = await work.session.get(SubscriptionSchedulerRun, run.id)
                scheduler.candidate_epoch += 1
            await work.commit()
        return observed

    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionCandidateApplication(factory, capture).apply(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.disposition == "candidate_prepared"
        assert "observation" not in result.application_payload


@pytest.mark.integration
async def test_cancelled_candidate_observation_can_resume_without_losing_preparation(
    session_factory, tmp_path
):
    observed = snapshot()
    factory, primary, _ = await selection_case(
        session_factory, tmp_path, tree_digest=observed.candidate_tree_digest
    )
    reading = asyncio.Event()

    async def capture(proposal):
        reading.set()
        await asyncio.Event().wait()

    operation = asyncio.create_task(
        SubscriptionCandidateApplication(factory, capture).apply(primary.attempt.attempt_id)
    )
    await asyncio.wait_for(reading.wait(), 5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.disposition == "candidate_prepared"
        assert "observation" not in result.application_payload

    async def resumed(proposal):
        return observed

    result = await SubscriptionCandidateApplication(factory, resumed).apply(primary.attempt.attempt_id)
    assert result.disposition == "review_selected"


@pytest.mark.integration
@pytest.mark.parametrize("route", ["unapproved", "missing"])
@pytest.mark.parametrize("repairs", [0, 1])
async def test_unapproved_review_route_is_rejected_before_freezing_candidate(
    session_factory, tmp_path, route, repairs
):
    observed = snapshot()
    factory, primary, _ = await selection_case(
        session_factory, tmp_path, review_required=route == "unapproved",
        tree_digest=observed.candidate_tree_digest, primary_budget=TaskBudget(max_repairs=repairs),
        selection_mutate=lambda decision, admission: replace(
            decision, review_required=True, no_review_reason=None, reviewer_route=_route("unapproved")
        ),
    )

    async def capture(proposal):
        raise AssertionError("an unapproved review route must not inspect Git")

    service = SubscriptionCandidateApplication(factory, capture)
    outcomes = await asyncio.gather(
        service.apply(primary.attempt.attempt_id), service.apply(primary.attempt.attempt_id)
    )
    expected = "decision_repair_queued" if repairs else "decision_rejected"
    assert all(not outcome.accepted and outcome.disposition == expected for outcome in outcomes)
    assert sum(outcome.replayed for outcome in outcomes) == 1
    assert (await service.apply(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        retained = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        task = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        assert retained.disposition == expected
        assert retained.application_payload is None
        assert scheduler.candidate_state == "open"
        assert scheduler.candidate_epoch == primary.candidate_epoch
        assert task.state == ("queued" if repairs else "terminal") and task.repairs == repairs
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionTask).where(
            SubscriptionTask.parent_task_id == primary.task.task_id
        )) == 0
    if repairs:
        next_attempt = await SubscriptionDecisionExecutor(factory).admit_next(
            "correct-review-selection", _reservation()
        )
        assert next_attempt is not None and next_attempt.task.task_id == primary.task.task_id
        assert (await service.apply(primary.attempt.attempt_id)).replayed
