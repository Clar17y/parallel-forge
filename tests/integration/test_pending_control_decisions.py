"""Accepted controls remain authoritative after stage evidence is published."""

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.domain.run import RunState
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_decisions import _decision_case
from test_pending_control_finalization import _enqueue_pending_stop
from test_review_decision import _decide_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("control", ["pause", "cancel"])
@pytest.mark.parametrize("lease", [False, True])
async def test_pending_control_fences_validation_decision(
    tmp_path, workflow_session_factory, control, lease
):
    case, command, delivery, runner = await _decision_case(tmp_path, workflow_session_factory)
    original = delivery._validation.execute

    async def publish_then_control(command, work):
        descriptor = await original(command, work)
        await _enqueue_pending_stop(command, workflow_session_factory, control, lease=lease)
        return descriptor

    delivery._validation.execute = publish_then_control
    with pytest.raises(CommandRecoveryRequired, match="fenced by operator control"):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await delivery.validate(command, work)
    assert runner.calls == ["unit", "lint"]
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.VALIDATING
        assert run.version == command.expected_run_version
        assert await work.commands.get_by_idempotency_key(f"{case.run_id}:review:1") is None
        assert not any(
            event.event_type == "run.validation_decided"
            for event in await work.events.list_after(case.run_id, 0)
        )


@pytest.mark.parametrize("control", ["pause", "cancel"])
@pytest.mark.parametrize("lease", [False, True])
async def test_pending_control_fences_review_decision(
    tmp_path, workflow_session_factory, control, lease
):
    case, command, service, _git, _review = await _decide_case(tmp_path, workflow_session_factory)
    await _enqueue_pending_stop(command, workflow_session_factory, control, lease=lease)
    with pytest.raises(CommandRecoveryRequired, match="fenced by operator control"):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await service.decide(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.REVIEWING
        assert run.version == command.expected_run_version
        assert run.pending_evidence_digest is None
        assert not any(
            event.event_type == "run.review_decided"
            for event in await work.events.list_after(case.run_id, 0)
        )
