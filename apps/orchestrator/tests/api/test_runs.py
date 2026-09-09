"""Authenticated run and closed command HTTP boundary tests."""

from __future__ import annotations

import asyncio

import pytest
from forge.api.app import create_app
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_runs_list_create_and_get_use_injected_services(
    task10_client, task10_route_context, route_headers
) -> None:
    host_headers = {"Host": route_headers["Host"]}
    listed = await task10_client.get("/api/runs", headers=host_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == str(task10_route_context.run.id)

    created = await task10_client.post(
        "/api/runs",
        headers={**route_headers, "Idempotency-Key": "run-create-1"},
        json={"task_id": str(task10_route_context.task.id)},
    )
    assert created.status_code == 201
    assert created.json()["state"] == "CREATED"
    assert task10_route_context.runs.create_calls[0][1] == "run-create-1"

    fetched = await task10_client.get(
        f"/api/runs/{task10_route_context.run.id}", headers=host_headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["base_ref"] == "refs/heads/main"


@pytest.mark.asyncio
async def test_run_command_route_enqueues_closed_command_without_transition(
    task10_client, task10_route_context, route_headers
) -> None:
    response = await task10_client.post(
        f"/api/runs/{task10_route_context.run.id}/commands",
        headers={**route_headers, "Idempotency-Key": "run-command-1"},
        json={"command_type": "pause", "expected_run_version": 0},
    )
    assert response.status_code == 202
    assert response.json()["command_type"] == "pause"
    assert response.json()["status"] == "pending"
    assert task10_route_context.commands.calls[0][2] == "run-command-1"


@pytest.mark.asyncio
async def test_run_command_missing_idempotency_key_is_422_and_not_enqueued(
    task10_client, task10_route_context, route_headers
) -> None:
    response = await task10_client.post(
        f"/api/runs/{task10_route_context.run.id}/commands",
        headers=route_headers,
        json={"command_type": "pause", "expected_run_version": 0},
    )
    assert response.status_code == 422
    assert task10_route_context.commands.calls == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_operator_client_registers_task_and_created_run(
    session_factory, tmp_path
) -> None:
    """Exercise the real session/auth/service stack through the HTTP client."""

    repository = tmp_path / "repo"
    data_root = tmp_path / "data"
    repository.mkdir()
    data_root.mkdir()
    await _git(repository, "init", "-b", "main")
    await _git(repository, "config", "user.email", "forge@example.test")
    await _git(repository, "config", "user.name", "Forge Test")
    (repository / "README.md").write_text("forge\n", encoding="utf-8")
    await _git(repository, "add", "README.md")
    await _git(repository, "commit", "-m", "initial")
    await _git(repository, "config", "remote.origin.url", "https://github.com/Owner/Repo.git")

    settings = Settings(web_origin="http://127.0.0.1:3000", data_root=data_root)
    app = create_app(settings, session_factory=session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        bootstrap = await app.state.auth_service.issue_bootstrap()
        exchanged = await client.post(
            "/api/auth/bootstrap",
            headers={"Host": "127.0.0.1:3000", "Origin": settings.web_origin},
            json={"token": bootstrap},
        )
        assert exchanged.status_code == 200
        secure_headers = {
            "Host": "127.0.0.1:3000",
            "Origin": settings.web_origin,
            "X-CSRF-Token": exchanged.json()["csrf_token"],
        }
        project_response = await client.post(
            "/api/projects",
            headers={**secure_headers, "Idempotency-Key": "operator-project-1"},
            json={
                "name": "Parallel",
                "repository_path": str(repository),
                "github_repository": "owner/repo",
                "default_branch": "main",
                "database": {"enabled": False},
            },
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]
        task_response = await client.post(
            "/api/tasks",
            headers={**secure_headers, "Idempotency-Key": "operator-task-1"},
            json={"project_id": project_id, "title": "Exact title", "body": "Exact body"},
        )
        assert task_response.status_code == 201
        run_response = await client.post(
            "/api/runs",
            headers={**secure_headers, "Idempotency-Key": "operator-run-1"},
            json={"task_id": task_response.json()["id"]},
        )

    assert run_response.status_code == 201
    assert run_response.json()["state"] == "CREATED"
    assert run_response.json()["base_ref"] == "refs/heads/main"


async def _git(repository, *arguments: str) -> None:
    import subprocess

    await asyncio.to_thread(
        subprocess.run,
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )
