"""Accepted controls fence stage finalization before they can become stale."""

from __future__ import annotations

import pytest
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.planning import PlanningRecoveryRequired
from forge.application.services.review import ReviewRecoveryRequired
from forge.persistence.models import (
    AgentExecution,
    ArtifactLineage,
    EvidenceSet,
    OperationIntent,
    Run,
    Step,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_review import _review_case
from test_delivery_validation import _case
from test_planning_failed_usage import _build_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _enqueue_pending_stop(command, factory, command_type: str, *, lease: bool = True) -> None:
    commands = PostgresCommandRepository(factory)
    control = await commands.enqueue(
        run_id=command.run_id,
        command_type=command_type,
        idempotency_key=f"{command.run_id}:pending-finalization:{command_type}",
        payload={},
        expected_run_version=command.expected_run_version,
        actor_id=command.actor_id,
    )
    if not lease:
        return
    # Lease it to cover both actionable command states.  The control handler is
    # intentionally not invoked: finalization must leave its exact version valid.
    leased = await commands.claim_next(
        worker_id="pending-control", lease_seconds=60, lane=CommandLane.CONTROL
    )
    assert leased is not None and leased.id == control.id


@pytest.mark.parametrize("command_type", ["pause", "cancel"])
async def test_pending_control_fences_review_publication(
    tmp_path, workflow_session_factory, command_type
):
    case, command, service, gateway, _git, _decision = await _review_case(
        tmp_path, workflow_session_factory
    )
    original = gateway.execute

    async def controlled(request):
        result = await original(request)
        await _enqueue_pending_stop(command, workflow_session_factory, command_type)
        return result

    gateway.execute = controlled
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewRecoveryRequired, match="fenced by operator control"):
            await service.execute(command, work)

    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        execution = await session.get(AgentExecution, gateway.requests[0].execution_id)
        evidence = list(
            await session.scalars(
                select(EvidenceSet).where(
                    EvidenceSet.run_id == case.run_id, EvidenceSet.kind == "review"
                )
            )
        )
    assert run is not None and run.version == command.expected_run_version
    assert execution is not None and execution.status == "RUNNING"
    assert evidence == []


async def test_pending_control_fences_planning_finalization_and_retains_known_result(
    tmp_path, workflow_session_factory
):
    case = await _build_case(tmp_path, workflow_session_factory, fail_invalid=False)
    original = case.gateway.execute

    async def controlled(request):
        result = await original(request)
        await PostgresCommandRepository(workflow_session_factory).enqueue(
            run_id=case.run_id,
            command_type="pause",
            idempotency_key=f"{case.run_id}:pending-plan-finalization",
            payload={},
            expected_run_version=1,
            actor_id=request.run_id,
        )
        return result

    case.gateway.execute = controlled
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(PlanningRecoveryRequired):
            await case.service.execute(case.command, work)

    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        producers = set(
            await session.scalars(
                select(ArtifactLineage.producer_kind).where(ArtifactLineage.run_id == case.run_id)
            )
        )
    assert run is not None and run.version == 1 and run.state == "PLANNING"
    assert {"planning_late_usage", "planning_late_result"} <= producers


@pytest.mark.parametrize("command_type", ["pause", "cancel"])
async def test_pending_control_fences_validation_publication_and_retains_receipt(
    tmp_path, workflow_session_factory, command_type
):
    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    original = runner.run_terminal

    async def controlled_check(request):
        result = await original(request)
        await _enqueue_pending_stop(command, workflow_session_factory, command_type, lease=False)
        return result

    runner.run_terminal = controlled_check
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired, match="fenced by operator control"):
            await service.execute(command, work)

    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        step = await session.scalar(
            select(Step).where(Step.run_id == case.run_id, Step.kind == "validate")
        )
        receipt = await session.scalar(
            select(OperationIntent).where(
                OperationIntent.run_id == case.run_id,
                OperationIntent.operation_kind == "controller_named_check",
            )
        )
        evidence = list(
            await session.scalars(
                select(EvidenceSet).where(
                    EvidenceSet.run_id == case.run_id, EvidenceSet.kind == "validation"
                )
            )
        )
    assert run is not None and run.version == command.expected_run_version
    assert runner.calls == ["unit"]
    assert step is not None and step.status == "RUNNING"
    assert receipt is not None and receipt.status == "SUCCEEDED"
    assert receipt.outcome_payload["command_result_digest"]
    assert evidence == []
