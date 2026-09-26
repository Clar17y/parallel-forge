"""Review selection prepares a stable candidate without claiming approval."""

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import ReviewSelection, SpecialistPurpose
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
    _route,
)
from test_subscription_preparation import preparation_case
from test_subscription_usage import _known, _reservation


async def selection_case(
    session_factory,
    tmp_path,
    *,
    primary_budget=None,
    tree_digest="a" * 64,
    candidate_commit=None,
    review_required=False,
    selection_mutate=None,
    plan_scope=None,
):
    factory, _, command, prepare, _ = await preparation_case(
        session_factory,
        tmp_path,
        primary_budget=primary_budget,
        review_route=_route("reviewer") if review_required else None,
        plan_scope=plan_scope,
    )
    async with factory() as work:
        await prepare.execute(command, work)
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("primary-selects-review", _reservation())
    decision = ReviewSelection(
        run_id=admission.task.run_id,
        candidate_commit=candidate_commit,
        candidate_tree_digest=tree_digest,
        review_required=review_required,
        no_review_reason=None if review_required else "Trivial repair with focused checks",
        reviewer_route=admission.envelope.route_for(SpecialistPurpose.INDEPENDENT_REVIEW).effective
        if review_required
        else None,
    )
    if selection_mutate is not None:
        decision = selection_mutate(decision, admission)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        decision=decision,
        telemetry=_known(),
        launch_proof=await record_stopped_launch(session_factory, admission),
    )
    assert (await executor.settle(admission, result)).disposition == "decision_pending"
    return factory, admission, result


@pytest.mark.integration
async def test_selection_freezes_candidate_and_releases_primary_once(session_factory, tmp_path):
    factory, admission, original = await selection_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    outcome = await service.prepare_review_selection(admission.attempt.attempt_id)
    assert outcome.accepted and outcome.disposition == "candidate_prepared"
    assert (await service.prepare_review_selection(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, admission.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert task.state == scheduled.state == "blocked"
        assert scheduled.lease_owner is None and scheduled.lease_expires_at is None
        assert scheduler.candidate_state == "closed"
        assert scheduler.candidate_epoch == admission.candidate_epoch + 1
        assert result.application_payload["kind"] == "candidate_intent"
        assert result.application_payload["candidate_epoch"] == scheduler.candidate_epoch
        assert "verified_tree_digest" not in result.application_payload
    assert (await SubscriptionDecisionExecutor(factory).settle(admission, original)).replayed


@pytest.mark.integration
@pytest.mark.parametrize("change", ["receipt", "digest", "source", "usage", "pause", "unfinished"])
async def test_candidate_intent_rejects_stale_or_corrupt_source(session_factory, tmp_path, change):
    from dataclasses import replace
    from uuid import uuid4

    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.domain.subscription import SpecialistPurpose
    from forge.persistence.models.subscription import SubscriptionAttempt
    from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption

    factory, admission, _ = await selection_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    if change in {"receipt", "digest"}:
        await service.prepare_review_selection(admission.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        if change == "receipt":
            result.application_payload = {
                **result.application_payload,
                "proposed_tree_digest": "b" * 64,
            }
        elif change == "digest":
            result.application_digest = "b" * 64
        elif change == "source":
            result.result_digest = "b" * 64
        elif change == "usage":
            row = await work.session.get(
                SubscriptionAttemptConsumption, admission.attempt.attempt_id
            )
            await work.session.delete(row)
        elif change == "pause":
            (
                await work.session.get(SubscriptionTask, admission.task.task_id)
            ).pause_requested = True
        else:
            child = replace(
                admission.task,
                task_id=uuid4(),
                parent_task_id=admission.task.task_id,
                purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                route=admission.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
            )
            await work.subscription.create_task(child, idempotency_key=str(child.task_id))
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await service.prepare_review_selection(admission.attempt.attempt_id)
    if change not in {"receipt", "digest"}:
        async with factory() as work:
            scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
            attempt = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
            assert scheduler.candidate_state == "open"
            assert scheduler.candidate_epoch == admission.candidate_epoch
            assert attempt.status == "reconciling"


@pytest.mark.integration
async def test_candidate_intent_rollback_and_concurrent_replay(session_factory, tmp_path):
    import asyncio

    factory, admission, _ = await selection_case(session_factory, tmp_path)
    async with factory() as work:
        await work.subscription_decisions.prepare_review_selection(admission.attempt.attempt_id)
        await work.rollback()
    async with factory() as work:
        row = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        assert row.candidate_state == "open" and row.candidate_epoch == admission.candidate_epoch
    service = SubscriptionDecisionApplication(factory)
    outcomes = await asyncio.gather(
        *(service.prepare_review_selection(admission.attempt.attempt_id) for _ in range(2))
    )
    assert sum(outcome.replayed for outcome in outcomes) == 1
    assert all(outcome.accepted for outcome in outcomes)
    async with factory() as work:
        row = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        assert row.candidate_epoch == admission.candidate_epoch + 1
