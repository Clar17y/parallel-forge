"""Tests for run workflow preferences and ordinary development routes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_run_create_default_workflow_preferences(
    task10_client: AsyncClient, task10_route_context, route_headers
) -> None:
    """New local run creation defaults to ordinary workflow preferences."""
    response = await task10_client.post(
        "/api/runs",
        headers={**route_headers, "Idempotency-Key": "run-create-workflow-default"},
        json={"task_id": str(task10_route_context.task.id)},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["workflow"] is not None
    assert body["workflow"]["review_plan_first"] is False
    assert body["workflow"]["delegation"] == "auto"
    assert body["workflow"]["optional_review"] == "auto"


@pytest.mark.asyncio
async def test_run_create_custom_workflow_preferences(
    task10_client: AsyncClient, task10_route_context, route_headers
) -> None:
    """New local run creation accepts explicit workflow preferences."""
    response = await task10_client.post(
        "/api/runs",
        headers={**route_headers, "Idempotency-Key": "run-create-workflow-custom"},
        json={
            "task_id": str(task10_route_context.task.id),
            "workflow": {
                "review_plan_first": True,
                "delegation": "disabled",
                "optional_review": "required",
            },
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["workflow"] is not None
    assert body["workflow"]["review_plan_first"] is True
    assert body["workflow"]["delegation"] == "disabled"
    assert body["workflow"]["optional_review"] == "required"


@pytest.mark.asyncio
async def test_put_run_workflow_preferences_updates_and_checks_version(
    task10_client: AsyncClient, task10_route_context, route_headers
) -> None:
    """Owner can update workflow preferences with version check."""
    run_id = task10_route_context.run.id
    response = await task10_client.put(
        f"/api/runs/{run_id}/workflow",
        headers={**route_headers, "Idempotency-Key": "update-workflow-1"},
        json={
            "expected_run_version": 0,
            "expected_workflow": None,
            "workflow": {
                "review_plan_first": False,
                "delegation": "disabled",
                "optional_review": "skip",
            },
            "note": "operator disabled delegation",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workflow"]["delegation"] == "disabled"
    assert body["workflow"]["optional_review"] == "skip"


@pytest.mark.asyncio
async def test_workflow_update_rejects_stale_version_and_preferences(
    task10_client, task10_route_context, route_headers
):
    for index, body in enumerate(
        (
            {"expected_run_version": 1, "expected_workflow": None, "workflow": {}},
            {"expected_run_version": 0, "expected_workflow": {}, "workflow": {}},
        )
    ):
        response = await task10_client.put(
            f"/api/runs/{task10_route_context.run.id}/workflow",
            headers={**route_headers, "Idempotency-Key": f"stale-workflow-{index}"},
            json=body,
        )
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_create_explicit_null_retains_legacy_preferences(
    task10_client, task10_route_context, route_headers
):
    response = await task10_client.post(
        "/api/runs",
        headers={**route_headers, "Idempotency-Key": "explicit-null"},
        json={"task_id": str(task10_route_context.task.id), "workflow": None},
    )
    assert response.status_code == 201
    assert response.json()["workflow"] is None
