"""Primary-directed owner steering: submit, deliver exactly once, close (issue #137)."""

from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_feedback import SubscriptionTaskFeedbackService
from forge.application.services.subscription_planning import SubscriptionPlanningService
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.domain.subscription import TaskBudget
from forge.domain.subscription_feedback import (
    MAX_PRIMARY_FEEDBACK_PENDING,
    SubscriptionPrimaryFeedbackRequest,
    SubscriptionTaskFeedbackRequest,
    TaskFeedbackConflict,
)
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_primary_feedback import SubscriptionPrimaryFeedback
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_feedback import _delegated_case
from test_subscription_planning_start import planning_start_case
from test_subscription_usage import _known, _reservation


def _actor():
    return AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())


async def _primary_case(session_factory, tmp_path):
    factory, run, command = await planning_start_case(session_factory, tmp_path)
    async with factory() as work:
        await SubscriptionPlanningService(TaskBudget(max_provider_attempts=64)).execute(command, work)
        await work.commit()
    async with factory() as work:
        run_now = await work.runs.get(run.id)
        rows = await work.session.scalars(
            select(SubscriptionTask).where(SubscriptionTask.run_id == run.id)
        )
        primary = rows.first()
        assert primary is not None
        return factory, run_now, primary.id, primary.version


@pytest.mark.integration
async def test_submit_to_primary_and_deliver_exactly_once(session_factory, tmp_path):
    factory, run, primary_id, task_version = await _primary_case(session_factory, tmp_path)
    service = SubscriptionTaskFeedbackService(factory)
    actor = _actor()
    receipt = await service.submit_primary(
        run_id=run.id, task_id=primary_id, actor=actor, idempotency_key="primary-1",
        request=SubscriptionPrimaryFeedbackRequest(
            expected_run_version=run.version, expected_task_version=task_version,
            feedback="The root cause is the parser; stop blind repairs.",
        ),
    )
    assert receipt.status == "pending" and receipt.task_id == primary_id
    # Replay same idempotency key returns same receipt
    replay = await service.submit_primary(
        run_id=run.id, task_id=primary_id, actor=actor, idempotency_key="primary-1",
        request=SubscriptionPrimaryFeedbackRequest(
            expected_run_version=run.version, expected_task_version=task_version,
            feedback="The root cause is the parser; stop blind repairs.",
        ),
    )
    assert replay.receipt_id == receipt.receipt_id
    # Second distinct message allowed (multiple pending)
    receipt2 = await service.submit_primary(
        run_id=run.id, task_id=primary_id, actor=actor, idempotency_key="primary-2",
        request=SubscriptionPrimaryFeedbackRequest(
            expected_run_version=run.version, expected_task_version=task_version,
            feedback="Also add a regression test for that parser case.",
        ),
    )
    assert receipt2.receipt_id != receipt.receipt_id

    admission = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert admission is not None and admission.task.task_id == primary_id
    request = await SubscriptionRequestBuilder(factory).build(admission)
    assert request.prompt_version == "forge-subscription-v24"
    delivered = request.untrusted_context["operator_primary_feedback"]
    assert [item["feedback"] for item in delivered] == [
        "The root cause is the parser; stop blind repairs.",
        "Also add a regression test for that parser case.",
    ]
    assert "operator_primary_feedback" in request.trusted_system_prompt or "Owner steering" in request.trusted_system_prompt
    # Building binds but does not deliver: rows settle only on launch proof.
    async with factory() as work:
        rows = list(await work.session.scalars(
            select(SubscriptionPrimaryFeedback).where(SubscriptionPrimaryFeedback.run_id == run.id)
        ))
        assert {row.state for row in rows} == {"pending"}
        assert {row.delivery_attempt_id for row in rows} == {admission.attempt.attempt_id}
        assert {row.delivered_at for row in rows} == {None}
    # Rebuild same attempt returns same bound set
    rebuilt = await SubscriptionRequestBuilder(factory).build(admission)
    assert rebuilt.untrusted_context["operator_primary_feedback"] == delivered
    async with factory() as work:
        await work.subscription_feedback.settle_delivery(admission.attempt.attempt_id)
        await work.commit()
    async with factory() as work:
        rows = list(await work.session.scalars(
            select(SubscriptionPrimaryFeedback).where(SubscriptionPrimaryFeedback.run_id == run.id)
        ))
        assert {row.state for row in rows} == {"delivered"}
        assert {row.delivery_attempt_id for row in rows} == {admission.attempt.attempt_id}
        assert all(row.delivered_at is not None for row in rows)
    # Post-settle rebuild of the same attempt still returns the settled set
    rebuilt_after_settle = await SubscriptionRequestBuilder(factory).build(admission)
    assert rebuilt_after_settle.untrusted_context["operator_primary_feedback"] == delivered


@pytest.mark.integration
async def test_unlaunched_attempt_rebinds_steering_to_next_attempt(session_factory, tmp_path):
    factory, run, primary_id, task_version = await _primary_case(session_factory, tmp_path)
    await SubscriptionTaskFeedbackService(factory).submit_primary(
        run_id=run.id, task_id=primary_id, actor=_actor(), idempotency_key="rebind-1",
        request=SubscriptionPrimaryFeedbackRequest(
            expected_run_version=run.version, expected_task_version=task_version,
            feedback="Do not lose this if the first attempt never launches.",
        ),
    )
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("primary", _reservation())
    assert first is not None and first.task.task_id == primary_id
    first_request = await SubscriptionRequestBuilder(factory).build(first)
    assert len(first_request.untrusted_context["operator_primary_feedback"]) == 1
    # The attempt fails with no launch proof: steering must survive.
    settled = await executor.settle(
        first,
        SubscriptionInvocationResult(
            attempt=first.attempt,
            failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
            launch_proof=None,
        ),
    )
    assert settled.disposition == "repair_queued"
    async with factory() as work:
        rows = list(await work.session.scalars(
            select(SubscriptionPrimaryFeedback).where(SubscriptionPrimaryFeedback.run_id == run.id)
        ))
        assert rows[0].state == "pending"
        assert rows[0].delivery_attempt_id == first.attempt.attempt_id
    second = await executor.admit_next("primary", _reservation())
    assert second is not None and second.attempt.attempt_id != first.attempt.attempt_id
    second_request = await SubscriptionRequestBuilder(factory).build(second)
    rebound = second_request.untrusted_context["operator_primary_feedback"]
    assert [item["feedback"] for item in rebound] == [
        "Do not lose this if the first attempt never launches."
    ]
    async with factory() as work:
        rows = list(await work.session.scalars(
            select(SubscriptionPrimaryFeedback).where(SubscriptionPrimaryFeedback.run_id == run.id)
        ))
        assert rows[0].state == "pending"
        assert rows[0].delivery_attempt_id == second.attempt.attempt_id


@pytest.mark.integration
async def test_forwarding_only_turn_does_not_bind_primary_steering(session_factory, tmp_path):
    factory, delegated, child, run_version, child_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    service = SubscriptionTaskFeedbackService(factory)
    actor = _actor()
    await service.submit(
        run_id=child.run_id, task_id=child.task_id, actor=actor,
        idempotency_key="worker-feedback-1",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version, expected_task_version=child_version,
            feedback="Worker-directed feedback pending forwarding.",
        ),
    )
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        primary_row = await work.session.get(SubscriptionTask, delegated.task.task_id)
        assert primary_row is not None
        primary_version = primary_row.version
        run_version_now = run.version
    await service.submit_primary(
        run_id=child.run_id, task_id=delegated.task.task_id, actor=actor,
        idempotency_key="primary-steering-1",
        request=SubscriptionPrimaryFeedbackRequest(
            expected_run_version=run_version_now, expected_task_version=primary_version,
            feedback="Steering that must wait for an actionable turn.",
        ),
    )
    primary = await SubscriptionDecisionExecutor(factory).admit_next("feedback-primary", _reservation())
    assert primary is not None and primary.task.task_id == delegated.task.task_id
    request = await SubscriptionRequestBuilder(factory).build(primary)
    assert request.untrusted_context["pending_worker_feedback"] is not None
    assert not request.untrusted_context["operator_primary_feedback"]
    async with factory() as work:
        rows = list(await work.session.scalars(
            select(SubscriptionPrimaryFeedback).where(
                SubscriptionPrimaryFeedback.run_id == child.run_id
            )
        ))
        assert rows[0].state == "pending"
        assert rows[0].delivery_attempt_id is None


@pytest.mark.integration
async def test_primary_submit_rejects_stale_child_and_cancelled(session_factory, tmp_path):
    factory, run, primary_id, task_version = await _primary_case(session_factory, tmp_path)
    service = SubscriptionTaskFeedbackService(factory)
    actor = _actor()
    with pytest.raises(TaskFeedbackConflict):
        await service.submit_primary(
            run_id=run.id, task_id=primary_id, actor=actor, idempotency_key="stale",
            request=SubscriptionPrimaryFeedbackRequest(
                expected_run_version=run.version, expected_task_version=task_version + 9,
                feedback="Stale steering.",
            ),
        )
    with pytest.raises(TaskFeedbackConflict):
        await service.submit_primary(
            run_id=run.id, task_id=uuid4(), actor=actor, idempotency_key="foreign",
            request=SubscriptionPrimaryFeedbackRequest(
                expected_run_version=run.version, expected_task_version=task_version,
                feedback="Foreign target.",
            ),
        )
    # Pending bound
    for index in range(MAX_PRIMARY_FEEDBACK_PENDING):
        await service.submit_primary(
            run_id=run.id, task_id=primary_id, actor=actor, idempotency_key=f"bound-{index}",
            request=SubscriptionPrimaryFeedbackRequest(
                expected_run_version=run.version, expected_task_version=task_version,
                feedback=f"Steering {index}.",
            ),
        )
    with pytest.raises(TaskFeedbackConflict, match="bound"):
        await service.submit_primary(
            run_id=run.id, task_id=primary_id, actor=actor, idempotency_key="bound-overflow",
            request=SubscriptionPrimaryFeedbackRequest(
                expected_run_version=run.version, expected_task_version=task_version,
                feedback="One too many.",
            ),
        )


@pytest.mark.integration
async def test_blocked_primary_submit_requeues_and_replay_matches(session_factory, tmp_path):
    factory, run, primary_id, task_version = await _primary_case(session_factory, tmp_path)
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, primary_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, primary_id)
        assert task is not None and scheduled is not None
        task.state = scheduled.state = "blocked"
        await work.commit()
    service = SubscriptionTaskFeedbackService(factory)
    actor = _actor()
    body = SubscriptionPrimaryFeedbackRequest(
        expected_run_version=run.version, expected_task_version=task_version,
        feedback="Steering for a blocked primary.",
    )
    receipt = await service.submit_primary(
        run_id=run.id, task_id=primary_id, actor=actor,
        idempotency_key="blocked-1", request=body,
    )
    # Receipt keeps the observed pre-requeue version; replay returns it unchanged.
    assert receipt.task_version == task_version
    replay = await service.submit_primary(
        run_id=run.id, task_id=primary_id, actor=actor,
        idempotency_key="blocked-1", request=body,
    )
    assert replay == receipt
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, primary_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, primary_id)
        assert task.state == scheduled.state == "queued"
        assert task.version == task_version + 1


@pytest.mark.integration
async def test_pending_primary_feedback_requeues_terminal_primary(session_factory, tmp_path):
    factory, run, primary_id, task_version = await _primary_case(session_factory, tmp_path)
    service = SubscriptionTaskFeedbackService(factory)
    await service.submit_primary(
        run_id=run.id, task_id=primary_id, actor=_actor(), idempotency_key="wake-1",
        request=SubscriptionPrimaryFeedbackRequest(
            expected_run_version=run.version, expected_task_version=task_version,
            feedback="Steering submitted while the attempt was running.",
        ),
    )
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, primary_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, primary_id)
        assert task is not None and scheduled is not None
        task.state = scheduled.state = "terminal"
        await work.commit()
    async with factory() as work:
        assert await work.subscription_feedback.has_pending_primary_feedback(run.id, primary_id)
        assert await work.subscription_feedback.requeue_primary_feedback_pending(
            run.id, primary_id
        )
        await work.commit()
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, primary_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, primary_id)
        assert task.state == scheduled.state == "queued"
        rows = list(await work.session.scalars(
            select(SubscriptionPrimaryFeedback).where(SubscriptionPrimaryFeedback.run_id == run.id)
        ))
        assert rows[0].state == "pending"


@pytest.mark.integration
async def test_primary_feedback_closes_on_run_cancel(session_factory, tmp_path):
    factory, run, primary_id, task_version = await _primary_case(session_factory, tmp_path)
    service = SubscriptionTaskFeedbackService(factory)
    await service.submit_primary(
        run_id=run.id, task_id=primary_id, actor=_actor(), idempotency_key="cancel-1",
        request=SubscriptionPrimaryFeedbackRequest(
            expected_run_version=run.version, expected_task_version=task_version,
            feedback="Steering that will be cancelled.",
        ),
    )
    async with factory() as work:
        from forge.persistence.models import Run as RunRow
        row = await work.session.get(RunRow, run.id)
        row.state = "CANCELLED"
        await work.commit()
    async with factory() as work:
        closed = await work.subscription_feedback.close_run_cancelled(run.id)
        await work.commit()
        assert closed >= 1
        rows = list(await work.session.scalars(
            select(SubscriptionPrimaryFeedback).where(SubscriptionPrimaryFeedback.run_id == run.id)
        ))
        assert rows[0].state == "closed" and rows[0].closed_reason == "cancelled"
