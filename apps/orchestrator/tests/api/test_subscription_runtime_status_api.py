"""Authenticated runtime diagnostics cannot grant registration or launch authority."""

from datetime import UTC, datetime

import pytest
from forge.application.services.auth import AuthenticationError


class Query:
    def __init__(self):
        self.calls = []

    async def status(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "observed_at": datetime(2026, 9, 12, tzinfo=UTC),
            "fresh_for_seconds": 45,
            "workers": [],
            "has_more": False,
        }


@pytest.mark.asyncio
async def test_runtime_status_requires_authentication_and_bounds_page(
    task10_client, task10_route_context, route_headers
):
    query = Query()
    task10_route_context.app.state.subscription_runtime_status = query
    path = "/api/subscription-runtime?offset=2&limit=10"
    response = await task10_client.get(path, headers={"Host": route_headers["Host"]})
    assert response.status_code == 200
    assert response.json()["workers"] == []
    assert query.calls == [{"offset": 2, "limit": 10}]
    assert (
        await task10_client.get(path + "1", headers={"Host": route_headers["Host"]})
    ).status_code == 422
    task10_route_context.auth.error = AuthenticationError()
    assert (
        await task10_client.get(path, headers={"Host": route_headers["Host"]})
    ).status_code == 401
    assert len(query.calls) == 1


@pytest.mark.asyncio
async def test_runtime_status_unavailable_is_not_empty_inventory(
    task10_client, task10_route_context, route_headers
):
    task10_route_context.app.state.subscription_runtime_status = None
    response = await task10_client.get(
        "/api/subscription-runtime", headers={"Host": route_headers["Host"]}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "subscription runtime status unavailable"
