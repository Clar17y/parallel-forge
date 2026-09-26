"""Matching candidates select review without granting approval or reopening writes."""

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_candidate import SubscriptionCandidateInspection
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import SpecialistPurpose
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_review_selection import selection_case
from test_subscription_usage import _reservation


@pytest.mark.integration
@pytest.mark.parametrize("review_required", [False, True])
async def test_matching_candidate_selects_review_and_queues_the_correct_role_once(
    session_factory, tmp_path, review_required
):
    snapshot = GitWorkingTreeSnapshot(
        head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=()
    )
    factory, admission, _ = await selection_case(
        session_factory,
        tmp_path,
        tree_digest=snapshot.candidate_tree_digest,
        candidate_commit=snapshot.head_sha,
        review_required=review_required,
    )

    async def inspect(proposal):
        return snapshot

    await SubscriptionCandidateInspection(factory, inspect).inspect(admission.attempt.attempt_id)
    service = SubscriptionDecisionApplication(factory)
    outcome = await service.finalize_review_selection(admission.attempt.attempt_id)
    assert outcome.accepted and outcome.disposition == "review_selected"
    assert (await service.finalize_review_selection(admission.attempt.attempt_id)).replayed
    next_attempt = await SubscriptionDecisionExecutor(factory).admit_next(
        "selected-role", _reservation()
    )
    assert next_attempt is not None
    expected = (
        SpecialistPurpose.INDEPENDENT_REVIEW if review_required else SpecialistPurpose.PRIMARY
    )
    assert next_attempt.task.purpose is expected
    from forge.application.services.subscription_requests import SubscriptionRequestBuilder

    request = await SubscriptionRequestBuilder(factory).build(next_attempt)
    assert (
        request.untrusted_context["review_selection"]["observation"]["tree_digest"]
        == snapshot.candidate_tree_digest
    )
    if review_required:
        assert next_attempt.task.parent_task_id == admission.task.task_id
        assert next_attempt.task.owned_paths == () and next_attempt.task.named_checks == ()
    assert (await service.finalize_review_selection(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        source = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert scheduler.candidate_state == "closed"
        assert scheduler.candidate_epoch == admission.candidate_epoch + 1
        assert source.application_payload["selection"]["review_required"] is review_required
        assert "reviewer_approval" not in source.application_payload


async def matching_case(
    session_factory,
    tmp_path,
    *,
    review_required=True,
    selection_mutate=None,
    primary_budget=None,
    plan_scope=None,
):
    observed = GitWorkingTreeSnapshot(
        head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=()
    )
    factory, admission, _ = await selection_case(
        session_factory,
        tmp_path,
        tree_digest=observed.candidate_tree_digest,
        candidate_commit=observed.head_sha,
        review_required=review_required,
        selection_mutate=selection_mutate,
        primary_budget=primary_budget,
        plan_scope=plan_scope,
    )

    async def snapshot(proposal):
        return observed

    await SubscriptionCandidateInspection(factory, snapshot).inspect(admission.attempt.attempt_id)
    return factory, admission


@pytest.mark.integration
@pytest.mark.parametrize("review_required", [False, True])
async def test_selection_rollback_and_concurrent_replay(session_factory, tmp_path, review_required):
    import asyncio

    from forge.persistence.models.subscription import SubscriptionTask
    from sqlalchemy import func, select

    factory, admission = await matching_case(
        session_factory, tmp_path, review_required=review_required
    )
    async with factory() as work:
        await work.subscription_decisions.finalize_review_selection(admission.attempt.attempt_id)
        await work.rollback()
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, admission.task.task_id)
        assert task.state == "blocked"
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(SubscriptionTask)
                .where(SubscriptionTask.parent_task_id == task.id)
            )
            == 0
        )
    service = SubscriptionDecisionApplication(factory)
    results = await asyncio.gather(
        *(service.finalize_review_selection(admission.attempt.attempt_id) for _ in range(2))
    )
    assert all(result.accepted for result in results)
    assert sum(result.replayed for result in results) == 1
    async with factory() as work:
        assert await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionTask)
            .where(SubscriptionTask.parent_task_id == admission.task.task_id)
        ) == int(review_required)


@pytest.mark.integration
@pytest.mark.parametrize("change", ["pause", "epoch", "receipt", "digest", "missing_child"])
async def test_selection_current_source_and_replay_guards(session_factory, tmp_path, change):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.domain.operation import canonical_digest
    from forge.domain.subscription import decode_subscription_record
    from forge.persistence.models.subscription import SubscriptionTask

    factory, admission = await matching_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    if change in {"receipt", "digest", "missing_child"}:
        await service.finalize_review_selection(admission.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        if change == "pause":
            (
                await work.session.get(SubscriptionTask, admission.task.task_id)
            ).pause_requested = True
        elif change == "epoch":
            (
                await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
            ).candidate_epoch += 1
        elif change == "missing_child":
            child = decode_subscription_record(
                result.application_payload["selection"]["review_task"]
            )
            await work.session.delete(await work.session.get(SubscriptionTask, child.task_id))
        elif change == "receipt":
            result.application_payload = {
                **result.application_payload,
                "selection": {"review_required": False, "review_task": None},
            }
            result.application_digest = canonical_digest(result.application_payload)
        else:
            result.application_digest = "b" * 64
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await service.finalize_review_selection(admission.attempt.attempt_id)


@pytest.mark.integration
async def test_unapproved_review_route_cannot_create_task(session_factory, tmp_path):
    from dataclasses import replace

    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.persistence.models.subscription import SubscriptionTask
    from sqlalchemy import func, select

    def mutate(selection, admission):
        return replace(
            selection, reviewer_route=replace(selection.reviewer_route, model="unapproved")
        )

    factory, admission, _ = await selection_case(
        session_factory, tmp_path, review_required=True, selection_mutate=mutate
    )
    application = SubscriptionDecisionApplication(factory)
    result = await application.prepare_review_selection(admission.attempt.attempt_id)
    assert not result.accepted and result.disposition == "decision_repair_queued"
    with pytest.raises(SubscriptionDecisionError, match="already rejected"):
        await application.finalize_review_selection(admission.attempt.attempt_id)
    async with factory() as work:
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(SubscriptionTask)
                .where(SubscriptionTask.parent_task_id == admission.task.task_id)
            )
            == 0
        )
        task = await work.session.get(SubscriptionTask, admission.task.task_id)
        assert task.state == "queued"


@pytest.mark.integration
@pytest.mark.parametrize("state", ["open", "closed"])
async def test_superseded_selection_does_not_supply_current_context(
    session_factory, tmp_path, state
):
    factory, admission = await matching_case(session_factory, tmp_path, review_required=False)
    await SubscriptionDecisionApplication(factory).finalize_review_selection(
        admission.attempt.attempt_id
    )
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        scheduler.candidate_state = state
        scheduler.candidate_epoch += 1
        source = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        source.application_digest = "b" * 64
        await work.commit()
    async with factory() as work:
        assert (
            await work.subscription_decisions.review_selection_context(
                admission.task.run_id, admission.task.task_id
            )
            is None
        )
