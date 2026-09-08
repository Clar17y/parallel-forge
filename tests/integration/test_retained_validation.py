"""Resume checks after known controller receipts were fenced by a queued pause."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.domain.command import CommandStatus
from forge.persistence.models import RunCommand, Step
from forge.persistence.models import RunEvent as RunEventRecord
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select, update
from test_delivery_validation import _case
from test_pending_control_finalization import _enqueue_pending_stop
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("obstruction", [None, "pending_operation", "admission_drift"])
async def test_resume_partial_validation_retains_receipt_and_reruns_required_checks(
    tmp_path, workflow_session_factory, obstruction
):
    case, source, service, runner = await _case(tmp_path, workflow_session_factory)
    original = runner.run_terminal

    async def pause_after_first_check(request):
        result = await original(request)
        await _enqueue_pending_stop(source, workflow_session_factory, "pause", lease=False)
        return result

    runner.run_terminal = pause_after_first_check
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired, match="fenced by operator control"):
            await service.execute(source, work)
    runner.run_terminal = original
    assert runner.calls == ["unit"]
    commands = PostgresCommandRepository(workflow_session_factory)
    pause = await commands.claim_next(worker_id="pause", lease_seconds=60, lane=CommandLane.CONTROL)
    assert pause is not None
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await PauseRunHandler()(pause, work)
        paused = await work.runs.get(source.run_id)
    await commands.complete(pause.id, worker_id="pause")
    async with workflow_session_factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await commands.enqueue(
        run_id=source.run_id,
        command_type="resume",
        idempotency_key=f"{source.run_id}:retained-check-resume",
        payload={},
        expected_run_version=paused.version,
        actor_id=uuid4(),
    )
    resume = await commands.claim_next(worker_id="resume", lease_seconds=60)
    assert resume is not None and resume.command_type == "resume"
    if obstruction is not None:
        if obstruction == "pending_operation":
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                await work.operations.begin(
                    run_id=source.run_id,
                    operation_type="repository_write",
                    idempotency_key="validation-unresolved-operation",
                    request_digest="a" * 64,
                    request_payload={},
                )
                await work.commit()
        else:
            async with workflow_session_factory() as session, session.begin():
                event = await session.scalar(
                    select(RunEventRecord).where(
                        RunEventRecord.run_id == source.run_id,
                        RunEventRecord.event_type == "run.validation_started",
                    )
                )
                assert event is not None
                event.payload = {**event.payload, "command_id": str(uuid4())}
        with pytest.raises(CommandRecoveryRequired):
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
        assert (await commands.get(source.id)).status is CommandStatus.LEASED
        assert runner.calls == ["unit"]
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            assert await work.runs.get(source.run_id) == paused
        async with workflow_session_factory() as session:
            step = await session.scalar(
                select(Step).where(Step.run_id == source.run_id, Step.kind == "validate")
            )
            assert step is not None and step.status == "RUNNING"
        return
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
    assert (await commands.get(source.id)).status is CommandStatus.CANCELLED
    assert runner.calls == ["unit"]
    await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="fresh", lease_seconds=60)
    assert fresh is not None and fresh.command_type == "validate"
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        evidence = await service.execute(fresh, work)
    assert evidence is not None
    assert runner.calls == ["unit", "unit", "lint"]
    async with workflow_session_factory() as session:
        steps = list(
            await session.scalars(
                select(Step).where(Step.run_id == source.run_id, Step.kind == "validate")
            )
        )
    assert sorted(step.status for step in steps) == ["CANCELLED", "SUCCEEDED"]
