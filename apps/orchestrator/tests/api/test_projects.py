"""Authenticated project HTTP boundary tests."""

from __future__ import annotations

from copy import deepcopy

import pytest


def _project_request() -> dict[str, object]:
    return {
        "name": "Parallel",
        "repository_path": "D:/Code/Parallel",
        "github_repository": "Clar17y/Parallel",
        "default_branch": "main",
        "database": {"enabled": False},
    }


@pytest.mark.asyncio
async def test_projects_list_and_registration_use_injected_service(
    task10_client, task10_route_context, route_headers
) -> None:
    listed = await task10_client.get("/api/projects", headers={"Host": route_headers["Host"]})
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == str(task10_route_context.project.id)

    headers = {**route_headers, "Idempotency-Key": "project-registration-1"}
    created = await task10_client.post("/api/projects", headers=headers, json=_project_request())
    assert created.status_code == 201
    assert created.json()["policy_version"] == 1
    assert created.json()["repository_path"] == task10_route_context.project.canonical_path
    assert task10_route_context.projects.register_calls[0][1] == "project-registration-1"
    request = task10_route_context.projects.register_calls[0][2]
    assert request.name == "Parallel"


@pytest.mark.asyncio
async def test_project_policy_update_uses_closed_mutable_request(
    task10_client, task10_route_context, route_headers
) -> None:
    response = await task10_client.post(
        f"/api/projects/{task10_route_context.project.id}/policy-versions",
        headers={**route_headers, "Idempotency-Key": "policy-update-1"},
        json={"expected_policy_version": 1, "runner_mode": "docker"},
    )
    assert response.status_code == 201
    assert response.json()["policy_version"] == 2
    assert task10_route_context.projects.update_calls[0][2] == "policy-update-1"


@pytest.mark.asyncio
async def test_project_mutations_reject_missing_or_blank_idempotency_key_without_service_call(
    task10_client, task10_route_context, route_headers
) -> None:
    payload = _project_request()
    missing = await task10_client.post("/api/projects", headers=route_headers, json=payload)
    blank = await task10_client.post(
        "/api/projects",
        headers={**route_headers, "Idempotency-Key": "   "},
        json=deepcopy(payload),
    )
    assert missing.status_code == 422
    assert blank.status_code == 422
    assert task10_route_context.projects.register_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie", [None, "expired-session-token"])
async def test_project_mutation_rejects_missing_or_invalid_operator_session(
    task10_client, task10_route_context, route_headers, cookie
) -> None:
    task10_client.cookies.clear()
    if cookie is not None:
        task10_client.cookies.set("forge_session", cookie)
    response = await task10_client.post(
        "/api/projects",
        headers={**route_headers, "Idempotency-Key": "project-session-1"},
        json=_project_request(),
    )
    assert response.status_code == 401
    assert task10_route_context.projects.register_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "security_headers,expected_status",
    [
        ({"Host": "127.0.0.1:3001", "Origin": "http://127.0.0.1:3000"}, 403),
        ({"Host": "127.0.0.1:3000", "Origin": "http://localhost:3000"}, 403),
        ({"Host": "127.0.0.1:3000", "Origin": "http://127.0.0.1:3000"}, 403),
    ],
)
async def test_project_mutations_reject_wrong_origin_or_host_or_csrf(
    task10_client, task10_route_context, route_headers, security_headers, expected_status
) -> None:
    headers = {**security_headers, "Idempotency-Key": "project-security-1"}
    if security_headers == {"Host": "127.0.0.1:3000", "Origin": "http://127.0.0.1:3000"}:
        headers["X-CSRF-Token"] = "wrong-csrf"
    response = await task10_client.post("/api/projects", headers=headers, json=_project_request())
    assert response.status_code == expected_status
    assert task10_route_context.projects.register_calls == []
