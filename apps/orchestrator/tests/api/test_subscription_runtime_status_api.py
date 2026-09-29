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

    async def model_catalogs(self):
        self.calls.append("models")
        return {
            "observed_at": datetime(2026, 9, 12, tzinfo=UTC),
            "catalogs": [
                {
                    "provider": "openai",
                    "client": "codex_app_server",
                    "source": "provider",
                    "status": "available",
                    "observed_at": datetime(2026, 9, 12, tzinfo=UTC),
                    "stale": False,
                    "models": [{"id": "gpt-6-sol", "label": "GPT-6 Sol", "efforts": ["medium"]}],
                    "message": "Available choices from the installed client; worker configuration is required to run a selected model.",
                }
            ],
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


@pytest.mark.asyncio
async def test_model_catalog_requires_operator_and_reads_snapshot_only(
    task10_client, task10_route_context, route_headers
):
    query = Query()
    task10_route_context.app.state.subscription_runtime_status = query
    path = "/api/subscription-models"
    response = await task10_client.get(path, headers={"Host": route_headers["Host"]})
    assert response.status_code == 200
    assert response.json()["catalogs"][0]["models"][0]["id"] == "gpt-6-sol"
    assert query.calls == ["models"]
    task10_route_context.auth.error = AuthenticationError()
    assert (
        await task10_client.get(path, headers={"Host": route_headers["Host"]})
    ).status_code == 401
    assert query.calls == ["models"]


@pytest.mark.asyncio
async def test_model_catalog_api_preserves_current_discovery_failure_message(
    task10_client, task10_route_context, route_headers
):
    class FailedQuery(Query):
        async def model_catalogs(self):
            value = await super().model_catalogs()
            catalog = value["catalogs"][0]
            catalog.update(
                source="configured",
                status="unavailable",
                observed_at=None,
                stale=True,
                message="Installed client model metadata is unavailable; showing configured choices.",
            )
            return value

    task10_route_context.app.state.subscription_runtime_status = FailedQuery()
    response = await task10_client.get(
        "/api/subscription-models", headers={"Host": route_headers["Host"]}
    )
    assert response.status_code == 200
    assert (
        response.json()["catalogs"][0]["message"]
        == "Installed client model metadata is unavailable; showing configured choices."
    )
