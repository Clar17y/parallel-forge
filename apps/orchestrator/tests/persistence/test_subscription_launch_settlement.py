"""Successful decisions require a correlated durable stopped client launch."""

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.domain.subscription import HandoffStatus, TaskHandoff
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_execution_constraints import _admitted
from test_subscription_usage import _known


@pytest.mark.integration
@pytest.mark.parametrize("control", ["run_pause", "task_cancel", "expired_lease"])
async def test_new_launch_refuses_stopped_admission(session_factory, persisted_run, control):
    from datetime import UTC, datetime, timedelta

    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from forge.persistence.models.subscription import SubscriptionTask
    from forge.persistence.repositories.subscription import SubscriptionConflict
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from sqlalchemy import select

    _, admission = await _admitted(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        if control == "run_pause":
            run = await work.runs.get(admission.task.run_id)
            await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        elif control == "task_cancel":
            task = await work.session.get(SubscriptionTask, admission.task.task_id)
            task.cancel_requested = True
        else:
            scheduled = await work.session.scalar(
                select(SubscriptionScheduledTask).where(
                    SubscriptionScheduledTask.task_id == admission.task.task_id
                )
            )
            scheduled.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionConflict, match="launch"):
            await work.subscription.launch_intent(
                admission.attempt.attempt_id, "new-launch", worker_identity=admission.lease.owner
            )
        await work.rollback()


@pytest.mark.integration
async def test_existing_launch_can_record_stop_after_run_pause(session_factory, persisted_run):
    from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    _, admission = await _admitted(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.launch_intent(
            admission.attempt.attempt_id,
            "started-before-pause",
            worker_identity=admission.lease.owner,
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get(admission.task.run_id)
        await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        await work.commit()
    proof = SubscriptionLaunchTerminalProof(
        launch_id="started-before-pause",
        pid=12345,
        process_identity="synthetic-start",
        outcome="cancelled",
        return_code=1,
        stop_confirmed=True,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.launch_started(
            admission.attempt.attempt_id,
            proof.launch_id,
            worker_identity=admission.lease.owner,
            pid=proof.pid,
            process_start_token=proof.process_identity,
        )
        await work.subscription.launch_finished(
            admission.attempt.attempt_id,
            proof.launch_id,
            worker_identity=admission.lease.owner,
            terminal=proof,
            uncertain=False,
        )
        await work.commit()


@pytest.mark.integration
async def test_success_without_client_launch_is_fenced(session_factory, persisted_run):
    executor, admission = await _admitted(session_factory, persisted_run)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        decision=TaskHandoff(
            run_id=admission.task.run_id,
            task_id=admission.task.task_id,
            attempt_id=admission.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest="a" * 64,
            evidence_receipt_ids=("unverified",),
            summary="Claimed completion without launch",
        ),
        telemetry=_known(),
    )
    settled = await executor.settle(admission, result)
    assert settled.disposition == "fenced" and not settled.accepted
    assert (await executor.settle(admission, result)).replayed


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "result_pid",
        "result_launch",
        "missing_started",
        "legacy_terminal",
        "uncertain",
        "missing_result_proof",
        "foreign_worker",
        "cancelled",
    ],
)
async def test_decision_requires_exact_terminal_launch(session_factory, persisted_run, mutation):
    from dataclasses import replace

    from forge.persistence.models.subscription import SubscriptionClientLaunch
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from sqlalchemy import select
    from subscription_launch_fixture import record_stopped_launch

    executor, admission = await _admitted(session_factory, persisted_run)
    proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        decision=TaskHandoff(
            run_id=admission.task.run_id,
            task_id=admission.task.task_id,
            attempt_id=admission.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest="a" * 64,
            evidence_receipt_ids=("unverified",),
        ),
        telemetry=_known(),
        launch_proof=proof,
    )
    if mutation == "result_pid":
        result = replace(result, launch_proof=proof.model_copy(update={"pid": proof.pid + 1}))
    elif mutation == "result_launch":
        result = replace(
            result, launch_proof=proof.model_copy(update={"launch_id": "other-launch"})
        )
    elif mutation == "missing_result_proof":
        result = replace(result, launch_proof=None)
    elif mutation != "none":
        async with PostgresUnitOfWork(session_factory) as work:
            row = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == admission.attempt.attempt_id
                )
            )
            if mutation == "missing_started":
                row.pid = None
            elif mutation == "legacy_terminal":
                row.terminal_payload = {"stop_confirmed": True}
            elif mutation == "uncertain":
                row.state = "uncertain"
            elif mutation == "foreign_worker":
                row.worker_identity = "other-worker"
            elif mutation == "cancelled":
                proof = proof.model_copy(update={"outcome": "cancelled"})
                row.terminal_payload = proof.model_dump(mode="json")
                result = replace(result, launch_proof=proof)
            await work.commit()
    settled = await executor.settle(admission, result)
    assert settled.disposition == ("decision_pending" if mutation == "none" else "fenced")
    assert not settled.accepted
    assert (await executor.settle(admission, result)).replayed
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(admission.task.run_id)
        assert usage.consumed.provider_attempts == 1


@pytest.mark.integration
async def test_supervisor_receipt_is_durable_before_decision_settlement(
    session_factory, persisted_run, tmp_path
):
    import sys

    from forge.agents.client_process import (
        ClientLaunchSpec,
        ClientProcessSupervisor,
        terminal_launch_proof,
    )
    from forge.persistence.models.subscription import SubscriptionClientLaunch
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from forge.worker.subscription_broker import DurableClientProcessLifecycle
    from sqlalchemy import select

    executor, admission = await _admitted(session_factory, persisted_run)
    lifecycle = DurableClientProcessLifecycle(
        lambda: PostgresUnitOfWork(session_factory),
        attempt_id=admission.attempt.attempt_id,
        worker_identity=admission.lease.owner,
    )
    spec = ClientLaunchSpec(
        argv=(
            sys.executable,
            "-u",
            "-c",
            "import time; print('{\"done\":true}', flush=True); time.sleep(30)",
        ),
        cwd=str(tmp_path),
        environment={},
        allowed_environment=frozenset(),
        duration_seconds=10,
    )
    session = await ClientProcessSupervisor().start(spec, lifecycle=lifecycle)
    try:
        assert await session.receive() == {"done": True}
        stopped = await session.close(completed=True)
    finally:
        await session.close()
    proof = terminal_launch_proof(stopped)
    assert proof.permits_decision and proof.outcome == "completed"
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.scalar(
            select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == admission.attempt.attempt_id
            )
        )
        assert row.terminal_payload == proof.model_dump(mode="json")
        assert (row.pid, row.process_start_token) == (proof.pid, proof.process_identity)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        launch_proof=proof,
        telemetry=_known(),
        decision=TaskHandoff(
            run_id=admission.task.run_id,
            task_id=admission.task.task_id,
            attempt_id=admission.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest="a" * 64,
            evidence_receipt_ids=("unverified",),
        ),
    )
    assert (await executor.settle(admission, result)).disposition == "decision_pending"
    from forge.persistence.repositories.subscription import SubscriptionConflict

    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionConflict, match="running attempt"):
            await work.subscription.launch_intent(
                admission.attempt.attempt_id,
                "late-new-launch",
                worker_identity=admission.lease.owner,
            )


@pytest.mark.integration
@pytest.mark.parametrize("version", [2, 3])
async def test_older_result_shapes_replay_without_new_usage(
    session_factory, persisted_run, version
):
    import hashlib
    import json

    from forge.application.ports.subscription_gateway import SubscriptionFailure
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    executor, admission = await _admitted(session_factory, persisted_run)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
    )
    await executor.settle(admission, result)
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        legacy = dict(row.result_payload)
        legacy["schema_version"] = version
        legacy.pop("launch_proof")
        if version == 2:
            legacy.pop("proposal_context")
        row.result_payload = legacy
        row.result_digest = hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        await work.commit()
    assert (await executor.settle(admission, result)).replayed
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(admission.task.run_id)
        assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 1
