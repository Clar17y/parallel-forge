"""Authenticated task HTTP boundary tests."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_tasks_list_create_and_get_use_injected_service(
    task10_client, task10_route_context, route_headers
) -> None:
    host_headers = {"Host": route_headers["Host"]}
    listed = await task10_client.get(
        f"/api/tasks?project_id={task10_route_context.project.id}", headers=host_headers
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == str(task10_route_context.task.id)

    created = await task10_client.post(
        "/api/tasks",
        headers={**route_headers, "Idempotency-Key": "task-create-1"},
        json={
            "project_id": str(task10_route_context.project.id),
            "title": "Exact title",
            "body": "Exact body",
        },
    )
    assert created.status_code == 201
    assert created.json()["title"] == task10_route_context.task.title
    assert task10_route_context.tasks.plain_calls[0][1] == "task-create-1"

    fetched = await task10_client.get(
        f"/api/tasks/{task10_route_context.task.id}", headers=host_headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["normalized_text"] == task10_route_context.task.normalized_text


@pytest.mark.asyncio
async def test_task_create_rejects_extra_request_fields_with_422(
    task10_client, task10_route_context, route_headers
) -> None:
    response = await task10_client.post(
        "/api/tasks",
        headers={**route_headers, "Idempotency-Key": "task-extra-1"},
        json={
            "project_id": str(task10_route_context.project.id),
            "title": "Exact title",
            "body": "Exact body",
            "environment": {"SECRET": "must-not-cross-http"},
        },
    )
    assert response.status_code == 422
    assert "must-not-cross-http" not in response.text
    assert task10_route_context.tasks.plain_calls == []
