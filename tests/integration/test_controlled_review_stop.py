"""A proven control stop settles a completed reviewer without publication."""

from __future__ import annotations

import pytest
from forge.application.handlers.run_controls import CancelRunHandler, PauseRunHandler
from forge.application.ports.commands import CommandLane, CommandSuspended
from forge.domain.run import RunState
from forge.persistence.models import AgentExecution, EvidenceSet, Run
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_review import _review_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize(
    ("command_type", "handler", "target"),
    (("pause", PauseRunHandler, RunState.PAUSED), ("cancel", CancelRunHandler, RunState.CANCELLED)),
)
async def test_worker_control_stop_settles_completed_review_without_evidence(
    tmp_path, workflow_session_factory, command_type, handler, target
):
    case, command, service, gateway, _git, _decision = await _review_case(
        tmp_path, workflow_session_factory
    )
    original = gateway.execute

    async def paused(request):
        result = await original(request)
        repository = PostgresCommandRepository(workflow_session_factory)
        control = await repository.enqueue(
            run_id=command.run_id,
            command_type=command_type,
            idempotency_key=f"review-control-{command_type}",
            payload={},
            expected_run_version=command.expected_run_version,
            actor_id=command.actor_id,
        )
        leased = await repository.claim_next(
            worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
        )
        assert leased is not None and leased.id == control.id
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await handler()(leased, work)
        await repository.complete(leased.id, worker_id="control")
        return result

    gateway.execute = paused
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandSuspended):
            await service.execute(command, work)
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        execution = await session.get(AgentExecution, gateway.requests[0].execution_id)
        evidence = await session.scalars(
            select(EvidenceSet).where(
                EvidenceSet.run_id == case.run_id, EvidenceSet.kind == "review"
            )
        )
    assert run is not None and run.state == target.value
    assert execution is not None and execution.status == "CANCELLED"
    assert list(evidence) == []
