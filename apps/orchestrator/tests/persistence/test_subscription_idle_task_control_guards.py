"""Settled controls cannot replace history, budgets, or scheduling authority."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription_task_controls import TaskControlConflict
from forge.persistence.models.api import ApiMutation
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import SubscriptionClientLaunch, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_scope_response import response_case
from test_subscription_task_control_recovery import active_case, control, finish_client
from test_subscription_usage import _known, _reservation


async def repaired_case(session_factory, tmp_path):
    factory, parent, child, executor, proof = await active_case(session_factory, tmp_path)
    await finish_client(factory, child, proof)
    assert (
        await executor.settle(
            child,
            SubscriptionInvocationResult(
                attempt=child.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "repair_queued"
    return factory, parent, child


@pytest.mark.integration
@pytest.mark.parametrize("cancel", [False, True])
async def test_idle_repair_controls_preserve_existing_debit_and_partial_work(
    session_factory, tmp_path, cancel
):
    factory, parent, child = await repaired_case(session_factory, tmp_path)
    async with factory() as work:
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        retained = result.result_digest, result.result_payload, result.disposition
    for _ in range(2):
        pause = await control(factory, child, "pause")
        assert (
            await control(factory, child, "resume", pause_id=pause.receipt_id)
        ).status == "queued"
    if cancel:
        await control(factory, child, "pause")
        assert (await control(factory, child, "cancel")).status == "cancelled"
    async with factory() as work:
        assert await work.subscription_budget.usage(child.task.run_id, child.task.task_id) == usage
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert (result.result_digest, result.result_payload, result.disposition) == retained
        assert (await work.session.get(SubscriptionScheduledTask, child.task.task_id)).repairs == 1
        if cancel:
            assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"
            outcomes = await work.subscription.invocation_outcomes(
                child.task.run_id, (child.task.task_id,)
            )
            assert next(
                item for item in outcomes if item.task_id == child.task.task_id
            ).cancel_requested


@pytest.mark.integration
@pytest.mark.parametrize(
    "change", ["result", "launch", "consumption", "candidate", "version", "audit", "effect"]
)
async def test_idle_resume_rejects_changed_history_or_authority(session_factory, tmp_path, change):
    factory, _, child = await repaired_case(session_factory, tmp_path)
    pause = await control(factory, child, "pause")
    async with factory() as work:
        if change == "result":
            result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            result.result_payload = {**result.result_payload, "failure_detail": "changed"}
        elif change == "launch":
            await work.session.delete(
                await work.session.scalar(
                    select(SubscriptionClientLaunch).where(
                        SubscriptionClientLaunch.attempt_id == child.attempt.attempt_id,
                    )
                )
            )
        elif change == "consumption":
            consumption = await work.session.get(
                SubscriptionAttemptConsumption, child.attempt.attempt_id
            )
            consumption.charged = {**consumption.charged, "provider_attempts": 0}
        elif change == "candidate":
            (
                await work.session.get(SubscriptionSchedulerRun, child.task.run_id)
            ).candidate_epoch += 1
        elif change == "version":
            (await work.session.get(SubscriptionTask, child.task.task_id)).version += 1
        elif change == "audit":
            mutation = await work.session.get(ApiMutation, pause.receipt_id)
            payload = deepcopy(mutation.response_payload)
            payload["proof"]["idle_state"] = "blocked"
            mutation.response_payload = payload
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
    with pytest.raises(TaskControlConflict):
        await control(factory, child, "resume", pause_id=pause.receipt_id)
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("guarded-resume", _reservation())
        is None
    )


@pytest.mark.integration
async def test_idle_pause_races_admission_without_a_second_attempt(session_factory, tmp_path):
    factory, _, child = await repaired_case(session_factory, tmp_path)
    pause, admitted = await asyncio.gather(
        control(factory, child, "pause"),
        SubscriptionDecisionExecutor(factory).admit_next("concurrent-admission", _reservation()),
        return_exceptions=True,
    )
    assert not isinstance(admitted, BaseException)
    if admitted is None:
        assert not isinstance(pause, BaseException) and pause.status == "paused"
    else:
        assert admitted.task.task_id == child.task.task_id and admitted.attempt.attempt_number == 2
        assert isinstance(pause, TaskControlConflict) or pause.status == "pause_requested"
    async with factory() as work:
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        assert usage.consumed.repairs == 1
        assert await work.scheduler._active_count(run_id=child.task.run_id) == int(
            admitted is not None
        )


@pytest.mark.integration
@pytest.mark.parametrize("applied", [False, True])
async def test_scope_answer_rejects_tampered_idle_resume_evidence(
    session_factory, tmp_path, applied
):
    factory, application, primary, child = await response_case(session_factory, tmp_path)
    pause = await control(factory, child, "pause")
    resume = await control(factory, child, "resume", pause_id=pause.receipt_id)
    if applied:
        assert (await application.apply_scope_response(primary.attempt.attempt_id)).accepted
    async with factory() as work:
        mutation = await work.session.get(ApiMutation, resume.receipt_id)
        payload = deepcopy(mutation.response_payload)
        payload["proof"]["history_digest"] = "a" * 64
        mutation.response_payload = payload
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_scope_response(primary.attempt.attempt_id)
