"""Durable operator feedback crosses primary recovery into the same worker task."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import decode_final
from forge.api.schemas.subscription_usage import SubscriptionUsagePage
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
from forge.domain.subscription import (
    ForwardFeedbackDecision,
    HandoffStatus,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
)
from forge.domain.subscription_feedback import (
    StoredTaskFeedback,
    SubscriptionTaskFeedbackRequest,
    TaskFeedbackConflict,
)
from forge.domain.subscription_task_controls import SubscriptionTaskControlRequest
from forge.persistence.models import OperatorAuditEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_feedback import SubscriptionTaskFeedback
from forge.persistence.queries.subscription_usage import SubscriptionUsageQuery
from forge.persistence.repositories.mutations import MutationConflict
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


async def _delegated_case(session_factory, tmp_path, *, child_attempts=3, primary_attempts=8):
    factory, delegated, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, _parent: (
            replace(
                child,
                budget=replace(child.budget, max_provider_attempts=child_attempts),
            ),
        ),
        primary_budget=TaskBudget(max_provider_attempts=primary_attempts, max_repairs=0),
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


async def _forward_pending(factory, session_factory, child, receipt, tmp_path):
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
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
    ).disposition == "decision_pending"
    recovery = await SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    ).reconcile_all()
    assert recovery.applied == 1 and recovery.deferred == recovery.unsupported == 0
    return primary, request


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
