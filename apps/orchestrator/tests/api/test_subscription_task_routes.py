"""Authenticated and bounded subscription inspection boundary."""

from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticationError


class Query:
    def __init__(self):
        self.calls = []

    async def tasks(self, run_id, *, offset, limit):
        self.calls.append((run_id, offset, limit))
        return {"run_id": run_id, "subscription": False, "tasks": [], "has_more": False}


@pytest.mark.asyncio
async def test_task_inspection_distinguishes_legacy_and_bounds_page(
    task10_client, task10_route_context, route_headers
):
    query = Query()
    task10_route_context.app.state.subscription_task_query = query
    run_id = uuid4()
    response = await task10_client.get(
        f"/api/runs/{run_id}/subscription-tasks?limit=20&offset=2",
        headers={"Host": route_headers["Host"]},
    )
    assert response.status_code == 200
    assert response.json()["subscription"] is False
    assert query.calls == [(run_id, 2, 20)]
    invalid = await task10_client.get(
        f"/api/runs/{run_id}/subscription-tasks?limit=101",
        headers={"Host": route_headers["Host"]},
    )
    assert invalid.status_code == 422
    assert len(query.calls) == 1


@pytest.mark.asyncio
async def test_task_inspection_authentication_precedes_query(
    task10_client, task10_route_context, route_headers
):
    query = Query()
    task10_route_context.app.state.subscription_task_query = query
    task10_route_context.auth.error = AuthenticationError()
    for suffix in ("", f"/{uuid4()}/attempts"):
        response = await task10_client.get(
            f"/api/runs/{uuid4()}/subscription-tasks{suffix}",
            headers={"Host": route_headers["Host"]},
        )
        assert response.status_code == 401
    assert not query.calls


@pytest.mark.asyncio
async def test_missing_task_and_run_are_explicit(
    task10_client, task10_route_context, route_headers
):
    class Missing:
        async def tasks(self, *args, **kwargs):
            return None

        async def attempts(self, *args, **kwargs):
            return None

    task10_route_context.app.state.subscription_task_query = Missing()
    for suffix in ("", f"/{uuid4()}/attempts"):
        response = await task10_client.get(
            f"/api/runs/{uuid4()}/subscription-tasks{suffix}",
            headers={"Host": route_headers["Host"]},
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_capacity_snapshot_survives_authenticated_projection(
    task10_client, task10_route_context, route_headers
):
    from datetime import UTC, datetime

    capacity = {
        "observed_at": datetime(2026, 9, 12, tzinfo=UTC),
        "policy_version": 2,
        "host": {"active": 2, "limit": 2},
        "run": {"active": 0, "limit": 3},
        "providers": [{"provider": "google", "active": 1, "limit": 2}],
        "queue_order": "least_recently_served_run_then_oldest_task",
    }

    class CapacityQuery(Query):
        async def tasks(self, run_id, *, offset, limit):
            value = await super().tasks(run_id, offset=offset, limit=limit)
            return {**value, "subscription": True, "capacity": capacity}

    task10_route_context.app.state.subscription_task_query = CapacityQuery()
    response = await task10_client.get(
        f"/api/runs/{uuid4()}/subscription-tasks", headers={"Host": route_headers["Host"]}
    )
    assert response.status_code == 200
    assert response.json()["capacity"] == {**capacity, "observed_at": "2026-09-12T00:00:00Z"}
