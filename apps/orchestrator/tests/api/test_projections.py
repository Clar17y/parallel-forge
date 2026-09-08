"""Durable cockpit projections expose selected evidence and server authority."""

from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.application.services.projects import _digest
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot
from forge.persistence.models import Project, ProjectPolicyVersion, Task
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("recovery_hold", [False, True])
async def test_disabled_database_projection_is_complete_and_has_server_commands(
    session_factory, tmp_path, recovery_hold
):
    persisted_run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), policy_version=1)
    policy = ProjectPolicy(
        id=persisted_run.project_id,
        version=1,
        repository_path=str(tmp_path),
        github_repository="owner/repo",
        default_branch="main",
    )
    async with session_factory() as session, session.begin():
        project = Project(
            id=policy.id,
            canonical_path=str(tmp_path),
            github_repository="owner/repo",
            default_branch="main",
        )
        document = policy.model_dump(mode="json")
        session.add_all(
            [
                project,
                ProjectPolicyVersion(
                    project_id=policy.id,
                    version=1,
                    document=document,
                    document_schema_version=1,
                    policy_digest=_digest(document),
                ),
                Task(
                    id=persisted_run.task_id,
                    project_id=policy.id,
                    normalized_text="test",
                    task_digest="b" * 64,
                ),
            ]
        )
        await session.flush()
        project.current_policy_version = 1
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(persisted_run)
        if recovery_hold:
            from forge.domain.event import RunEvent
            from forge.domain.operation import canonical_digest

            await work.runs.pause(persisted_run.id, 0, "test.paused", {})
            await work.operations.begin(
                run_id=persisted_run.id, operation_type="unknown_effect",
                idempotency_key="unknown", request_payload={}, request_digest=canonical_digest({}),
            )
            await work.events.append(RunEvent(
                run_id=persisted_run.id, run_version=1,
                event_type="run.recovery_intervention", actor_class="worker",
                payload={"reason": "startup_outcome_unresolved"},
            ))
        await work.commit()
    app = create_app(Settings(data_root=tmp_path / "data"), session_factory=session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        url = f"/api/runs/{persisted_run.id}/projection"
        assert (await client.get(url)).status_code == 401
        token = await app.state.auth_service.issue_bootstrap()
        assert (
            await client.post(
                "/api/auth/bootstrap",
                json={"token": token},
                headers={"Origin": "http://127.0.0.1:3000"},
            )
        ).status_code == 200
        response = await client.get(url)
    assert response.status_code == 200
    projection = response.json()
    assert set(projection) == {
        "run",
        "task",
        "project",
        "resource",
        "plan",
        "candidate",
        "pull_request",
        "checks",
        "review",
        "agents",
        "budgets",
        "usage",
        "security",
        "latest_events",
        "available_commands",
        "next_gate",
        "recovery_hold",
    }
    assert projection["agents"]["reviewer"]["independent"] is None
    assert projection["resource"]["database_state"] == "DISABLED"
    assert projection["resource"]["database_name"] is None
    assert "secret_id" not in response.text and "secret_reference" not in response.text
    assert projection["recovery_hold"] is recovery_hold
    expected = {"cancel"} if recovery_hold else {"pause", "cancel"}
    assert {item["name"] for item in projection["available_commands"]} == expected


@pytest.mark.parametrize("path", ["/api/dashboard/summary", f"/api/runs/{uuid4()}/projection"])
async def test_unconfigured_projection_returns_bounded_503(path, tmp_path):
    from forge.api.dependencies import require_operator

    app = create_app(Settings(data_root=tmp_path), unit_of_work_factory=lambda: None)
    app.dependency_overrides[require_operator] = lambda: object()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "projection unavailable"}
