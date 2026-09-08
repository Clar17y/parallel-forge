"""A paused Planner resumes through the worker with a fresh, bounded execution."""

import json
from uuid import uuid4

import pytest
from forge.application.handlers.planning import PlanningHandler
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandSuspended
from forge.application.services.worker import Worker
from forge.domain.run import RunState
from forge.persistence.models import AgentExecution
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_planning_failed_usage import _build_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("pause_count, fail_invalid", [(1, False), (2, False), (1, True)])
async def test_worker_resumes_planning_with_fresh_execution(
    tmp_path, workflow_session_factory, pause_count, fail_invalid
):
    case = await _build_case(tmp_path, workflow_session_factory, fail_invalid=fail_invalid)
    commands = PostgresCommandRepository(workflow_session_factory)
    original = case.gateway.execute

    async def pause_first_result(request):
        try:
            return await original(request)
        finally:
            if len(case.gateway.requests) <= pause_count:
                async with PostgresUnitOfWork(workflow_session_factory) as work:
                    current = await work.runs.get(case.run_id)
                await commands.enqueue(
                    run_id=case.run_id,
                    command_type="pause",
                    idempotency_key=f"pause-{len(case.gateway.requests)}",
                    payload={},
                    expected_run_version=current.version,
                    actor_id=uuid4(),
                )
                pause = await commands.claim_next(
                    worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
                )
                assert pause is not None
                async with PostgresUnitOfWork(workflow_session_factory) as work:
                    await PauseRunHandler()(pause, work)
                await commands.complete(pause.id, worker_id="control")

    case.gateway.execute = pause_first_result
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandSuspended):
            await case.service.execute(case.command, work)
    await commands.complete(case.command.id, worker_id="test-worker")

    async def resume_and_replay(command, work):
        handler = ResumeRunHandler()
        await handler(command, work)
        # Simulate a delivery retried after commit but before its queue ACK.
        await handler(command, work)
        events = await work.events.list_for_version(
            command.run_id, command.expected_run_version + 1
        )
        assert sum(event.event_type == "run.resumed" for event in events) == 1

    worker = Worker(
        commands,
        workflow_session_factory,
        handlers={"resume": resume_and_replay, "start_planning": PlanningHandler(case.service)},
        worker_id="resuming",
    )
    for index in range(pause_count):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            paused = await work.runs.get(case.run_id)
        assert paused.state is RunState.PAUSED
        await commands.enqueue(
            run_id=case.run_id,
            command_type="resume",
            idempotency_key=f"resume-{index}",
            payload={},
            expected_run_version=paused.version,
            actor_id=uuid4(),
        )
        assert await worker.tick() is True
        assert await worker.tick() is True
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        run = await work.runs.get(case.run_id)
    assert run.state is (
        RunState.AWAITING_HUMAN_INTERVENTION if fail_invalid else RunState.AWAITING_PLAN_APPROVAL
    )
    assert run.local_remediation_count == 0
    assert len(case.gateway.requests) == pause_count + 1
    assert len({request.execution_id for request in case.gateway.requests}) == pause_count + 1
    async with workflow_session_factory() as session:
        executions = list(
            await session.scalars(
                select(AgentExecution).where(AgentExecution.run_id == case.run_id)
            )
        )
    assert sorted(row.status for row in executions) == ["CANCELLED"] * pause_count + [
        "FAILED" if fail_invalid else "SUCCEEDED"
    ]
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        for request in case.gateway.requests[:-1]:
            receipts = await work.artifacts.get_by_producer(
                run_id=case.run_id,
                producer_type="planning_late_result",
                producer_id=request.execution_id,
            )
            assert len(receipts) == 1
            receipt = json.loads(await case.artifact_store.open_bytes(receipts[0].digest))
            assert receipt["execution_id"] == str(request.execution_id)
            original_output = json.loads(
                await case.artifact_store.open_bytes(receipt["output_digest"])
            )
            assert receipt["output"] == original_output
