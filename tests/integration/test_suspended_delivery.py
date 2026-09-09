"""Stopped delivery receipts are atomic proof for crash-before-ACK recovery."""

from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import CancelRunHandler, PauseRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.suspended_delivery import record_suspended_delivery
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.domain.run import RunState
from forge.observability.usage import UsageRecord
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize(
    "causal,terminal,control_type",
    [(True, True, "pause"), (True, True, "cancel"), (False, True, "pause"), (True, False, "pause")],
)
async def test_suspended_receipt_requires_causal_stop_and_terminal_execution(
    persisted_run, session_factory, causal, terminal, control_type
):
    commands = PostgresCommandRepository(session_factory)
    await commands.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="plan",
        payload={},
        expected_run_version=persisted_run.version,
    )
    command = await commands.claim_next(worker_id="normal", lease_seconds=60)
    assert command is not None
    step_id, execution_id = uuid4(), uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        admitted = await work.runs.transition(
            persisted_run.id, persisted_run.version, RunState.PLANNING, "test.planning", {}
        )
        await work.executions.admit(
            admitted.id, step_id, execution_id, "plan", 1, AgentRole.PLANNER, "1", "test", "fixture"
        )
        await work.commit()
    if causal:
        await commands.enqueue(
            run_id=admitted.id,
            command_type=control_type,
            idempotency_key="pause",
            payload={},
            expected_run_version=admitted.version,
            actor_id=uuid4(),
        )
        pause = await commands.claim_next(
            worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
        )
        assert pause is not None
        async with PostgresUnitOfWork(session_factory) as work:
            handler = PauseRunHandler() if control_type == "pause" else CancelRunHandler()
            await handler(pause, work)
    else:
        async with PostgresUnitOfWork(session_factory) as work:
            await work.runs.pause(admitted.id, admitted.version, "test.paused", {})
            await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        if terminal:
            await work.executions.finalize(
                admitted.id,
                step_id,
                execution_id,
                AgentFinishStatus.CANCELLED,
                UsageRecord(
                    provider="test",
                    model="fixture",
                    prompt_version="1",
                    input_tokens=7,
                    pricing_version="fixture",
                    currency="USD",
                    unknown_price_reason="fixture",
                ),
            )
        if causal and terminal:
            await record_suspended_delivery(work, command, admitted, step_id, "plan", 1)
            await record_suspended_delivery(work, command, admitted, step_id, "plan", 1)
            await work.commit()
        else:
            with pytest.raises(CommandRecoveryRequired):
                await record_suspended_delivery(work, command, admitted, step_id, "plan", 1)
            await work.rollback()
    async with PostgresUnitOfWork(session_factory) as work:
        events = [
            e
            for e in await work.events.list_after(admitted.id, 0)
            if e.event_type == "delivery.suspended"
        ]
        assert len(events) == int(causal and terminal)
        if events:
            assert events[0].payload["command_id"] == str(command.id)
            assert events[0].payload["expected_run_version"] == 0
            assert events[0].payload["admitted_run_version"] == 1
            assert events[0].payload["execution_id"] == str(execution_id)
            assert events[0].payload["step_id"] == str(step_id)
            assert events[0].run_version == 2
