"""Contract and authentication checks for the run-list route."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient


class FakeQuery:
    async def list(self, **kwargs):
        self.kwargs = kwargs
        return (
            [
                {
                    "task_id": uuid4(),
                    "task_title": "Task",
                    "project_id": uuid4(),
                    "project_name": "Project",
                    "run_id": uuid4(),
                    "state": "CREATED",
                    "version": 2,
                    "pending_gate": None,
                    "next_gate": None,
                    "attention_required": False,
                    "local_remediation_count": 0,
                    "remote_remediation_count": 1,
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "updated_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                    "elapsed_ms": 1000,
                    "elapsed_seconds": 1.0,
                    "pull_request": None,
                    "cost_summary": {"currencies": [], "unpriced_calls": 0},
                }
            ],
            True,
        )


@pytest.fixture
def route_context():
    from apps.orchestrator.tests.api.conftest import FakeRouteAuthService

    auth = FakeRouteAuthService()
    app = create_app(
        Settings(web_origin="http://127.0.0.1:3000"),
        unit_of_work_factory=lambda: object(),
        auth_service=auth,
    )
    app.state.run_list_query = FakeQuery()
    return SimpleNamespace(app=app, auth=auth)


@pytest.mark.asyncio
async def test_run_list_passes_filters_and_returns_closed_page(route_context):
    async with AsyncClient(
        transport=ASGITransport(app=route_context.app), base_url="http://127.0.0.1:3000"
    ) as client:
        client.cookies.set("forge_session", route_context.auth.session_token)
        response = await client.get(
            "/api/run-projections?state=CREATED&attention=false&offset=2&limit=1",
            headers={"Host": "127.0.0.1:3000"},
        )
    assert response.status_code == 200
    assert response.json()["offset"] == 2
    assert response.json()["truncated"] is True
    assert route_context.app.state.run_list_query.kwargs["offset"] == 2
    assert route_context.app.state.run_list_query.kwargs["limit"] == 1


@pytest.mark.asyncio
async def test_run_list_requires_operator_and_rejects_bad_bounds(route_context):
    async with AsyncClient(
        transport=ASGITransport(app=route_context.app), base_url="http://127.0.0.1:3000"
    ) as client:
        unauthenticated = await client.get("/api/run-projections")
        client.cookies.set("forge_session", route_context.auth.session_token)
        invalid = await client.get("/api/run-projections?limit=101")
    assert unauthenticated.status_code == 401
    assert invalid.status_code == 422
