"""An accepted stop request wins before the next controlled tool dispatch."""

from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandLane
from forge.domain.actor import AgentRole
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork


@pytest.mark.integration
@pytest.mark.parametrize(
    "request_kind,leased,stale,allowed",
    [
        (None, False, False, True),
        ("pause", False, False, False),
        ("pause", True, False, False),
        ("cancel", False, False, False),
        ("pause", False, True, True),
    ],
)
async def test_pending_control_fences_tool_admission(
    persisted_run, session_factory, request_kind, leased, stale, allowed
) -> None:
    step_id, execution_id = uuid4(), uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.transition(
            persisted_run.id, persisted_run.version, RunState.PLANNING, "test.planning", {}
        )
        await work.executions.admit(
            persisted_run.id,
            step_id,
            execution_id,
            "plan",
            1,
            AgentRole.PLANNER,
            "1",
            "test",
            "fixture",
            transition_from="CREATED",
            transition_to="PLANNING",
        )
        run = await work.runs.get(persisted_run.id)
        await work.commit()
    if request_kind is not None:
        commands = PostgresCommandRepository(session_factory)
        await commands.enqueue(
            run_id=run.id,
            command_type=request_kind,
            idempotency_key="accepted-control",
            payload={},
            expected_run_version=run.version - int(stale),
            actor_id=uuid4(),
        )
        if leased:
            assert (
                await commands.claim_next(
                    worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
                )
                is not None
            )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.get_for_update(run.id)
        assert (
            await work.tool_calls.validate_execution_context(
                run.id, execution_id, step_id, AgentRole.PLANNER
            )
            is allowed
        )
