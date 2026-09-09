"""Worker controls settle an admitted check without dispatching another one."""

from uuid import NAMESPACE_URL, uuid5

import pytest
from forge.application.handlers.run_controls import CancelRunHandler, PauseRunHandler
from forge.application.ports.commands import CommandLane, CommandSuspended
from forge.persistence.models import EvidenceSet, OperationIntent, Run, Step
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_validation import _case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("control_kind", ["pause", "cancel"])
async def test_control_after_check_settles_receipt_and_step_without_next_dispatch(
    tmp_path, workflow_session_factory, control_kind
):
    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    original = runner.run_terminal

    async def controlled_check(request):
        result = await original(request)
        commands = PostgresCommandRepository(workflow_session_factory)
        control = await commands.enqueue(
            run_id=case.run_id,
            command_type=control_kind,
            idempotency_key=f"{case.run_id}:test-control:{control_kind}",
            payload={},
            expected_run_version=command.expected_run_version,
            actor_id=command.actor_id,
        )
        leased = await commands.claim_next(
            worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
        )
        assert leased is not None and leased.id == control.id
        handler = PauseRunHandler() if control_kind == "pause" else CancelRunHandler()
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await handler(leased, work)
        # The state transition is durable before the control worker acknowledges it.
        return result

    runner.run_terminal = controlled_check
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandSuspended):
            await service.execute(command, work)
    assert runner.calls == ["unit"]
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        step = await session.get(Step, uuid5(NAMESPACE_URL, f"forge:validate:{command.id}"))
        intents = list(
            await session.scalars(
                select(OperationIntent).where(
                    OperationIntent.run_id == case.run_id,
                    OperationIntent.operation_kind == "controller_named_check",
                )
            )
        )
        evidence = list(
            await session.scalars(
                select(EvidenceSet).where(
                    EvidenceSet.run_id == case.run_id, EvidenceSet.kind == "validation"
                )
            )
        )
    assert run.state == ("PAUSED" if control_kind == "pause" else "CANCELLED")
    assert step.status == "CANCELLED"
    assert len(intents) == 1 and intents[0].status == "SUCCEEDED"
    assert intents[0].outcome_payload["command_result_digest"]
    assert evidence == []
