"""Stopped scope requests release workers and wake their primary durably."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.subscription import ScopeRequestDecision, TaskBudget, WaitDecision
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import SubscriptionClientLaunch, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from sqlalchemy import select
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_usage import _known, _reservation


async def scope_case(
    session_factory, tmp_path, *, child_budget=None, requested_paths=("apps/shared",)
):
    factory, parent, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        None if child_budget is None else lambda child, _: (replace(child, budget=child_budget),),
        primary_budget=TaskBudget(max_provider_attempts=8),
        plan_scope=("apps",),
    )
    application = SubscriptionDecisionApplication(factory)
    await application.apply_delegation(parent.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("worker", _reservation())
    assert child is not None and child.task.task_id == children[0].task_id
    decision = ScopeRequestDecision(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        requested_paths=requested_paths,
        reason="Shared interface change needed",
    )
    proof = await record_stopped_launch(session_factory, child)
    assert (
        await executor.settle(
            child,
            SubscriptionInvocationResult(
                attempt=child.attempt, decision=decision, telemetry=_known(), launch_proof=proof
            ),
        )
    ).disposition == "decision_pending"
    return factory, application, parent, child


@pytest.mark.integration
async def test_scope_request_releases_child_and_wakes_primary_once(session_factory, tmp_path):
    factory, application, parent, child = await scope_case(session_factory, tmp_path)
    result = await application.apply_scope_request(child.attempt.attempt_id)
    assert result.accepted and result.disposition == "scope_requested" and not result.replayed
    assert (await application.apply_scope_request(child.attempt.attempt_id)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        assert task.state == scheduled.state == "blocked"
        assert scheduled.lease_owner is None
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"
    resumed = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert resumed is not None and resumed.task.task_id == parent.task.task_id
    request = await SubscriptionRequestBuilder(factory).build(resumed)
    outcome = next(
        value
        for value in request.untrusted_context["task_outcomes"]
        if value["task_id"] == str(child.task.task_id)
    )
    assert outcome["scope_request_attempt_id"] == str(child.attempt.attempt_id)
    assert outcome["scope_request"] is not None


@pytest.mark.integration
async def test_wait_cannot_sleep_through_existing_scope_request(session_factory, tmp_path):
    factory, application, _, child = await scope_case(session_factory, tmp_path)
    await application.apply_scope_request(child.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    parent = await executor.admit_next("primary", _reservation())
    proof = await record_stopped_launch(session_factory, parent)
    result = SubscriptionInvocationResult(
        attempt=parent.attempt,
        decision=WaitDecision(
            run_id=parent.task.run_id,
            task_id=parent.task.task_id,
            waiting_on_task_ids=(child.task.task_id,),
            reason="Waiting for child",
        ),
        telemetry=_known(),
        launch_proof=proof,
    )
    await executor.settle(parent, result)
    assert (await application.apply_wait(parent.attempt.attempt_id)).accepted
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"


@pytest.mark.integration
@pytest.mark.parametrize("change", ["cancel", "source", "launch", "effect"])
async def test_scope_request_defers_without_current_stopped_source(
    session_factory, tmp_path, change
):
    factory, application, parent, child = await scope_case(session_factory, tmp_path)
    async with factory() as work:
        if change == "cancel":
            (await work.session.get(SubscriptionTask, child.task.task_id)).cancel_requested = True
        elif change == "source":
            (
                await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            ).result_digest = "f" * 64
        elif change == "launch":
            await work.session.delete(
                await work.session.scalar(
                    select(SubscriptionClientLaunch).where(
                        SubscriptionClientLaunch.attempt_id == child.attempt.attempt_id
                    )
                )
            )
        else:
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=child.task.run_id,
                    task_id=child.task.task_id,
                    lease_owner=child.lease.owner,
                    lease_generation=child.lease.generation,
                    candidate_epoch=child.candidate_epoch,
                )
            )
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_scope_request(child.attempt.attempt_id)
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
        assert (
            await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        ).disposition == "decision_pending"


@pytest.mark.integration
async def test_scope_request_rollback_and_concurrent_replay(session_factory, tmp_path):
    factory, application, parent, child = await scope_case(session_factory, tmp_path)
    async with factory() as work:
        await work.subscription_decisions.apply_scope_request(child.attempt.attempt_id)
        await work.rollback()
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            application.apply_scope_request(child.attempt.attempt_id),
            application.apply_scope_request(child.attempt.attempt_id),
        ),
        10,
    )
    assert sorted(result.replayed for result in outcomes) == [False, True]


@pytest.mark.integration
async def test_periodic_recovery_applies_scope_request(session_factory, tmp_path):
    factory, _, _, _ = await scope_case(session_factory, tmp_path)
    recovery = SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "scope-artifacts")
    )
    report = await recovery.reconcile_all()
    assert report.applied == 1 and report.deferred == report.unsupported == 0
    assert (await recovery.reconcile_all()).applied == 0


@pytest.mark.integration
async def test_scope_request_arriving_after_primary_wait_wakes_it(session_factory, tmp_path):
    factory, original, _, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, _: (child, replace(child, task_id=uuid4(), owned_paths=("apps/other",))),
        primary_budget=TaskBudget(max_provider_attempts=8),
        plan_scope=("apps",),
    )
    application = SubscriptionDecisionApplication(factory)
    await application.apply_delegation(original.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("first", _reservation())
    second = await executor.admit_next("second", _reservation())
    assert first is not None and second is not None
    await executor.settle(
        first,
        SubscriptionInvocationResult(
            attempt=first.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        ),
    )
    parent = await executor.admit_next("parent", _reservation())
    assert parent is not None
    proof = await record_stopped_launch(session_factory, parent)
    await executor.settle(
        parent,
        SubscriptionInvocationResult(
            attempt=parent.attempt,
            decision=WaitDecision(
                run_id=parent.task.run_id,
                task_id=parent.task.task_id,
                waiting_on_task_ids=(second.task.task_id,),
                reason="Wait for remaining child",
            ),
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    await application.apply_wait(parent.attempt.attempt_id)
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
    proof = await record_stopped_launch(session_factory, second)
    await executor.settle(
        second,
        SubscriptionInvocationResult(
            attempt=second.attempt,
            decision=ScopeRequestDecision(
                run_id=second.task.run_id,
                task_id=second.task.task_id,
                requested_paths=("apps/shared",),
                reason="Need interface scope",
            ),
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    await application.apply_scope_request(second.attempt.attempt_id)
    resumed = await executor.admit_next("resumed", _reservation())
    assert resumed is not None and resumed.task.task_id == parent.task.task_id
