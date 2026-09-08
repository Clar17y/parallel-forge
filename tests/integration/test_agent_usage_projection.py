"""Agent cards report the selected execution's usage rather than the whole run."""

from uuid import uuid4

import pytest
from forge.api.schemas.projections import RunProjection
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.projections import ProjectionService
from forge.persistence.queries.dashboard import DashboardQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_development import _service_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_agent_usage_is_bound_to_the_latest_execution(tmp_path, workflow_session_factory):
    _case, command, service, gateway, _git = await _service_case(tmp_path, workflow_session_factory)
    query = ProjectionService(DashboardQuery(workflow_session_factory))
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    before = await query.run_projection(command.run_id, actor)
    assert before["agents"]["developer"]["usage"] is None
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
    result = RunProjection.model_validate(await query.run_projection(command.run_id, actor))
    developer = result.agents["developer"]
    assert developer.execution_id == gateway.requests[0].execution_id
    assert developer.usage.input_tokens == 3
    assert developer.usage.output_tokens == 2
    assert developer.usage.model_calls == 1
    assert developer.usage.currencies[0].known_cost_minor == 1
    assert result.usage.input_tokens > developer.usage.input_tokens
