"""Owner interruption recovery remains compatible with ordinary task controls."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.domain.operation import canonical_digest
from forge.domain.subscription import AttemptTelemetry, encode_subscription_record
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.subscription_recovery import RecoveryAction
from forge.domain.subscription_task_controls import SubscriptionTaskControlRequest
from forge.domain.tool import ToolName
from forge.persistence.models import Run
from forge.persistence.models.execution import OperationIntent, ToolCall
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.subscription_recovery import (
    PostgresSubscriptionRecoveryRepository,
)
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_interrupted_check_recovery import interrupted_case, owner_preview
from test_subscription_usage import _known, _reservation


@pytest.mark.parametrize("later_attempt", [False, True])
async def test_recovered_attempt_supports_idle_pause_resume_and_preserves_real_proof(
    session_factory,
    tmp_path,
    later_attempt,
):
    factory, identity, _ = await interrupted_case(session_factory, tmp_path, specialist=True)
    launch_id = str(uuid4())
    terminal = {
        "schema_version": 1,
        "launch_id": launch_id,
        "pid": 24680,
        "process_identity": "proved-process",
        "outcome": "exited",
        "return_code": 0,
        "stop_confirmed": True,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    async with factory() as work:
        attempt = await work.session.get(SubscriptionAttempt, identity.attempt_id)
        work.session.add(
            SubscriptionClientLaunch(
                attempt_id=identity.attempt_id,
                launch_id=launch_id,
                worker_identity=attempt.lease_owner,
                state="terminal",
                pid=24680,
                process_start_token="proved-process",
                terminal_payload=terminal,
            )
        )
        await work.commit()
    service, actor, request = await owner_preview(factory, identity)
    await service.apply(
        run_id=identity.run_id,
        task_id=identity.task_id,
        attempt_id=identity.attempt_id,
        actor=actor,
        request=request,
        idempotency_key="recover-for-controls",
    )
    if later_attempt:
        executor = SubscriptionDecisionExecutor(factory)
        fresh = await executor.admit_next("after-owner-recovery", _reservation())
        assert fresh is not None and fresh.task.task_id == identity.task_id
        proof = SubscriptionLaunchTerminalProof.model_validate(
            {
                **terminal,
                "launch_id": str(uuid4()),
                "pid": 24681,
                "process_identity": "fresh-process",
            }
        )
        async with factory() as work:
            await work.subscription.launch_intent(
                fresh.attempt.attempt_id,
                proof.launch_id,
                worker_identity=fresh.lease.owner,
            )
            await work.subscription.launch_started(
                fresh.attempt.attempt_id,
                proof.launch_id,
                worker_identity=fresh.lease.owner,
                pid=proof.pid,
                process_start_token=proof.process_identity,
            )
            await work.subscription.launch_finished(
                fresh.attempt.attempt_id,
                proof.launch_id,
                worker_identity=fresh.lease.owner,
                terminal=proof,
                uncertain=False,
            )
            await work.commit()
        settled = await executor.settle(
            fresh,
            SubscriptionInvocationResult(
                attempt=fresh.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                telemetry=_known(),
                launch_proof=proof,
            ),
        )
        assert settled.disposition == "repair_queued"
    async with factory() as work:
        run = await work.session.get(Run, identity.run_id)
        task = await work.session.get(SubscriptionTask, identity.task_id)
        run_version, task_version = run.version, task.version
    controls = SubscriptionTaskControlService(factory)
    paused = await controls.control(
        run_id=identity.run_id,
        task_id=identity.task_id,
        actor=actor,
        idempotency_key="pause-after-recovery",
        request=SubscriptionTaskControlRequest(
            action="pause",
            expected_run_version=run_version,
            expected_task_version=task_version,
            reason="Inspect retained work",
        ),
    )
    assert paused.status == "paused"
    resumed = await SubscriptionTaskControlService(factory).control(
        run_id=identity.run_id,
        task_id=identity.task_id,
        actor=actor,
        idempotency_key="resume-after-recovery",
        request=SubscriptionTaskControlRequest(
            action="resume",
            expected_run_version=run_version,
            expected_task_version=paused.task_version,
            reason="Continue retained work",
            pause_receipt_id=paused.receipt_id,
        ),
    )
    assert resumed.status == "queued"
    async with factory() as work:
        launch = await work.session.scalar(
            select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == identity.attempt_id,
                SubscriptionClientLaunch.launch_id == launch_id,
            )
        )
        assert launch.terminal_payload == terminal


async def test_recovery_retains_reported_usage_without_filling_unknown_fields(
    session_factory, tmp_path
):
    factory, identity, _ = await interrupted_case(session_factory, tmp_path)
    telemetry = encode_subscription_record(
        AttemptTelemetry(input_tokens=80, output_tokens=12, duration_ms=100)
    )
    async with factory() as work:
        attempt = await work.session.get(SubscriptionAttempt, identity.attempt_id)
        attempt.telemetry_payload = telemetry
        await work.commit()
    service, actor, request = await owner_preview(factory, identity)
    await service.apply(
        run_id=identity.run_id,
        task_id=identity.task_id,
        attempt_id=identity.attempt_id,
        actor=actor,
        request=request,
        idempotency_key="partial-usage",
    )
    async with factory() as work:
        usage = await work.session.get(SubscriptionAttemptConsumption, identity.attempt_id)
        assert usage.telemetry_payload == telemetry
        assert usage.observed["input_tokens"] == 80
        assert usage.observed["output_tokens"] == 12
        assert usage.uncertain and usage.unknown_fields


@pytest.mark.parametrize("kind", ["operation", "tool", "binding"])
async def test_recovery_refuses_other_unresolved_work(session_factory, tmp_path, kind):
    factory, identity, _ = await interrupted_case(session_factory, tmp_path)
    other_id = uuid4()
    async with factory() as work:
        if kind == "operation":
            payload = {"interrupted": "other-operation"}
            work.session.add(
                OperationIntent(
                    id=other_id,
                    run_id=identity.run_id,
                    operation_kind="named_check",
                    idempotency_key=f"other:{other_id}",
                    request_payload=payload,
                    request_digest=canonical_digest(payload),
                    status="PENDING",
                )
            )
        elif kind == "tool":
            work.session.add(
                ToolCall(
                    id=other_id,
                    run_id=identity.run_id,
                    tool_name=ToolName.BUILD_RUN_NAMED_CHECK.value,
                    subscription_task_id=identity.task_id,
                    subscription_attempt_id=identity.attempt_id,
                    subscription_purpose="primary",
                    arguments_schema_version=1,
                    normalized_arguments={"command_name": "unit"},
                    authorized=True,
                    status="RUNNING",
                    started_at=datetime.now(UTC),
                )
            )
        else:
            work.session.add(
                SubscriptionOperationBinding(
                    attempt_id=identity.attempt_id,
                    provider_call_key="another-call",
                    durable_operation_id=other_id,
                    payload={"unresolved": True},
                )
            )
        await work.commit()
    async with factory() as work:
        preview = await PostgresSubscriptionRecoveryRepository(work.session).preview(
            identity.run_id,
            identity.task_id,
            identity.attempt_id,
            RecoveryAction.ACKNOWLEDGE_INTERRUPTED_CHECK,
        )
    assert not preview.eligible
    assert preview.reason_code == "other_work_unresolved"


async def test_preview_reports_invalid_saved_telemetry_before_owner_action(
    session_factory, tmp_path
):
    factory, identity, _ = await interrupted_case(session_factory, tmp_path)
    async with factory() as work:
        attempt = await work.session.get(SubscriptionAttempt, identity.attempt_id)
        attempt.telemetry_payload = {"schema_version": 999, "kind": "unknown"}
        await work.commit()
    async with factory() as work:
        preview = await PostgresSubscriptionRecoveryRepository(work.session).preview(
            identity.run_id,
            identity.task_id,
            identity.attempt_id,
            RecoveryAction.ACKNOWLEDGE_INTERRUPTED_CHECK,
        )
    assert not preview.eligible
    assert preview.reason_code == "telemetry_unresolved"
