"""Accepted operator stops prevent another controller check from starting."""

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_validation import _case
from test_pending_control_finalization import _enqueue_pending_stop
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("control_kind", ["pause", "cancel"])
async def test_pending_stop_prevents_controller_check_dispatch(
    tmp_path, workflow_session_factory, control_kind
):
    _case_data, command, service, runner = await _case(tmp_path, workflow_session_factory)
    await _enqueue_pending_stop(command, workflow_session_factory, control_kind, lease=False)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert runner.calls == []
