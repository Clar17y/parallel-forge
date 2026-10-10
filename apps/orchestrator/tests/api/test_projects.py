"""Authenticated project HTTP boundary tests."""

from __future__ import annotations

from copy import deepcopy

import pytest
from forge.api.schemas.projects import ProjectCreateRequest
from forge.application.services.task_usage_policy import MonitoringPolicyRevisionResult
from forge.domain.task_usage_contract import MonitoringPolicy
from pydantic import ValidationError


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
async def test_monitoring_route_uses_authenticated_versioned_owner_action(
    task10_client, task10_route_context, route_headers
) -> None:
    project_id = task10_route_context.project.id

    class FakeMonitoringService:
        def __init__(self) -> None:
            self.calls: list[tuple[object, object, object]] = []

        async def get(self, project_id, revision=None):
            return MonitoringPolicy(project_id=project_id, revision=revision or 1)

        async def revise(self, *, actor, project_id, idempotency_key, request):
            self.calls.append((actor, idempotency_key, request))
            return MonitoringPolicyRevisionResult(
                policy=MonitoringPolicy(
                    project_id=project_id,
                    revision=request.expected_revision + 1,
                    window_days=request.window_days,
                ),
                actor_id=actor.actor_id,
                affected_snapshot="a" * 64,
            )

    service = FakeMonitoringService()
    task10_route_context.app.state.task_usage_policy_service = service
    read = await task10_client.get(f"/api/projects/{project_id}/monitoring-policy")
    assert read.status_code == 200
    assert read.json()["hard_token_cap"] is None
    assert read.json()["window_days"] is None
    created = await task10_client.post(
        f"/api/projects/{project_id}/monitoring-policy",
        headers={**route_headers, "Idempotency-Key": "monitoring-1"},
        json={"expected_revision": 1, "hard_token_cap": None, "window_days": 30},
    )
    assert created.status_code == 201
    assert created.json()["revision"] == 2
    assert created.json()["window_days"] == 30
    assert created.json()["owner_action"]["actor_id"] == str(service.calls[0][0].actor_id)
    assert created.json()["owner_action"]["affected_snapshot"] == "a" * 64
    assert service.calls[0][1] == "monitoring-1"
    assert service.calls[0][2].model_fields_set == {
        "expected_revision",
        "hard_token_cap",
        "window_days",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner_model", "developer_model", "reviewer_model"])
async def test_registration_rejects_unsupported_reasoning_before_service_call(
    task10_client, task10_route_context, route_headers, role
) -> None:
    payload = {
        **_project_request(),
        role: {"provider": "google", "model": "gemini-3-pro", "reasoning_effort": "medium"},
    }
    with pytest.raises(ValidationError) as error:
        ProjectCreateRequest.model_validate(payload)
    assert (role, "reasoning_effort") in [tuple(item["loc"]) for item in error.value.errors()]
    response = await task10_client.post(
        "/api/projects",
        headers={**route_headers, "Idempotency-Key": f"project-invalid-{role}"},
        json=payload,
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}
    assert task10_route_context.projects.register_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner_model", "developer_model", "reviewer_model"])
@pytest.mark.parametrize("effort", [None, "medium"])
async def test_registration_rejects_cli_composite_model_before_service_call(
    task10_client, task10_route_context, route_headers, role, effort
) -> None:
    payload = {
        **_project_request(),
        role: {
            "provider": "google",
            "model": "gemini-3.8-flash-medium",
            "reasoning_effort": effort,
        },
    }
    with pytest.raises(ValidationError) as error:
        ProjectCreateRequest.model_validate(payload)
    assert (role, "model") in [tuple(item["loc"]) for item in error.value.errors()]
    response = await task10_client.post(
        "/api/projects",
        headers={**route_headers, "Idempotency-Key": f"project-invalid-cli-{role}-{effort}"},
        json=payload,
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}
    assert task10_route_context.projects.register_calls == []


@pytest.mark.asyncio
async def test_issue_import_capability_requires_configured_server_service(
    task10_client, task10_route_context, route_headers
) -> None:
    url = f"/api/projects/{task10_route_context.project.id}"
    response = await task10_client.get(url)
    assert response.json()["issue_import_available"] is False
    task10_route_context.app.state.github_issue_import_service = object()
    response = await task10_client.get(url)
    assert response.json()["issue_import_available"] is True
    listed = await task10_client.get("/api/projects")
    assert listed.json()[0]["issue_import_available"] is True


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
