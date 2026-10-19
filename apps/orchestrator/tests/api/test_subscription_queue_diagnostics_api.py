"""API serialization tests for subscription task queue diagnostics."""

from uuid import uuid4

import pytest
from forge.api.schemas.subscription_tasks import SubscriptionTaskPage

ROUTE = {
    "provider": "google",
    "client": "antigravity_cli",
    "model": "gemini-3.8-flash-medium",
    "effort": "medium",
    "auth_mode": "subscription",
    "billing_mode": "allowance_only",
}


@pytest.mark.asyncio
async def test_queue_diagnostic_api_serialization_all_statuses(
    task10_client, task10_route_context, route_headers
):
    run_id = uuid4()
    task_id = uuid4()

    for status, reason in [
        ("no_current_worker", None),
        ("no_matching_registered_route", None),
        ("matched_unavailable", "signed_out"),
        ("matching_ready", "operator_trusted"),
    ]:
        diag = {
            "task_id": str(task_id),
            "purpose": "primary",
            "status": status,
            "route": ROUTE,
            "reason": reason,
            "message": f"Diagnostics test for {status}",
            "guidance": "Test guidance",
            "warnings": ["approved_tools_unproved"],
            "available_routes": [ROUTE],
        }

        def make_mock(current_diag):
            class MockQuery:
                async def tasks(self, *args, **kwargs):
                    return {
                        "run_id": run_id,
                        "subscription": True,
                        "tasks": [
                            {
                                "task_id": task_id,
                                "parent_task_id": None,
                                "dependency_task_ids": [],
                                "purpose": "primary",
                                "owned_paths": [],
                                "state": "queued",
                                "pause_requested": False,
                                "cancel_requested": False,
                                "version": 1,
                                "repairs": None,
                                "unsettled_effects": 0,
                                "queue_diagnostic": current_diag,
                            }
                        ],
                        "has_more": False,
                        "queue_diagnostic": current_diag,
                    }

            return MockQuery()

        task10_route_context.app.state.subscription_task_query = make_mock(diag)
        response = await task10_client.get(
            f"/api/runs/{run_id}/subscription-tasks",
            headers={"Host": route_headers["Host"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["queue_diagnostic"]["status"] == status
        assert data["queue_diagnostic"]["reason"] == reason
        assert data["tasks"][0]["queue_diagnostic"]["status"] == status

        # Pydantic model validation on schema
        page = SubscriptionTaskPage.model_validate(data)
        assert page.queue_diagnostic.status == status


@pytest.mark.asyncio
async def test_queue_diagnostic_api_suppression(
    task10_client, task10_route_context, route_headers
):
    run_id = uuid4()
    task_id = uuid4()

    class SuppressedQuery:
        async def tasks(self, *args, **kwargs):
            return {
                "run_id": run_id,
                "subscription": True,
                "tasks": [
                    {
                        "task_id": task_id,
                        "parent_task_id": None,
                        "dependency_task_ids": [],
                        "purpose": "primary",
                        "owned_paths": [],
                        "state": "leased",
                        "pause_requested": False,
                        "cancel_requested": False,
                        "version": 1,
                        "repairs": None,
                        "unsettled_effects": 0,
                        "queue_diagnostic": None,
                    }
                ],
                "has_more": False,
                "queue_diagnostic": None,
            }

    task10_route_context.app.state.subscription_task_query = SuppressedQuery()
    response = await task10_client.get(
        f"/api/runs/{run_id}/subscription-tasks",
        headers={"Host": route_headers["Host"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["queue_diagnostic"] is None
    assert data["tasks"][0]["queue_diagnostic"] is None
    page = SubscriptionTaskPage.model_validate(data)
    assert page.queue_diagnostic is None


@pytest.mark.asyncio
async def test_queue_diagnostic_api_fallback_route_serialization(
    task10_client, task10_route_context, route_headers
):
    run_id = uuid4()
    task_id = uuid4()

    fallback_route = {
        "provider": "codex",
        "client": "codex_cli",
        "model": "gpt-5.4",
        "effort": "medium",
        "auth_mode": "subscription",
        "billing_mode": "allowance_only",
    }
    preferred_route = ROUTE

    diag = {
        "task_id": str(task_id),
        "purpose": "routine_implementation",
        "status": "matching_ready",
        "route": fallback_route,
        "reason": "operator_trusted",
        "message": (
            "Specialist fallback route codex/codex_cli:gpt-5.4 is registered by an active worker "
            "and quota-ready; preferred route google/antigravity_cli:gemini-3.8-flash-medium has "
            "exhausted allowance quota."
        ),
        "guidance": "Worker will switch task execution to fallback route upon lease.",
        "warnings": [],
        "available_routes": [fallback_route],
    }

    class FallbackQuery:
        async def tasks(self, *args, **kwargs):
            return {
                "run_id": run_id,
                "subscription": True,
                "tasks": [
                    {
                        "task_id": task_id,
                        "parent_task_id": None,
                        "dependency_task_ids": [],
                        "purpose": "routine_implementation",
                        "owned_paths": [],
                        "state": "queued",
                        "pause_requested": False,
                        "cancel_requested": False,
                        "version": 1,
                        "repairs": None,
                        "unsettled_effects": 0,
                        "requested_route": preferred_route,
                        "effective_route": fallback_route,
                        "fallback_selected": True,
                        "queue_diagnostic": diag,
                    }
                ],
                "has_more": False,
                "queue_diagnostic": diag,
            }

    task10_route_context.app.state.subscription_task_query = FallbackQuery()
    response = await task10_client.get(
        f"/api/runs/{run_id}/subscription-tasks",
        headers={"Host": route_headers["Host"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["queue_diagnostic"]["status"] == "matching_ready"
    assert data["queue_diagnostic"]["route"]["provider"] == "codex"
    assert data["tasks"][0]["fallback_selected"] is True
    assert data["tasks"][0]["effective_route"]["provider"] == "codex"

    page = SubscriptionTaskPage.model_validate(data)
    assert page.queue_diagnostic.status == "matching_ready"
    assert page.queue_diagnostic.route.provider == "codex"
    assert page.tasks[0].fallback_selected is True
