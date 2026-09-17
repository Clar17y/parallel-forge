"""Durable operator feedback crosses primary recovery into the same worker task."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import decode_final
from forge.api.schemas.subscription_usage import SubscriptionUsagePage
from forge.application.handlers.run_controls import CancelRunHandler
from forge.application.ports.commands import CommandLane
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_feedback import SubscriptionTaskFeedbackService
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.run import RunState
from forge.domain.subscription import (
    AttemptTelemetry,
    ForwardFeedbackDecision,
    HandoffStatus,
    ScopeRequestDecision,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
    WaitDecision,
)
from forge.domain.subscription_feedback import (
    StoredTaskFeedback,
    SubscriptionTaskFeedbackRequest,
    TaskFeedbackConflict,
)
from forge.domain.subscription_task_controls import SubscriptionTaskControlRequest
from forge.persistence.models import ApiMutation, OperatorAuditEvent
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_feedback import SubscriptionTaskFeedback
from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop
from forge.persistence.queries.subscription_usage import SubscriptionUsageQuery
from forge.persistence.repositories.mutations import MutationConflict
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_feedback import MAX_FEEDBACK_PER_TASK
from forge.worker.subscription_invocation import (
    SubscriptionInvocationSession,
    SubscriptionInvocationWorker,
)
from sqlalchemy import func, select
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_reassignment import reassignment_case
from test_subscription_task_acceptance import task_acceptance_case
from test_subscription_usage import _known, _reservation


def _actor() -> AuthenticatedActor:
    return AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())


async def _delegated_case(
    session_factory,
    tmp_path,
    *,
    child_attempts=3,
    primary_attempts=8,
    primary_duration_seconds=1800,
):
    factory, delegated, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, _parent: (
            replace(
                child,
                budget=replace(child.budget, max_provider_attempts=child_attempts),
            ),
        ),
        primary_budget=TaskBudget(
            max_duration_seconds=primary_duration_seconds,
            max_provider_attempts=primary_attempts,
            max_repairs=0,
        ),
    )
    applied = await SubscriptionDecisionApplication(factory).apply_delegation(
        delegated.attempt.attempt_id
    )
    assert applied.accepted
    child = children[0]
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        row = await work.session.get(SubscriptionTask, child.task_id)
        usage = await work.subscription_budget.usage(child.run_id, delegated.task.task_id)
        assert row is not None
        return factory, delegated, child, run.version, row.version, usage.consumed.provider_attempts


async def _delegated_pair_case(
    session_factory,
    tmp_path,
    *,
    primary_attempts=4,
    primary_repairs=0,
    primary_duration_seconds=1800,
    target_depends_on_sibling=False,
    child_repairs=0,
):
    def pair(child, _parent):
        budget = replace(
            child.budget,
            max_provider_attempts=3,
            max_repairs=child_repairs,
        )
        sibling_id = uuid4()
        return (
            replace(
                child,
                budget=budget,
                max_repairs=child_repairs,
                dependency_task_ids=(sibling_id,) if target_depends_on_sibling else (),
            ),
            replace(
                child,
                task_id=sibling_id,
                budget=budget,
                max_repairs=child_repairs,
                owned_paths=("apps/other",),
            ),
        )

    factory, delegated, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        pair,
        primary_budget=TaskBudget(
            max_duration_seconds=primary_duration_seconds,
            max_provider_attempts=primary_attempts,
            max_repairs=primary_repairs,
        ),
    )
    applied = await SubscriptionDecisionApplication(factory).apply_delegation(
        delegated.attempt.attempt_id
    )
    assert applied.accepted
    target, sibling = children
    async with factory() as work:
        run = await work.runs.get(target.run_id)
        row = await work.session.get(SubscriptionTask, target.task_id)
        assert row is not None
        return factory, delegated, target, sibling, run.version, row.version


async def _forward_pending(factory, session_factory, child, receipt, tmp_path, *, telemetry=None):
    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("feedback-primary", _reservation())
    assert primary is not None and primary.task.task_id == receipt.primary_task_id
    request = await SubscriptionRequestBuilder(factory).build(primary)
    decision = decode_final(
        {
            "kind": "forward_feedback",
            "task_id": str(child.task_id),
            "feedback_receipt_id": str(receipt.receipt_id),
            "feedback_digest": receipt.feedback_digest,
        },
        request,
    ).decision
    proof = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt,
                decision=decision,
                telemetry=_known() if telemetry is None else telemetry,
                launch_proof=proof,
            ),
        )
    ).disposition == "decision_pending"
    recovery = await SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    ).reconcile_all()
    assert recovery.applied == 1 and recovery.deferred == recovery.unsupported == 0
    return primary, request


async def _cancel_run(factory, command_repository, run_id, *, key):
    async with factory() as work:
        run = await work.runs.get(run_id)
    await command_repository.enqueue(
        run_id=run_id,
        command_type="cancel",
        idempotency_key=key,
        expected_run_version=run.version,
        actor_id=uuid4(),
        payload={},
    )
    command = await command_repository.claim_next(
        worker_id=key,
        lease_seconds=30,
        lane=CommandLane.CONTROL,
    )
    assert command is not None and command.run_id == run_id
    async with factory() as work:
        await CancelRunHandler()(command, work)


async def _unlaunched_cancel_case(session_factory, tmp_path, *, case_key):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    actor = _actor()
    feedback_request = SubscriptionTaskFeedbackRequest(
        expected_run_version=run_version,
        expected_task_version=task_version,
        feedback="Close this undelivered receipt when cancellation stops the bound worker.",
    )
    feedback = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key=f"{case_key}-feedback",
        request=feedback_request,
    )
    await _forward_pending(factory, session_factory, child, feedback, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next(f"{case_key}-worker", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    await SubscriptionRequestBuilder(factory).build(worker)
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        assert row.state == "forwarded" and row.delivery_attempt_id == worker.attempt.attempt_id
    cancelled = await SubscriptionTaskControlService(factory).control(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key=f"{case_key}-worker",
        request=SubscriptionTaskControlRequest(
            action="cancel",
            expected_run_version=run_version,
            expected_task_version=worker.task_version,
            reason="Cancellation supersedes unproved delivery.",
        ),
    )
    assert cancelled.status == "cancel_requested"
    return factory, child, actor, feedback_request, feedback, cancelled, executor, worker


@pytest.mark.integration
async def test_feedback_survives_restart_and_reaches_exact_worker_without_new_task(
    session_factory, tmp_path
):
    factory, delegated, child, run_version, task_version, primary_attempts = await _delegated_case(
        session_factory, tmp_path
    )
    actor = _actor()
    text = "Keep the partial parser work and add the missing replay assertion."
    service = SubscriptionTaskFeedbackService(factory)
    receipt = await service.submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="feedback-main",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback=text,
        ),
    )
    assert receipt.operator_id == actor.actor_id
    assert receipt.primary_task_id == delegated.task.task_id
    assert receipt.status == "pending_primary"

    async with factory() as work:
        usage = await work.subscription_budget.usage(child.run_id, delegated.task.task_id)
        task_count = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionTask)
            .where(SubscriptionTask.run_id == child.run_id)
        )
        audit = await work.session.scalar(
            select(OperatorAuditEvent).where(
                OperatorAuditEvent.correlation_id == receipt.receipt_id
            )
        )
        assert usage.consumed.provider_attempts == primary_attempts
        assert task_count == 2
        assert audit is not None and text not in str(audit.payload)

    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("feedback-primary", _reservation())
    assert primary is not None and primary.task.task_id == delegated.task.task_id
    primary_request = await SubscriptionRequestBuilder(factory).build(primary)
    pending = primary_request.untrusted_context["pending_worker_feedback"]
    assert pending["feedback"] == text
    assert pending["receipt_id"] == str(receipt.receipt_id)
    assert primary_request.authorization.permitted_tools == frozenset()
    forwarded = decode_final(
        {
            "kind": "forward_feedback",
            "task_id": str(child.task_id),
            "feedback_receipt_id": str(receipt.receipt_id),
            "feedback_digest": receipt.feedback_digest,
        },
        primary_request,
    ).decision
    assert isinstance(forwarded, ForwardFeedbackDecision)
    primary_proof = await record_stopped_launch(session_factory, primary)
    settled = await executor.settle(
        primary,
        SubscriptionInvocationResult(
            attempt=primary.attempt,
            decision=forwarded,
            telemetry=_known(),
            launch_proof=primary_proof,
        ),
    )
    assert settled.disposition == "decision_pending"

    recovery = await SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    ).reconcile_all()
    assert recovery.applied == 1 and recovery.deferred == recovery.unsupported == 0
    replay = await SubscriptionDecisionApplication(factory).apply_feedback(
        primary.attempt.attempt_id
    )
    assert replay.replayed and replay.disposition == "feedback_forwarded"
    usage_page = SubscriptionUsagePage.model_validate(
        await SubscriptionUsageQuery(session_factory).usage(
            run_id=child.run_id,
            include_assessment=True,
        )
    )
    assert usage_page.assessment is not None
    assert sum(item.applied_decisions for item in usage_page.assessment.outcomes) == 2
    assert usage_page.assessment.unverified_decisions == 0
    async with factory() as work:
        primary_usage = await work.subscription_budget.usage(child.run_id, delegated.task.task_id)
        assert primary_usage.consumed.provider_attempts == primary_attempts + 1
        assert primary_usage.consumed.repairs == 0

    worker = await executor.admit_next("same-worker", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    assert worker.task.route == child.route
    assert worker.task.owned_paths == child.owned_paths
    worker_request = await SubscriptionRequestBuilder(factory).build(worker)
    delivered = worker_request.untrusted_context["operator_feedback"]
    assert len(delivered) == 1
    assert delivered[0]["feedback"] == text
    assert delivered[0]["feedback_digest"] == receipt.feedback_digest
    assert worker_request.authorization.permitted_tools

    worker_proof = await record_stopped_launch(session_factory, worker)
    worker_result = SubscriptionInvocationResult(
        attempt=worker.attempt,
        decision=TaskHandoff(
            run_id=worker.task.run_id,
            task_id=worker.task.task_id,
            attempt_id=worker.attempt.attempt_id,
            status=HandoffStatus.BLOCKED,
            summary="Feedback observed; retained partial work without a tool call.",
        ),
        telemetry=_known(tool_call_count=0),
        launch_proof=worker_proof,
    )
    assert (await executor.settle(worker, worker_result)).disposition == "handoff"
    async with factory() as work:
        feedback = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        usage = await work.subscription_budget.usage(child.run_id, child.task_id)
        task_count = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionTask)
            .where(SubscriptionTask.run_id == child.run_id)
        )
        assert feedback.state == "delivered"
        assert feedback.delivery_attempt_id == worker.attempt.attempt_id
        assert usage.consumed.provider_attempts == 1
        assert usage.consumed.tool_calls == 0
        assert task_count == 2

    replayed_receipt = await service.submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="feedback-main",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback=text,
        ),
    )
    assert replayed_receipt == receipt
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row is not None
        with pytest.raises(TaskFeedbackConflict, match="stored task feedback receipt differs"):
            await work.subscription_feedback.verify_receipt(
                StoredTaskFeedback(receipt=receipt.model_copy(update={"status": "delivered"})),
                actor_id=actor.actor_id,
                request_digest=row.request_digest,
            )
    with pytest.raises(MutationConflict):
        await service.submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="feedback-main",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=task_version,
                feedback=text + " Changed.",
            ),
        )
    async with factory() as work:
        feedback = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        feedback.observed_task_digest = "b" * 64
        await work.commit()
    with pytest.raises(TaskFeedbackConflict, match="stored task feedback receipt differs"):
        await service.submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="feedback-main",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=task_version,
                feedback=text,
            ),
        )


@pytest.mark.integration
async def test_feedback_replay_rejects_a_foreign_operator_in_the_stored_receipt(
    session_factory, tmp_path
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    actor = _actor()
    request = SubscriptionTaskFeedbackRequest(
        expected_run_version=run_version,
        expected_task_version=task_version,
        feedback="Keep the replay receipt bound to the authenticated operator.",
    )
    service = SubscriptionTaskFeedbackService(factory)
    receipt = await service.submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="feedback-foreign-operator-replay",
        request=request,
    )
    async with factory() as work:
        mutation = await work.session.get(ApiMutation, receipt.receipt_id)
        assert mutation is not None and isinstance(mutation.response_payload, dict)
        payload = dict(mutation.response_payload)
        stored_receipt = dict(payload["receipt"])
        stored_receipt["operator_id"] = str(uuid4())
        payload["receipt"] = stored_receipt
        mutation.response_payload = payload
        await work.commit()

    with pytest.raises(TaskFeedbackConflict, match="stored task feedback receipt differs"):
        await service.submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="feedback-foreign-operator-replay",
            request=request,
        )


@pytest.mark.integration
async def test_fake_primary_and_worker_clients_deliver_feedback_through_worker_runtime(
    session_factory, tmp_path
):
    factory, delegated, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    text = "Keep the same task and validate the exact fake-client delivery path."
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="fake-client-feedback",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback=text,
        ),
    )
    observed: list[tuple[SpecialistPurpose, str]] = []
    revoked = []

    def session_for(admission, request):
        class FakeBoundClient:
            async def execute(self, value):
                assert value == request
                proof = await record_stopped_launch(session_factory, admission)
                if value.task.purpose is SpecialistPurpose.PRIMARY:
                    pending = value.untrusted_context["pending_worker_feedback"]
                    assert pending["feedback"] == text
                    assert value.authorization.permitted_tools == frozenset()
                    decision = decode_final(
                        {
                            "kind": "forward_feedback",
                            "task_id": str(child.task_id),
                            "feedback_receipt_id": str(receipt.receipt_id),
                            "feedback_digest": receipt.feedback_digest,
                        },
                        value,
                    ).decision
                    observed.append((value.task.purpose, pending["feedback"]))
                else:
                    feedback = value.untrusted_context["operator_feedback"]
                    assert len(feedback) == 1 and feedback[0]["feedback"] == text
                    assert value.task.task_id == child.task_id
                    decision = TaskHandoff(
                        run_id=value.task.run_id,
                        task_id=value.task.task_id,
                        attempt_id=value.attempt.attempt_id,
                        status=HandoffStatus.BLOCKED,
                        summary="Exact feedback reached the intended fake worker client.",
                    )
                    observed.append((value.task.purpose, feedback[0]["feedback"]))
                return SubscriptionInvocationResult(
                    attempt=value.attempt,
                    decision=decision,
                    telemetry=_known(tool_call_count=0),
                    launch_proof=proof,
                )

        async def revoke():
            revoked.append(admission.attempt.attempt_id)

        return SubscriptionInvocationSession(FakeBoundClient(), revoke)

    runtime = SubscriptionInvocationWorker(
        factory,
        session_for,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts"),
        owner="fake-feedback-clients",
        reservation=_reservation(),
    )
    primary = await runtime.run_once()
    assert primary is not None and primary.admission.task.task_id == delegated.task.task_id
    assert primary.application is not None
    assert primary.application.disposition == "feedback_forwarded"
    worker = await runtime.run_once()
    assert worker is not None and worker.admission.task.task_id == child.task_id
    assert worker.application is None and worker.attempt.settlement.disposition == "handoff"
    assert observed == [
        (SpecialistPurpose.PRIMARY, text),
        (SpecialistPurpose.ROUTINE_IMPLEMENTATION, text),
    ]
    assert len(revoked) == 2
    async with factory() as work:
        task_count = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionTask)
            .where(SubscriptionTask.run_id == child.run_id)
        )
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert task_count == 2
        assert row.state == "delivered"


@pytest.mark.integration
async def test_feedback_arriving_after_primary_request_survives_that_attempt_failure(
    session_factory, tmp_path
):
    factory, delegated, child, _, _, _ = await _delegated_case(session_factory, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("worker-before-feedback", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    worker_proof = await record_stopped_launch(session_factory, worker)
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=worker_proof,
            ),
        )
    ).disposition == "failed"
    primary = await executor.admit_next("primary-already-investigating", _reservation())
    assert primary is not None and primary.task.task_id == delegated.task.task_id
    built_before_feedback = await SubscriptionRequestBuilder(factory).build(primary)
    assert built_before_feedback.untrusted_context["pending_worker_feedback"] is None
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        run_version = run.version
        task_version = target.version
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="feedback-after-primary-build",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Preserve this request after the in-flight primary attempt fails.",
        ),
    )
    primary_proof = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=primary_proof,
            ),
        )
    ).disposition == "failed"
    retried_primary = await executor.admit_next("feedback-primary-retry", _reservation())
    assert retried_primary is not None
    assert retried_primary.task.task_id == delegated.task.task_id
    retry_request = await SubscriptionRequestBuilder(factory).build(retried_primary)
    pending = retry_request.untrusted_context["pending_worker_feedback"]
    assert pending["receipt_id"] == str(receipt.receipt_id)
    assert pending["feedback"] == (
        "Preserve this request after the in-flight primary attempt fails."
    )


@pytest.mark.integration
@pytest.mark.parametrize("valid_wait", [True, False])
async def test_late_feedback_closes_after_final_primary_decision_application(
    session_factory, tmp_path, valid_wait
):
    factory, delegated, child, _, _, _ = await _delegated_case(
        session_factory, tmp_path, primary_attempts=4
    )
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("worker-before-final-primary", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    worker_proof = await record_stopped_launch(session_factory, worker)
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=worker_proof,
            ),
        )
    ).disposition == "failed"
    primary = await executor.admit_next("final-primary-before-feedback", _reservation())
    assert primary is not None and primary.task.task_id == delegated.task.task_id
    built_before_feedback = await SubscriptionRequestBuilder(factory).build(primary)
    assert built_before_feedback.untrusted_context["pending_worker_feedback"] is None

    async with factory() as work:
        run = await work.runs.get(child.run_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        run_version, task_version = run.version, target.version
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key=f"late-final-primary-decision-{valid_wait}",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Close this receipt after the in-flight final decision is applied.",
        ),
    )
    decision = WaitDecision(
        run_id=child.run_id,
        task_id=primary.task.task_id,
        waiting_on_task_ids=(child.task_id if valid_wait else uuid4(),),
        reason="Finish the already-started primary investigation.",
    )
    primary_proof = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt,
                decision=decision,
                telemetry=_known(),
                launch_proof=primary_proof,
            ),
        )
    ).disposition == "decision_pending"
    applied = await SubscriptionDecisionApplication(factory).apply_wait(primary.attempt.attempt_id)
    assert applied.accepted is valid_wait
    assert applied.disposition == ("waiting" if valid_wait else "decision_rejected")
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        primary_task = await work.session.get(SubscriptionTask, receipt.primary_task_id)
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.primary_attempt_id is None
        assert row.application_digest is not None
        assert primary_task.state == ("queued" if valid_wait else "terminal")
    assert await executor.admit_next("after-final-primary-decision", _reservation()) is None


@pytest.mark.integration
async def test_late_unbound_feedback_closes_on_the_terminal_primary_budget_boundary(
    session_factory, tmp_path, monkeypatch
):
    factory, delegated, child, _, _, _ = await _delegated_case(session_factory, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("worker-before-budget-boundary", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    worker_proof = await record_stopped_launch(session_factory, worker)
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=worker_proof,
            ),
        )
    ).disposition == "failed"
    primary = await executor.admit_next("primary-before-budget-boundary", _reservation())
    assert primary is not None and primary.task.task_id == delegated.task.task_id
    await SubscriptionRequestBuilder(factory).build(primary)

    async with factory() as work:
        run = await work.runs.get(child.run_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        run_version, task_version = run.version, target.version
    actor = _actor()
    request = SubscriptionTaskFeedbackRequest(
        expected_run_version=run_version,
        expected_task_version=task_version,
        feedback="Close this late receipt when shared primary capacity is consumed.",
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="late-primary-budget-boundary",
        request=request,
    )
    original_fit = PostgresSubscriptionBudgetRepository.fit_reservation

    async def exhausted_primary(repository, run_id, task_id, budget):
        if (run_id, task_id) == (child.run_id, primary.task.task_id):
            return None
        return await original_fit(repository, run_id, task_id, budget)

    monkeypatch.setattr(PostgresSubscriptionBudgetRepository, "fit_reservation", exhausted_primary)
    proof = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        target_attempts = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionAttempt)
            .where(SubscriptionAttempt.task_row_id == child.task_id)
        )
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.primary_attempt_id == primary.attempt.attempt_id
        assert row.application_digest is not None
        assert row.delivery_attempt_id is None and row.delivered_at is None
        assert target_attempts == 1
    assert (
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="late-primary-budget-boundary",
            request=request,
        )
    ) == receipt


@pytest.mark.integration
async def test_final_unbuilt_primary_replaces_terminal_forwarding_binding_at_budget_exhaustion(
    session_factory, tmp_path
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path, primary_attempts=4
    )
    actor = _actor()
    request = SubscriptionTaskFeedbackRequest(
        expected_run_version=run_version,
        expected_task_version=task_version,
        feedback="Close against the final primary even when its request was never built.",
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="terminal-unbuilt-primary-feedback",
        request=request,
    )
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("forwarding-primary", _reservation())
    assert first is not None and first.task.task_id == receipt.primary_task_id
    await SubscriptionRequestBuilder(factory).build(first)
    first_proof = await record_stopped_launch(session_factory, first)
    assert (
        await executor.settle(
            first,
            SubscriptionInvocationResult(
                attempt=first.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=first_proof,
            ),
        )
    ).disposition == "failed"
    final = await executor.admit_next("unbuilt-final-primary", _reservation())
    assert final is not None and final.task.task_id == receipt.primary_task_id
    final_proof = await record_stopped_launch(session_factory, final)
    assert (
        await executor.settle(
            final,
            SubscriptionInvocationResult(
                attempt=final.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=final_proof,
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.primary_attempt_id == final.attempt.attempt_id
        assert row.application_digest is not None
        assert row.delivery_attempt_id is None and row.delivered_at is None
    assert (
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="terminal-unbuilt-primary-feedback",
            request=request,
        )
        == receipt
    )


@pytest.mark.integration
async def test_feedback_rejects_stale_foreign_cancelled_and_credential_requests(
    session_factory, tmp_path
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    service = SubscriptionTaskFeedbackService(factory)
    actor = _actor()

    with pytest.raises(TaskFeedbackConflict):
        await service.submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="stale",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=task_version + 1,
                feedback="Use the current parser contract.",
            ),
        )
    with pytest.raises(TaskFeedbackConflict):
        await service.submit(
            run_id=child.run_id,
            task_id=uuid4(),
            actor=actor,
            idempotency_key="foreign",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=task_version,
                feedback="This target is foreign.",
            ),
        )
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task_id)
        task.cancel_requested = scheduled.cancel_requested = True
        await work.commit()
    with pytest.raises(TaskFeedbackConflict):
        await service.submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="cancelled",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=task_version,
                feedback="Do not revive cancelled work.",
            ),
        )
    with pytest.raises(ValueError, match="credential"):
        SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        )


@pytest.mark.integration
async def test_feedback_rejects_a_worker_already_settled_by_primary_acceptance(
    session_factory, tmp_path, monkeypatch
):
    factory, primary, child, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch
    )
    accepted = await application.prepare_acceptance(primary.attempt.attempt_id)
    assert accepted.accepted and accepted.disposition == "task_accepted"
    async with factory() as work:
        run = await work.runs.get(child.task.run_id)
        target = await work.session.get(SubscriptionTask, child.task.task_id)
        run_version = run.version
        task_version = target.version
    with pytest.raises(TaskFeedbackConflict, match="already accepted"):
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=child.task.run_id,
            task_id=child.task.task_id,
            actor=_actor(),
            idempotency_key="accepted-feedback",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=task_version,
                feedback="This must not reopen accepted work.",
            ),
        )


@pytest.mark.integration
async def test_feedback_rejects_a_second_unforwarded_request(session_factory, tmp_path):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    actor = _actor()
    service = SubscriptionTaskFeedbackService(factory)
    await service.submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="first-pending-feedback",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Forward this request before accepting another one.",
        ),
    )
    with pytest.raises(TaskFeedbackConflict, match="already has pending feedback"):
        await service.submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="second-pending-feedback",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=task_version,
                feedback="This request has no separately reserved primary turn.",
            ),
        )


@pytest.mark.integration
async def test_feedback_history_has_a_hard_per_task_bound(session_factory, tmp_path):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory,
        tmp_path,
        primary_attempts=MAX_FEEDBACK_PER_TASK + 3,
    )
    actor = _actor()
    pause = await SubscriptionTaskControlService(factory).control(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="pause-for-history-bound",
        request=SubscriptionTaskControlRequest(
            action="pause",
            expected_run_version=run_version,
            expected_task_version=task_version,
            reason="Keep the worker idle while feedback history is bounded",
        ),
    )
    service = SubscriptionTaskFeedbackService(factory)
    for index in range(MAX_FEEDBACK_PER_TASK):
        receipt = await service.submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key=f"bounded-feedback-{index}",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=pause.task_version,
                feedback=f"Bounded worker guidance {index}.",
            ),
        )
        await _forward_pending(factory, session_factory, child, receipt, tmp_path)
    with pytest.raises(TaskFeedbackConflict, match="history reached its bound"):
        await service.submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="bounded-feedback-overflow",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=pause.task_version,
                feedback="This request exceeds the durable per-task bound.",
            ),
        )


@pytest.mark.integration
async def test_paused_feedback_waits_for_causal_resume_and_cancelled_feedback_closes(
    session_factory, tmp_path
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    actor = _actor()
    controls = SubscriptionTaskControlService(factory)
    pause = await controls.control(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="pause-for-feedback",
        request=SubscriptionTaskControlRequest(
            action="pause",
            expected_run_version=run_version,
            expected_task_version=task_version,
            reason="Hold the same worker while feedback is routed",
        ),
    )
    assert pause.status == "paused"
    service = SubscriptionTaskFeedbackService(factory)
    feedback = await service.submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="paused-feedback",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=pause.task_version,
            feedback="Retain the partial work; resume with the boundary assertion.",
        ),
    )
    await _forward_pending(factory, session_factory, child, feedback, tmp_path)
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("paused-worker", _reservation())
        is None
    )
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        assert row.state == "forwarded" and row.delivery_attempt_id is None

    resumed = await controls.control(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="resume-for-feedback",
        request=SubscriptionTaskControlRequest(
            action="resume",
            expected_run_version=run_version,
            expected_task_version=pause.task_version,
            reason="Continue with retained feedback",
            pause_receipt_id=pause.receipt_id,
        ),
    )
    assert resumed.status == "queued"
    worker = await SubscriptionDecisionExecutor(factory).admit_next(
        "resumed-worker", _reservation()
    )
    assert worker is not None and worker.task.task_id == child.task_id
    request = await SubscriptionRequestBuilder(factory).build(worker)
    assert request.untrusted_context["operator_feedback"][0]["feedback"] == (
        "Retain the partial work; resume with the boundary assertion."
    )

    # A separately submitted request remains durably auditable when a later
    # cancellation wins; forwarding closes it and never revives the worker.
    factory2, _, child2, run2, version2, _ = await _delegated_case(
        session_factory, tmp_path / "cancelled"
    )
    service2 = SubscriptionTaskFeedbackService(factory2)
    feedback2 = await service2.submit(
        run_id=child2.run_id,
        task_id=child2.task_id,
        actor=actor,
        idempotency_key="cancel-feedback",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run2,
            expected_task_version=version2,
            feedback="If still active, keep the parsing boundary narrow.",
        ),
    )
    cancelled = await SubscriptionTaskControlService(factory2).control(
        run_id=child2.run_id,
        task_id=child2.task_id,
        actor=actor,
        idempotency_key="cancel-after-feedback",
        request=SubscriptionTaskControlRequest(
            action="cancel",
            expected_run_version=run2,
            expected_task_version=version2,
            reason="Cancellation supersedes pending feedback",
        ),
    )
    assert cancelled.status == "cancelled"
    await _forward_pending(factory2, session_factory, child2, feedback2, tmp_path / "cancelled")
    followup = await SubscriptionDecisionExecutor(factory2).admit_next(
        "post-cancel-primary", _reservation()
    )
    assert followup is not None and followup.task.task_id == feedback2.primary_task_id
    followup_request = await SubscriptionRequestBuilder(factory2).build(followup)
    assert followup_request.untrusted_context["pending_worker_feedback"] is None
    async with factory2() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback2.receipt_id)
        target = await work.session.get(SubscriptionTask, child2.task_id)
        assert row.state == "closed" and row.closed_reason == "cancelled"
        assert target.state == "terminal" and target.cancel_requested


@pytest.mark.integration
async def test_cancelling_paused_worker_closes_already_forwarded_feedback(
    session_factory, tmp_path
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    actor = _actor()
    controls = SubscriptionTaskControlService(factory)
    paused = await controls.control(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="pause-before-forwarded-cancel",
        request=SubscriptionTaskControlRequest(
            action="pause",
            expected_run_version=run_version,
            expected_task_version=task_version,
            reason="Hold delivery before cancellation",
        ),
    )
    feedback = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="forward-before-cancel",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=paused.task_version,
            feedback="This remains auditable when cancellation supersedes delivery.",
        ),
    )
    await _forward_pending(factory, session_factory, child, feedback, tmp_path)
    cancelled = await controls.control(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="cancel-forwarded-feedback",
        request=SubscriptionTaskControlRequest(
            action="cancel",
            expected_run_version=run_version,
            expected_task_version=paused.task_version,
            reason="Cancel instead of resuming feedback delivery",
        ),
    )
    assert cancelled.status == "cancelled"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        assert row.state == "closed" and row.closed_reason == "cancelled"
        assert row.delivery_attempt_id is None


@pytest.mark.integration
async def test_cancelling_launched_worker_retains_proved_feedback_delivery(
    session_factory, tmp_path
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    actor = _actor()
    feedback = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="feedback-before-active-cancel",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Retain proof that this reached the launched worker before cancellation.",
        ),
    )
    await _forward_pending(factory, session_factory, child, feedback, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("worker-cancelled-after-delivery", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    request = await SubscriptionRequestBuilder(factory).build(worker)
    assert request.untrusted_context["operator_feedback"][0]["feedback"] == (
        "Retain proof that this reached the launched worker before cancellation."
    )
    proof = await record_stopped_launch(session_factory, worker)
    cancelled = await SubscriptionTaskControlService(factory).control(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="cancel-after-feedback-delivery",
        request=SubscriptionTaskControlRequest(
            action="cancel",
            expected_run_version=run_version,
            expected_task_version=worker.task_version,
            reason="Cancel after the worker received its bounded feedback",
        ),
    )
    assert cancelled.status == "cancel_requested"
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.INTERRUPTED,
                telemetry=_known(tool_call_count=0),
                launch_proof=proof,
            ),
        )
    ).disposition == "stale"
    recovery = await SubscriptionTaskControlService(factory).reconcile_all()
    assert recovery.stopped == 1 and recovery.deferred == 0
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        assert row.state == "delivered"
        assert row.delivery_attempt_id == worker.attempt.attempt_id
        assert target.state == "terminal" and target.cancel_requested


@pytest.mark.integration
async def test_settled_worker_feedback_reuses_logical_task_and_partial_work(
    session_factory, tmp_path
):
    factory, _, child, _, _, _ = await _delegated_case(session_factory, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("initial-worker", _reservation())
    assert first is not None and first.task.task_id == child.task_id
    partial = tmp_path / "prepared" / "apps" / "feature" / "partial.py"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("retained partial work\n", encoding="utf-8")
    first_proof = await record_stopped_launch(session_factory, first)
    assert (
        await executor.settle(
            first,
            SubscriptionInvocationResult(
                attempt=first.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=first_proof,
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        current_run_version = run.version
        current_task_version = target.version
    feedback = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="settled-feedback",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=current_run_version,
            expected_task_version=current_task_version,
            feedback="Continue from the retained partial file; correct the protocol response.",
        ),
    )
    await _forward_pending(factory, session_factory, child, feedback, tmp_path)
    second = await executor.admit_next("continued-worker", _reservation())
    assert second is not None
    assert second.task.task_id == first.task.task_id
    assert second.attempt.attempt_number == first.attempt.attempt_number + 1
    request = await SubscriptionRequestBuilder(factory).build(second)
    assert request.untrusted_context["operator_feedback"][0]["feedback"] == (
        "Continue from the retained partial file; correct the protocol response."
    )
    assert partial.read_text(encoding="utf-8") == "retained partial work\n"
    async with factory() as work:
        usage = await work.subscription_budget.usage(child.run_id, child.task_id)
        assert usage.consumed.provider_attempts == 1
        assert usage.outstanding.provider_attempts == 1
        assert usage.consumed.repairs == 0


@pytest.mark.integration
async def test_pending_feedback_fences_same_run_tasks_until_primary_route_recovers(
    session_factory, tmp_path, monkeypatch
):
    from forge.persistence.repositories.subscription_quota import (
        PostgresSubscriptionQuotaRepository,
    )

    factory, delegated, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path, primary_attempts=3
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="feedback-primary-priority-fence",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Preserve the final run attempt while the primary route recovers.",
        ),
    )
    original = PostgresSubscriptionQuotaRepository.route_for_task

    async def primary_unavailable(repository, logical, *, eligible_routes=None):
        if logical.id == delegated.task.task_id:
            return None
        return await original(repository, logical, eligible_routes=eligible_routes)

    executor = SubscriptionDecisionExecutor(factory)
    with monkeypatch.context() as patch:
        patch.setattr(PostgresSubscriptionQuotaRepository, "route_for_task", primary_unavailable)
        assert await executor.admit_next("ordinary-task-must-wait", _reservation()) is None
    primary = await executor.admit_next("feedback-primary-after-route-recovery", _reservation())
    assert primary is not None and primary.task.task_id == delegated.task.task_id
    request = await SubscriptionRequestBuilder(factory).build(primary)
    assert request.untrusted_context["pending_worker_feedback"]["receipt_id"] == str(
        receipt.receipt_id
    )
    async with factory() as work:
        child_attempts = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionAttempt)
            .where(SubscriptionAttempt.task_row_id == child.task_id)
        )
        assert child_attempts == 0


@pytest.mark.integration
async def test_forwarded_feedback_fences_sibling_until_worker_route_recovers(
    session_factory, tmp_path, monkeypatch
):
    from forge.persistence.repositories.subscription_quota import (
        PostgresSubscriptionQuotaRepository,
    )

    factory, _, target, sibling, run_version, task_version = await _delegated_pair_case(
        session_factory, tmp_path
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=target.run_id,
        task_id=target.task_id,
        actor=_actor(),
        idempotency_key="feedback-worker-priority-fence",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Preserve the final run attempt while this worker route recovers.",
        ),
    )
    await _forward_pending(factory, session_factory, target, receipt, tmp_path)
    original = PostgresSubscriptionQuotaRepository.route_for_task

    async def target_unavailable(repository, logical, *, eligible_routes=None):
        if logical.id == target.task_id:
            return None
        return await original(repository, logical, eligible_routes=eligible_routes)

    executor = SubscriptionDecisionExecutor(factory)
    with monkeypatch.context() as patch:
        patch.setattr(PostgresSubscriptionQuotaRepository, "route_for_task", target_unavailable)
        assert await executor.admit_next("sibling-must-wait", _reservation()) is None
    worker = await executor.admit_next("feedback-worker-after-route-recovery", _reservation())
    assert worker is not None and worker.task.task_id == target.task_id
    request = await SubscriptionRequestBuilder(factory).build(worker)
    assert request.untrusted_context["operator_feedback"][0]["receipt_id"] == str(
        receipt.receipt_id
    )
    async with factory() as work:
        sibling_attempts = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionAttempt)
            .where(SubscriptionAttempt.task_row_id == sibling.task_id)
        )
        assert sibling_attempts == 0


@pytest.mark.integration
async def test_feedback_priority_fence_allows_the_target_dependency(session_factory, tmp_path):
    factory, _, target, sibling, run_version, task_version = await _delegated_pair_case(
        session_factory,
        tmp_path,
        primary_attempts=5,
        target_depends_on_sibling=True,
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=target.run_id,
        task_id=target.task_id,
        actor=_actor(),
        idempotency_key="feedback-dependency-priority-fence",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Wait for the declared dependency without admitting unrelated work.",
        ),
    )
    await _forward_pending(factory, session_factory, target, receipt, tmp_path)
    dependency = await SubscriptionDecisionExecutor(factory).admit_next(
        "feedback-target-dependency", _reservation()
    )
    assert dependency is not None and dependency.task.task_id == sibling.task_id


@pytest.mark.integration
async def test_feedback_fence_survives_the_primary_decision_recovery_window(
    session_factory, tmp_path
):
    factory, delegated, child, sibling, run_version, task_version = await _delegated_pair_case(
        session_factory, tmp_path, primary_attempts=5
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="feedback-decision-recovery-fence",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Do not admit another task before this forwarding decision is applied.",
        ),
    )
    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("feedback-primary-awaits-application", _reservation())
    assert primary is not None and primary.task.task_id == delegated.task.task_id
    request = await SubscriptionRequestBuilder(factory).build(primary)
    decision = decode_final(
        {
            "kind": "forward_feedback",
            "task_id": str(child.task_id),
            "feedback_receipt_id": str(receipt.receipt_id),
            "feedback_digest": receipt.feedback_digest,
        },
        request,
    ).decision
    proof = await record_stopped_launch(session_factory, primary)
    settled = await executor.settle(
        primary,
        SubscriptionInvocationResult(
            attempt=primary.attempt,
            decision=decision,
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    assert settled.disposition == "decision_pending"
    assert await executor.admit_next("decision-application-gap", _reservation()) is None

    recovery = await SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "recovery-gap-artifacts")
    ).reconcile_all()
    assert recovery.applied == 1
    worker = await executor.admit_next("feedback-after-application", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    worker_request = await SubscriptionRequestBuilder(factory).build(worker)
    assert worker_request.untrusted_context["operator_feedback"][0]["receipt_id"] == str(
        receipt.receipt_id
    )
    async with factory() as work:
        sibling_attempts = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionAttempt)
            .where(SubscriptionAttempt.task_row_id == sibling.task_id)
        )
        assert sibling_attempts == 0


@pytest.mark.integration
async def test_dependency_exhaustion_closes_forwarded_feedback(session_factory, tmp_path):
    factory, _, target, sibling, run_version, task_version = await _delegated_pair_case(
        session_factory,
        tmp_path,
        primary_attempts=4,
        target_depends_on_sibling=True,
    )
    actor = _actor()
    request = SubscriptionTaskFeedbackRequest(
        expected_run_version=run_version,
        expected_task_version=task_version,
        feedback="Close this receipt if the dependency consumes the final run attempt.",
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=target.run_id,
        task_id=target.task_id,
        actor=actor,
        idempotency_key="feedback-dependency-exhaustion",
        request=request,
    )
    await _forward_pending(factory, session_factory, target, receipt, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    dependency = await executor.admit_next("feedback-final-dependency", _reservation())
    assert dependency is not None and dependency.task.task_id == sibling.task_id
    proof = await record_stopped_launch(session_factory, dependency)
    assert (
        await executor.settle(
            dependency,
            SubscriptionInvocationResult(
                attempt=dependency.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"

    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        target_attempts = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionAttempt)
            .where(SubscriptionAttempt.task_row_id == target.task_id)
        )
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.delivery_attempt_id is None and target_attempts == 0
    assert await executor.admit_next("feedback-after-exhaustion", _reservation()) is None
    assert (
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=target.run_id,
            task_id=target.task_id,
            actor=actor,
            idempotency_key="feedback-dependency-exhaustion",
            request=request,
        )
        == receipt
    )


@pytest.mark.integration
async def test_outstanding_dependency_reservation_does_not_close_feedback(
    session_factory, tmp_path
):
    factory, _, target, sibling, run_version, task_version = await _delegated_pair_case(
        session_factory,
        tmp_path,
        primary_attempts=5,
        primary_duration_seconds=11,
        target_depends_on_sibling=True,
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=target.run_id,
        task_id=target.task_id,
        actor=_actor(),
        idempotency_key="feedback-temporary-dependency-reservation",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Wait for the dependency reservation to settle before judging capacity.",
        ),
    )
    await _forward_pending(factory, session_factory, target, receipt, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    dependency = await executor.admit_next("feedback-reserved-dependency", _reservation())
    assert dependency is not None and dependency.task.task_id == sibling.task_id

    async with factory() as work:
        assert await work.subscription_feedback.close_exhausted(target.run_id) == 0
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "forwarded" and row.closed_reason is None
        await work.commit()

    proof = await record_stopped_launch(session_factory, dependency)
    assert (
        await executor.settle(
            dependency,
            SubscriptionInvocationResult(
                attempt=dependency.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"
    worker = await executor.admit_next("feedback-after-dependency-refund", _reservation())
    assert worker is not None and worker.task.task_id == target.task_id
    request = await SubscriptionRequestBuilder(factory).build(worker)
    assert request.untrusted_context["operator_feedback"][0]["receipt_id"] == str(
        receipt.receipt_id
    )


@pytest.mark.integration
async def test_forwarding_waits_for_refundable_dependency_capacity(session_factory, tmp_path):
    factory, _, target, sibling, run_version, task_version = await _delegated_pair_case(
        session_factory,
        tmp_path,
        primary_attempts=5,
        primary_duration_seconds=12,
        target_depends_on_sibling=True,
    )
    executor = SubscriptionDecisionExecutor(factory)
    dependency = await executor.admit_next("dependency-before-feedback", _reservation())
    assert dependency is not None and dependency.task.task_id == sibling.task_id
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=target.run_id,
        task_id=target.task_id,
        actor=_actor(),
        idempotency_key="feedback-while-dependency-reserved",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Retain this feedback while the dependency holds refundable capacity.",
        ),
    )
    await _forward_pending(
        factory,
        session_factory,
        target,
        receipt,
        tmp_path,
        telemetry=_known(duration_ms=1500),
    )
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "forwarded" and row.closed_reason is None

    proof = await record_stopped_launch(session_factory, dependency)
    assert (
        await executor.settle(
            dependency,
            SubscriptionInvocationResult(
                attempt=dependency.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"
    worker = await executor.admit_next("feedback-after-forwarding-refund", _reservation())
    assert worker is not None and worker.task.task_id == target.task_id
    request = await SubscriptionRequestBuilder(factory).build(worker)
    assert request.untrusted_context["operator_feedback"][0]["receipt_id"] == str(
        receipt.receipt_id
    )


@pytest.mark.integration
async def test_feedback_submission_waits_for_active_attempt_capacity(session_factory, tmp_path):
    factory, _, child, _, _, _ = await _delegated_case(
        session_factory,
        tmp_path,
        primary_attempts=5,
        primary_duration_seconds=11,
    )
    executor = SubscriptionDecisionExecutor(factory)
    active = await executor.admit_next("worker-before-capacity-feedback", _reservation())
    assert active is not None and active.task.task_id == child.task_id
    await SubscriptionRequestBuilder(factory).build(active)
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        task = await work.session.get(SubscriptionTask, child.task_id)
        assert task is not None
        run_version, task_version = run.version, task.version
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="feedback-waits-for-active-capacity",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Retain this request until the active reservation settles.",
        ),
    )
    assert receipt.status == "pending_primary"

    proof = await record_stopped_launch(session_factory, active)
    assert (
        await executor.settle(
            active,
            SubscriptionInvocationResult(
                attempt=active.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"
    await _forward_pending(factory, session_factory, child, receipt, tmp_path)
    worker = await executor.admit_next("worker-after-capacity-refund", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    request = await SubscriptionRequestBuilder(factory).build(worker)
    assert request.untrusted_context["operator_feedback"][0]["receipt_id"] == str(
        receipt.receipt_id
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("active_duration_ms", "capacity_restored"),
    [(1, True), (10_500, False)],
)
async def test_failed_primary_reconciles_after_other_attempt_capacity(
    session_factory, tmp_path, active_duration_ms, capacity_restored
):
    factory, _, first, second, _, _ = await _delegated_pair_case(
        session_factory,
        tmp_path,
        primary_attempts=6,
        primary_duration_seconds=12,
    )
    executor = SubscriptionDecisionExecutor(factory)
    active = await executor.admit_next("worker-during-primary-failure", _reservation())
    assert active is not None
    await SubscriptionRequestBuilder(factory).build(active)
    target = second if active.task.task_id == first.task_id else first
    async with factory() as work:
        run = await work.runs.get(target.run_id)
        target_row = await work.session.get(SubscriptionTask, target.task_id)
        assert target_row is not None
        run_version, task_version = run.version, target_row.version
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=target.run_id,
        task_id=target.task_id,
        actor=_actor(),
        idempotency_key="feedback-primary-failure-with-active-capacity",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Retry forwarding after the other active reservation settles.",
        ),
    )
    primary = await executor.admit_next("failing-feedback-primary", _reservation())
    assert primary is not None and primary.task.task_id == receipt.primary_task_id
    await SubscriptionRequestBuilder(factory).build(primary)
    primary_proof = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(duration_ms=1500),
                launch_proof=primary_proof,
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        primary_row = await work.session.get(SubscriptionTask, receipt.primary_task_id)
        assert row.state == "pending_primary" and row.closed_reason is None
        assert primary_row is not None and primary_row.state == "queued"

    active_proof = await record_stopped_launch(session_factory, active)
    assert (
        await executor.settle(
            active,
            SubscriptionInvocationResult(
                attempt=active.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(duration_ms=active_duration_ms),
                launch_proof=active_proof,
            ),
        )
    ).disposition == "failed"
    if not capacity_restored:
        async with factory() as work:
            row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
            assert row.state == "closed" and row.closed_reason == "budget_exhausted"
            assert row.primary_attempt_id == primary.attempt.attempt_id
        return
    retry = await executor.admit_next("feedback-primary-after-capacity-refund", _reservation())
    assert retry is not None and retry.task.task_id == receipt.primary_task_id
    request = await SubscriptionRequestBuilder(factory).build(retry)
    assert request.untrusted_context["pending_worker_feedback"]["receipt_id"] == str(
        receipt.receipt_id
    )


@pytest.mark.integration
async def test_failed_delivery_waits_for_other_attempt_capacity(session_factory, tmp_path):
    factory, _, first, second, _, _ = await _delegated_pair_case(
        session_factory,
        tmp_path,
        primary_attempts=7,
        primary_duration_seconds=12,
    )
    executor = SubscriptionDecisionExecutor(factory)
    active = await executor.admit_next("sibling-during-delivery-failure", _reservation())
    assert active is not None
    await SubscriptionRequestBuilder(factory).build(active)
    target = second if active.task.task_id == first.task_id else first
    async with factory() as work:
        run = await work.runs.get(target.run_id)
        target_row = await work.session.get(SubscriptionTask, target.task_id)
        assert target_row is not None
        run_version, task_version = run.version, target_row.version
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=target.run_id,
        task_id=target.task_id,
        actor=_actor(),
        idempotency_key="feedback-delivery-failure-with-active-capacity",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Retry delivery after the other active reservation settles.",
        ),
    )
    await _forward_pending(factory, session_factory, target, receipt, tmp_path)
    delivery = await executor.admit_next("failing-feedback-delivery", _reservation())
    assert delivery is not None and delivery.task.task_id == target.task_id
    request = await SubscriptionRequestBuilder(factory).build(delivery)
    assert request.untrusted_context["operator_feedback"][0]["receipt_id"] == str(
        receipt.receipt_id
    )
    assert (
        await executor.settle(
            delivery,
            SubscriptionInvocationResult(
                attempt=delivery.attempt,
                failure=SubscriptionFailure.UNAVAILABLE,
                telemetry=_known(duration_ms=1500),
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        target_row = await work.session.get(SubscriptionTask, target.task_id)
        assert row.state == "forwarded" and row.delivery_attempt_id is None
        assert target_row is not None and target_row.state == "queued"

    active_proof = await record_stopped_launch(session_factory, active)
    assert (
        await executor.settle(
            active,
            SubscriptionInvocationResult(
                attempt=active.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=active_proof,
            ),
        )
    ).disposition == "failed"
    retry = await executor.admit_next("feedback-delivery-after-capacity-refund", _reservation())
    assert retry is not None and retry.task.task_id == target.task_id
    retry_request = await SubscriptionRequestBuilder(factory).build(retry)
    assert retry_request.untrusted_context["operator_feedback"][0]["receipt_id"] == str(
        receipt.receipt_id
    )


@pytest.mark.integration
async def test_last_primary_attempt_closes_feedback_before_worker_delivery(
    session_factory, tmp_path
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path, primary_attempts=3
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="feedback-primary-consumes-final-attempt",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Close this receipt if forwarding consumes the final run attempt.",
        ),
    )
    await _forward_pending(factory, session_factory, child, receipt, tmp_path)
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.delivery_attempt_id is None


@pytest.mark.integration
async def test_unbound_pending_feedback_closes_after_uncertain_budget_exhaustion(
    session_factory, tmp_path
):
    factory, _, child, _, _, _ = await _delegated_case(
        session_factory, tmp_path, primary_attempts=4
    )
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("worker-before-feedback", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    await SubscriptionRequestBuilder(factory).build(worker)
    proof = await record_stopped_launch(session_factory, worker)
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        task = await work.session.get(SubscriptionTask, child.task_id)
        assert task is not None
        run_version, task_version = run.version, task.version
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="feedback-unbound-uncertain-exhaustion",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Close safely if an earlier uncertain attempt exhausts admission policy.",
        ),
    )
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=AttemptTelemetry(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.primary_attempt_id is None and row.application_digest is not None


@pytest.mark.integration
async def test_repair_debit_cannot_strand_unbound_pending_feedback(session_factory, tmp_path):
    factory, _, repair_task, feedback_target, _, _ = await _delegated_pair_case(
        session_factory,
        tmp_path,
        primary_attempts=4,
        primary_repairs=1,
        child_repairs=1,
    )
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("repair-before-feedback", _reservation())
    assert worker is not None and worker.task.task_id == repair_task.task_id
    await SubscriptionRequestBuilder(factory).build(worker)
    proof = await record_stopped_launch(session_factory, worker)
    oversized_scope = ScopeRequestDecision(
        run_id=worker.task.run_id,
        task_id=worker.task.task_id,
        requested_paths=tuple(f"apps/repair-{index}" for index in range(65)),
        reason="This invalid request deterministically consumes the final repair slot.",
    )
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                decision=oversized_scope,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "decision_pending"
    async with factory() as work:
        run = await work.runs.get(feedback_target.run_id)
        target = await work.session.get(SubscriptionTask, feedback_target.task_id)
        assert target is not None
        run_version, task_version = run.version, target.version
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=feedback_target.run_id,
        task_id=feedback_target.task_id,
        actor=_actor(),
        idempotency_key="feedback-before-final-repair-debit",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Close explicitly if an already-pending repair consumes the last slot.",
        ),
    )
    rejected = await SubscriptionDecisionApplication(factory).apply_scope_request(
        worker.attempt.attempt_id
    )
    assert rejected.disposition == "decision_repair_queued"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.primary_attempt_id is None


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["pending", "forwarded"])
async def test_run_cancellation_closes_unbound_feedback(
    session_factory, tmp_path, command_repository, stage
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    actor = _actor()
    request = SubscriptionTaskFeedbackRequest(
        expected_run_version=run_version,
        expected_task_version=task_version,
        feedback=f"Close this {stage} receipt when its run is cancelled.",
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key=f"feedback-run-cancel-{stage}",
        request=request,
    )
    if stage == "forwarded":
        await _forward_pending(factory, session_factory, child, receipt, tmp_path)

    await _cancel_run(
        factory,
        command_repository,
        child.run_id,
        key=f"cancel-run-with-{stage}-feedback",
    )
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert run.state is RunState.CANCELLED
        assert row.state == "closed" and row.closed_reason == "cancelled"
        assert row.delivery_attempt_id is None and row.application_digest is not None
    assert (
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key=f"feedback-run-cancel-{stage}",
            request=request,
        )
        == receipt
    )


@pytest.mark.integration
async def test_run_cancellation_preserves_launched_feedback_delivery(
    session_factory, tmp_path, command_repository
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="feedback-run-cancel-launched",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Retain the delivery proof even if the run is cancelled.",
        ),
    )
    await _forward_pending(factory, session_factory, child, receipt, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("feedback-worker-before-run-cancel", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    await SubscriptionRequestBuilder(factory).build(worker)
    proof = await record_stopped_launch(session_factory, worker)
    await _cancel_run(
        factory,
        command_repository,
        child.run_id,
        key="cancel-run-after-feedback-launch",
    )
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "forwarded"
        assert row.delivery_attempt_id == worker.attempt.attempt_id

    settled = await executor.settle(
        worker,
        SubscriptionInvocationResult(
            attempt=worker.attempt,
            failure=SubscriptionFailure.UNAVAILABLE,
            telemetry=_known(tool_call_count=0),
            launch_proof=proof,
        ),
    )
    assert settled.disposition == "stale"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "delivered" and row.closed_reason is None
        assert row.delivery_attempt_id == worker.attempt.attempt_id


@pytest.mark.integration
async def test_run_cancellation_closes_bound_feedback_without_a_launch(
    session_factory, tmp_path, command_repository
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path
    )
    receipt = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="feedback-run-cancel-unlaunched",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Close after cancellation if the bound request never launched.",
        ),
    )
    await _forward_pending(factory, session_factory, child, receipt, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("feedback-unlaunched-before-run-cancel", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    await SubscriptionRequestBuilder(factory).build(worker)
    await _cancel_run(
        factory,
        command_repository,
        child.run_id,
        key="cancel-run-before-feedback-launch",
    )
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.UNAVAILABLE,
                telemetry=_known(tool_call_count=0),
            ),
        )
    ).disposition == "stale"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt.receipt_id)
        assert row.state == "closed" and row.closed_reason == "cancelled"
        assert row.delivery_attempt_id is None and row.delivered_at is None


@pytest.mark.integration
async def test_feedback_never_resets_an_exhausted_cumulative_task_budget(session_factory, tmp_path):
    factory, _, child, _, _, _ = await _delegated_case(session_factory, tmp_path, child_attempts=1)
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("only-worker-attempt", _reservation())
    assert first is not None and first.task.task_id == child.task_id
    proof = await record_stopped_launch(session_factory, first)
    assert (
        await executor.settle(
            first,
            SubscriptionInvocationResult(
                attempt=first.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        current_run_version = run.version
        current_task_version = target.version
    feedback = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="exhausted-feedback",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=current_run_version,
            expected_task_version=current_task_version,
            feedback="Retain this guidance even though the task budget is exhausted.",
        ),
    )
    await _forward_pending(factory, session_factory, child, feedback, tmp_path)
    followup = await executor.admit_next("budget-followup-primary", _reservation())
    assert followup is not None and followup.task.task_id == feedback.primary_task_id
    assert followup.task.task_id != child.task_id
    async with factory() as work:
        usage = await work.subscription_budget.usage(child.run_id, child.task_id)
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        assert usage.consumed.provider_attempts == 1
        assert usage.outstanding.provider_attempts == 0
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"


@pytest.mark.integration
async def test_feedback_rejects_when_primary_cumulative_budget_cannot_forward(
    session_factory, tmp_path
):
    factory, _, child, _, _, _ = await _delegated_case(
        session_factory,
        tmp_path,
        child_attempts=1,
        primary_attempts=3,
    )
    executor = SubscriptionDecisionExecutor(factory)
    worker = await executor.admit_next("consume-final-run-attempt", _reservation())
    assert worker is not None and worker.task.task_id == child.task_id
    proof = await record_stopped_launch(session_factory, worker)
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        run_version = run.version
        task_version = target.version
    with pytest.raises(TaskFeedbackConflict, match="primary cumulative budget is exhausted"):
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=_actor(),
            idempotency_key="primary-budget-exhausted-feedback",
            request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version,
                expected_task_version=task_version,
                feedback="Do not retain feedback that the primary can never forward.",
            ),
        )
    async with factory() as work:
        count = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionTaskFeedback)
            .where(
                SubscriptionTaskFeedback.run_id == child.run_id,
                SubscriptionTaskFeedback.task_id == child.task_id,
            )
        )
        assert count == 0


@pytest.mark.integration
async def test_undelivered_feedback_closes_if_the_active_attempt_exhausts_budget(
    session_factory, tmp_path
):
    factory, _, child, _, _, _ = await _delegated_case(session_factory, tmp_path, child_attempts=1)
    executor = SubscriptionDecisionExecutor(factory)
    active = await executor.admit_next("active-worker-without-launch", _reservation())
    assert active is not None and active.task.task_id == child.task_id
    request = await SubscriptionRequestBuilder(factory).build(active)
    assert not request.untrusted_context["operator_feedback"]
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        run_version = run.version
        task_version = target.version
    feedback = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=_actor(),
        idempotency_key="active-exhausted-feedback",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=run_version,
            expected_task_version=task_version,
            feedback="Retain this request if the active client never launches.",
        ),
    )
    await _forward_pending(factory, session_factory, child, feedback, tmp_path)
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        assert row.state == "forwarded" and row.delivery_attempt_id is None
    assert (
        await executor.settle(
            active,
            SubscriptionInvocationResult(
                attempt=active.attempt,
                failure=SubscriptionFailure.UNAVAILABLE,
                telemetry=_known(tool_call_count=0),
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        usage = await work.subscription_budget.usage(child.run_id, child.task_id)
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.delivery_attempt_id is None
        assert usage.consumed.provider_attempts == 1
        assert usage.consumed.repairs == 0


@pytest.mark.integration
async def test_cancel_recovery_closes_feedback_bound_to_an_unlaunched_worker(
    session_factory, tmp_path
):
    (
        factory,
        child,
        actor,
        feedback_request,
        feedback,
        _,
        executor,
        worker,
    ) = await _unlaunched_cancel_case(session_factory, tmp_path, case_key="cancel-bound-unlaunched")
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.UNAVAILABLE,
                telemetry=_known(tool_call_count=0),
            ),
        )
    ).disposition == "stale"
    assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        task = await work.session.get(SubscriptionTask, child.task_id)
        attempts = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionAttempt)
            .where(SubscriptionAttempt.task_row_id == child.task_id)
        )
        assert row.state == "closed" and row.closed_reason == "cancelled"
        assert row.delivery_attempt_id is None and row.delivered_at is None
        assert task.state == "terminal" and attempts == 1
    assert (
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="cancel-bound-unlaunched-feedback",
            request=feedback_request,
        )
        == feedback
    )


@pytest.mark.integration
async def test_paused_run_recovery_closes_feedback_bound_to_an_unlaunched_worker(
    session_factory, tmp_path
):
    factory, child, _, _, feedback, _, executor, worker = await _unlaunched_cancel_case(
        session_factory, tmp_path, case_key="paused-cancel-unlaunched"
    )
    async with factory() as work:
        run = await work.runs.get(child.run_id)
        await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        await work.commit()
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.UNAVAILABLE,
                telemetry=_known(tool_call_count=0),
            ),
        )
    ).disposition == "stale"
    report = await SubscriptionTaskControlService(factory).reconcile_all()
    assert report.stopped == 1 and report.deferred == 0
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        task = await work.session.get(SubscriptionTask, child.task_id)
        attempt = await work.session.get(SubscriptionAttempt, worker.attempt.attempt_id)
        assert row.state == "closed" and row.closed_reason == "cancelled"
        assert task.state == "terminal" and attempt.status == "terminal"


@pytest.mark.integration
@pytest.mark.parametrize("barrier", ["operation", "effect", "attempt"])
async def test_unlaunched_cancel_recovery_defers_unresolved_work(
    session_factory, tmp_path, barrier
):
    factory, child, _, _, _, cancelled, executor, worker = await _unlaunched_cancel_case(
        session_factory, tmp_path, case_key=f"guard-{barrier}-unlaunched"
    )
    assert (
        await executor.settle(
            worker,
            SubscriptionInvocationResult(
                attempt=worker.attempt,
                failure=SubscriptionFailure.UNAVAILABLE,
                telemetry=_known(tool_call_count=0),
            ),
        )
    ).disposition == "stale"
    async with factory() as work:
        attempt = await work.session.get(SubscriptionAttempt, worker.attempt.attempt_id)
        assert attempt is not None
        if barrier == "operation":
            work.session.add(
                SubscriptionOperationBinding(
                    attempt_id=attempt.id,
                    provider_call_key="unreceipted-operation",
                    durable_operation_id=uuid4(),
                    payload={"schema_version": 1},
                )
            )
        elif barrier == "effect":
            assert (
                attempt.lease_owner is not None
                and attempt.lease_generation is not None
                and attempt.candidate_epoch is not None
            )
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=child.run_id,
                    task_id=child.task_id,
                    lease_owner=attempt.lease_owner,
                    lease_generation=attempt.lease_generation,
                    candidate_epoch=attempt.candidate_epoch,
                    whole_worktree_exclusive=False,
                )
            )
        else:
            work.session.add(
                SubscriptionAttempt(
                    id=uuid4(),
                    run_id=attempt.run_id,
                    task_row_id=attempt.task_row_id,
                    attempt_number=attempt.attempt_number + 1,
                    idempotency_key="unresolved-other-attempt",
                    route_payload=attempt.route_payload,
                )
            )
        await work.commit()
    report = await SubscriptionTaskControlService(factory).reconcile_all()
    assert report.stopped == 0 and report.deferred == 1
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task_id)
        attempt = await work.session.get(SubscriptionAttempt, worker.attempt.attempt_id)
        stop = await work.session.get(SubscriptionTaskStop, cancelled.receipt_id)
        assert task.state == "reconciling" and attempt.status == "reconciling"
        assert stop.state == "requested"


@pytest.mark.integration
async def test_final_primary_failure_closes_bound_pending_feedback_without_a_worker_retry(
    session_factory, tmp_path
):
    factory, _, child, run_version, task_version, _ = await _delegated_case(
        session_factory, tmp_path, primary_attempts=3
    )
    actor = _actor()
    feedback_request = SubscriptionTaskFeedbackRequest(
        expected_run_version=run_version,
        expected_task_version=task_version,
        feedback="Close this receipt if its only forwarding primary attempt fails.",
    )
    feedback = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.run_id,
        task_id=child.task_id,
        actor=actor,
        idempotency_key="final-primary-failure-feedback",
        request=feedback_request,
    )
    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("only-feedback-primary", _reservation())
    assert primary is not None and primary.task.task_id == feedback.primary_task_id
    await SubscriptionRequestBuilder(factory).build(primary)
    proof = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "failed"
    async with factory() as work:
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        primary_task = await work.session.get(SubscriptionTask, feedback.primary_task_id)
        target = await work.session.get(SubscriptionTask, child.task_id)
        target_attempts = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionAttempt)
            .where(SubscriptionAttempt.task_row_id == child.task_id)
        )
        assert row.state == "closed" and row.closed_reason == "budget_exhausted"
        assert row.primary_attempt_id == primary.attempt.attempt_id
        assert row.delivery_attempt_id is None and row.delivered_at is None
        assert primary_task.state == "terminal" and target.state == "queued"
        assert target_attempts == 0
    assert (
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=child.run_id,
            task_id=child.task_id,
            actor=actor,
            idempotency_key="final-primary-failure-feedback",
            request=feedback_request,
        )
        == feedback
    )


@pytest.mark.integration
async def test_feedback_survives_real_reassignment_without_resetting_budget(
    session_factory, tmp_path, monkeypatch
):
    factory, application, reassignment_primary, child, decision = await reassignment_case(
        session_factory, tmp_path, monkeypatch
    )
    partial = tmp_path / "prepared" / "apps" / "feature" / "partial.py"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("preserved across reassignment\n", encoding="utf-8")
    async with factory() as work:
        run = await work.runs.get(child.task.run_id)
        target = await work.session.get(SubscriptionTask, child.task.task_id)
        current_run_version = run.version
        current_task_version = target.version
    feedback = await SubscriptionTaskFeedbackService(factory).submit(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        actor=_actor(),
        idempotency_key="reassigned-feedback",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=current_run_version,
            expected_task_version=current_task_version,
            feedback="Use the preserved partial file on the approved fallback route.",
        ),
    )
    reassigned = await application.apply_reassignment(reassignment_primary.attempt.attempt_id)
    assert reassigned.accepted and reassigned.disposition == "reassigned"
    await _forward_pending(factory, session_factory, child.task, feedback, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    replacement = await executor.admit_next("replacement-worker", _reservation())
    assert replacement is not None and replacement.task.task_id == child.task.task_id
    assert replacement.task.route.effective == decision.new_route
    request = await SubscriptionRequestBuilder(factory).build(replacement)
    assert request.untrusted_context["operator_feedback"][0]["feedback"] == (
        "Use the preserved partial file on the approved fallback route."
    )
    proof = await record_stopped_launch(session_factory, replacement)
    await executor.settle(
        replacement,
        SubscriptionInvocationResult(
            attempt=replacement.attempt,
            failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    assert partial.read_text(encoding="utf-8") == "preserved across reassignment\n"
    async with factory() as work:
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        row = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
        assert usage.consumed.provider_attempts == 2
        assert usage.consumed.repairs == 0
        assert row.state == "delivered"
