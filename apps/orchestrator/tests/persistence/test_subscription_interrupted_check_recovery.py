"""Explicit owner settlement of an interrupted check with no result."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_recovery import SubscriptionRecoveryService
from forge.domain.operation import canonical_digest
from forge.domain.subscription import TaskBudget, UnknownTelemetryPolicy
from forge.domain.subscription_recovery import (
    RecoveryAction,
    RecoveryApplyRequest,
    RecoveryPreviewRequest,
)
from forge.domain.tool import ToolName
from forge.persistence.models import Run
from forge.persistence.models.execution import OperationIntent, ToolCall
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_recovery import SubscriptionRecoveryReceipt
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.queries.recovery import startup_intervention_hold, unresolved_work
from forge.persistence.repositories.subscription_launch import launches_confirmed
from forge.persistence.repositories.subscription_recovery import (
    PostgresSubscriptionRecoveryRepository,
    RecoveryConflict,
)
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_preparation import preparation_case
from test_subscription_usage import _reservation


async def interrupted_case(session_factory, tmp_path, *, max_repairs=3, specialist=False):
    if specialist:
        from dataclasses import replace

        from forge.application.services.subscription_decisions import (
            SubscriptionDecisionApplication,
        )
        from test_subscription_delegation_application import delegation_case

        budget = TaskBudget(
            max_provider_attempts=8,
            max_repairs=max_repairs,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=5),
        )
        factory, parent, _, _ = await delegation_case(
            session_factory,
            tmp_path,
            lambda child, _: (replace(child, budget=budget, max_repairs=max_repairs),),
            primary_budget=budget,
            plan_scope=("apps",),
        )
        await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    else:
        factory = await _prepared_primary(session_factory, tmp_path, max_repairs)
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "interrupted-owner", _reservation()
    )
    assert admission is not None
    return await _interrupt_admission(factory, admission)


async def _prepared_primary(session_factory, tmp_path, max_repairs):
    factory, _evidence, command, preparation, _ = await preparation_case(
        session_factory,
        tmp_path,
        plan_scope=("apps",),
        primary_budget=TaskBudget(
            max_provider_attempts=5,
            max_repairs=max_repairs,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=5),
        ),
    )
    async with factory() as work:
        await preparation.execute(command, work)
    return factory


async def _interrupt_admission(factory, admission):
    identity = admission.attempt
    operation_id = uuid4()
    args = {"command_name": "unit"}
    async with factory() as work:
        run = await work.runs.get(identity.run_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, identity.task_id)
        attempt = await work.session.get(SubscriptionAttempt, identity.attempt_id)
        assert scheduled is not None and attempt is not None
        request = {
            "authority_schema_version": 2,
            "protocol_version": 1,
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "subscription_task_id": str(identity.task_id),
            "subscription_attempt_id": str(identity.attempt_id),
            "subscription_purpose": "primary",
            "tool_call_id": str(operation_id),
            "policy_version": run.policy_version,
            "worktree_id": scheduled.worktree_id,
            "head_sha": "a" * 40,
            "command_name": "unit",
            "kind": "test",
            "command_digest": "b" * 64,
            "environment_keys_digest": canonical_digest([]),
        }
        work.session.add(
            OperationIntent(
                id=operation_id,
                run_id=run.id,
                operation_kind="named_check",
                idempotency_key=f"tool:{operation_id}",
                request_schema_version=1,
                request_payload=request,
                request_digest=canonical_digest(request),
                status="NEEDS_RECONCILIATION",
            )
        )
        work.session.add(
            ToolCall(
                id=operation_id,
                run_id=run.id,
                tool_name=ToolName.BUILD_RUN_NAMED_CHECK.value,
                subscription_task_id=identity.task_id,
                subscription_attempt_id=identity.attempt_id,
                subscription_purpose="primary",
                arguments_schema_version=1,
                normalized_arguments=args,
                authorized=True,
                status="RUNNING",
                started_at=datetime.now(UTC) - timedelta(minutes=10),
                result_metadata_schema_version=1,
                result_metadata={
                    "operation_intent_id": str(operation_id),
                    "correlation_id": str(operation_id),
                    "policy_version": run.policy_version,
                    "request_digest": canonical_digest(args),
                    "resource_id": scheduled.worktree_id,
                    "invocation_schema_version": 1,
                },
            )
        )
        work.session.add(
            SubscriptionScheduledEffect(
                id=operation_id,
                run_id=run.id,
                task_id=identity.task_id,
                lease_owner=attempt.lease_owner,
                lease_generation=attempt.lease_generation,
                candidate_epoch=attempt.candidate_epoch,
                whole_worktree_exclusive=True,
                state="admitted",
            )
        )
        work.session.add(
            SubscriptionOperationBinding(
                attempt_id=identity.attempt_id,
                provider_call_key="check-1",
                durable_operation_id=operation_id,
                payload={
                    "tool_name": ToolName.BUILD_RUN_NAMED_CHECK.value,
                    "arguments_digest": canonical_digest(args),
                },
            )
        )
        work.session.add(
            SubscriptionClientLaunch(
                attempt_id=identity.attempt_id,
                launch_id=str(uuid4()),
                worker_identity=attempt.lease_owner,
                state="started",
                pid=12345,
                process_start_token="old-process",
            )
        )
        scheduled.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await work.runs.intervene(
            run.id,
            run.version,
            "run.recovery_intervention",
            {"reason": "startup_outcome_unresolved", "operation_id": str(operation_id)},
        )
        await work.commit()
    return factory, identity, operation_id


async def test_owner_can_preview_resultless_interrupted_check(session_factory, tmp_path):
    factory, identity, _ = await interrupted_case(session_factory, tmp_path)
    async with factory() as work:
        snapshot = await PostgresSubscriptionRecoveryRepository(work.session).preview(
            identity.run_id,
            identity.task_id,
            identity.attempt_id,
            RecoveryAction.ACKNOWLEDGE_INTERRUPTED_CHECK,
        )
    assert snapshot.eligible, snapshot.reason_code
    assert "unknown" in snapshot.message.lower()
    assert "owner" in snapshot.message.lower()


async def owner_preview(factory, identity):
    service = SubscriptionRecoveryService(factory)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    preview = await service.preview(
        run_id=identity.run_id,
        task_id=identity.task_id,
        attempt_id=identity.attempt_id,
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.ACKNOWLEDGE_INTERRUPTED_CHECK),
    )
    assert preview.eligible, preview.reason_code
    return (
        service,
        actor,
        RecoveryApplyRequest(
            action=preview.action,
            preview_token=preview.preview_token,
        ),
    )


async def test_owner_settlement_preserves_unknowns_and_replay_is_idempotent(
    session_factory, tmp_path
):
    factory, identity, operation_id = await interrupted_case(session_factory, tmp_path)
    service, actor, request = await owner_preview(factory, identity)
    kwargs = {
        "run_id": identity.run_id,
        "task_id": identity.task_id,
        "attempt_id": identity.attempt_id,
        "actor": actor,
        "request": request,
        "idempotency_key": "recover-interrupted",
    }
    receipt = await service.apply(**kwargs)
    assert receipt.status == "applied"
    assert await service.apply(**kwargs) == receipt
    async with factory() as work:
        run = await work.session.get(Run, identity.run_id)
        assert run.state == "IMPLEMENTING"
        operation = await work.session.get(OperationIntent, operation_id)
        assert operation.status == "FAILED"
        assert operation.outcome_payload is None
        assert operation.outcome_schema_version is None
        assert "unknown" in operation.last_error
        call = await work.session.get(ToolCall, operation_id)
        assert call.status == "CANCELLED"
        assert call.result_metadata["recovery"]["check_outcome"] == "unknown"
        assert call.result_metadata["recovery"]["stop_confirmed"] is False
        assert "exit_code" not in call.result_metadata["recovery"]
        assert "duration_ms" not in call.result_metadata
        attempt = await work.session.get(SubscriptionAttempt, identity.attempt_id)
        assert attempt.status == "terminal" and attempt.telemetry_payload is None
        assert await work.session.get(SubscriptionAttemptResult, identity.attempt_id) is None
        usage = await work.session.get(SubscriptionAttemptConsumption, identity.attempt_id)
        assert usage.uncertain and usage.telemetry_payload is None
        assert usage.unknown_fields and usage.charged["provider_attempts"] == 1
        scheduled = await work.session.get(SubscriptionScheduledTask, identity.task_id)
        assert scheduled.state == "queued" and scheduled.lease_owner is None
        assert await work.session.get(SubscriptionRepairDebit, identity.attempt_id) is not None
        launch = await work.session.scalar(
            select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == identity.attempt_id
            )
        )
        assert launch.pid == 12345 and launch.process_start_token == "old-process"
        assert launch.terminal_payload["stop_confirmed"] is False
        assert launch.terminal_payload["owner_acknowledged_stopped"] is True
        assert not launches_confirmed(
            [launch],
            None,
            require_decision=False,
            worker_identity=launch.worker_identity,
        )
        callback = await work.session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == operation_id
            )
        )
        assert callback.receipt_payload["accepted"] is False
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(SubscriptionRecoveryReceipt)
                .where(SubscriptionRecoveryReceipt.attempt_id == identity.attempt_id)
            )
            == 1
        )
        assert await work.session.scalar(select(unresolved_work(identity.run_id))) is False
        assert (
            await work.session.scalar(select(startup_intervention_hold(identity.run_id))) is False
        )

    fresh = await SubscriptionDecisionExecutor(factory).admit_next("fresh-owner", _reservation())
    assert fresh is not None
    assert fresh.attempt.attempt_id != identity.attempt_id
    async with factory() as work:
        old_attempt = await work.session.get(SubscriptionAttempt, identity.attempt_id)
        fresh_attempt = await work.session.get(SubscriptionAttempt, fresh.attempt.attempt_id)
        assert fresh_attempt.lease_generation > old_attempt.lease_generation


@pytest.mark.parametrize("limit", ["budget", "scheduler"])
async def test_exhausted_budget_preserves_actionable_intervention(session_factory, tmp_path, limit):
    factory, identity, _ = await interrupted_case(
        session_factory,
        tmp_path,
        max_repairs=0 if limit == "budget" else 3,
    )
    if limit == "scheduler":
        async with factory() as work:
            scheduled = await work.session.get(SubscriptionScheduledTask, identity.task_id)
            scheduled.max_repairs = 0
            await work.commit()
    service, actor, request = await owner_preview(factory, identity)
    receipt = await service.apply(
        run_id=identity.run_id,
        task_id=identity.task_id,
        attempt_id=identity.attempt_id,
        actor=actor,
        request=request,
        idempotency_key="budget-blocked",
    )
    assert receipt.reason_code == "interruption_acknowledged_budget_blocked"
    async with factory() as work:
        run = await work.session.get(Run, identity.run_id)
        assert run.state == "AWAITING_HUMAN_INTERVENTION"
        scheduled = await work.session.get(SubscriptionScheduledTask, identity.task_id)
        assert scheduled.state == "blocked" and scheduled.lease_owner is None
        assert await work.session.get(SubscriptionRepairDebit, identity.attempt_id) is None
        usage = await work.session.get(SubscriptionAttemptConsumption, identity.attempt_id)
        assert usage.uncertain


@pytest.mark.parametrize(
    "change",
    [
        "run_version",
        "operation",
        "live_lease",
        "live_operation",
        "uncertain_lease",
        "task_pause",
        "task_cancel",
        "run_pause",
    ],
)
async def test_changed_or_live_source_cannot_apply_preview(session_factory, tmp_path, change):
    factory, identity, operation_id = await interrupted_case(session_factory, tmp_path)
    service, actor, request = await owner_preview(factory, identity)
    async with factory() as work:
        run = await work.session.get(Run, identity.run_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, identity.task_id)
        operation = await work.session.get(OperationIntent, operation_id)
        task = await work.session.get(SubscriptionTask, identity.task_id)
        if change == "run_version":
            run.version += 1
        elif change == "operation":
            operation.request_payload = {**operation.request_payload, "head_sha": "c" * 40}
            operation.request_digest = canonical_digest(operation.request_payload)
        elif change == "live_lease":
            scheduled.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        elif change == "uncertain_lease":
            scheduled.lease_expires_at = None
        elif change == "live_operation":
            operation.execution_owner = "new-owner"
            operation.execution_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        elif change == "task_pause":
            task.pause_requested = True
        elif change == "task_cancel":
            task.cancel_requested = True
        else:
            await work.runs.pause(run.id, run.version, "run.paused", {})
        await work.commit()
    with pytest.raises(RecoveryConflict, match="source differs"):
        await service.apply(
            run_id=identity.run_id,
            task_id=identity.task_id,
            attempt_id=identity.attempt_id,
            actor=actor,
            request=request,
            idempotency_key="stale",
        )
    async with factory() as work:
        assert (
            await work.session.get(OperationIntent, operation_id)
        ).status == "NEEDS_RECONCILIATION"
        assert await work.session.get(SubscriptionAttemptConsumption, identity.attempt_id) is None
