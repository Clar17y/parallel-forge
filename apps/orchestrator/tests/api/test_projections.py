"""Durable cockpit projections expose selected evidence and server authority."""

from uuid import UUID, uuid4

import pytest
from forge.api.app import create_app
from forge.application.services.projects import _digest
from forge.domain.policy import CommandSpec, ProjectPolicy, StepKind
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
        allowed_environment_files=(".env.worker",),
        commands=(
            CommandSpec(kind=StepKind.TEST, name="unit", argv=("pytest",), timeout_seconds=60),
            CommandSpec(
                kind=StepKind.INSTALL,
                name="install",
                argv=("npm", "ci"),
                timeout_seconds=60,
                network_enabled=True,
            ),
        ),
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
        newer = policy.model_copy(
            update={
                "version": 2,
                "commands": (),
                "secret_paths": ("new-secret",),
                "allowed_environment_files": (),
            }
        )
        newer_document = newer.model_dump(mode="json")
        session.add(
            ProjectPolicyVersion(
                project_id=policy.id,
                version=2,
                document=newer_document,
                document_schema_version=1,
                policy_digest=_digest(newer_document),
            )
        )
        await session.flush()
        project.current_policy_version = 1
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(persisted_run)
        if recovery_hold:
            from forge.domain.event import RunEvent
            from forge.domain.operation import canonical_digest

            await work.runs.pause(persisted_run.id, 0, "test.paused", {})
            intent = await work.operations.begin(
                run_id=persisted_run.id,
                operation_type="unknown_effect",
                idempotency_key="unknown",
                request_payload={},
                request_digest=canonical_digest({}),
            )
            await work.events.append(
                RunEvent(
                    run_id=persisted_run.id,
                    run_version=1,
                    event_type="run.recovery_intervention",
                    actor_class="worker",
                    payload={"reason": "startup_outcome_unresolved"},
                )
            )
        await work.commit()
    async with session_factory() as session, session.begin():
        project = await session.get(Project, policy.id)
        project.current_policy_version = 2
    from datetime import UTC, datetime, timedelta

    from forge.persistence.models import Step, ValidationResult

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        for attempt, status in ((1, "FAILED"), (2, "RUNNING")):
            step = Step(run_id=persisted_run.id, kind="validate", attempt=attempt, status=status)
            session.add(step)
            await session.flush()
            session.add(
                ValidationResult(
                    run_id=persisted_run.id,
                    step_id=step.id,
                    check_name="ci",
                    command_name="ci",
                    command_version=1,
                    status=status,
                    started_at=now + timedelta(seconds=attempt),
                    completed_at=now + timedelta(seconds=attempt + 1) if attempt == 1 else None,
                    exit_code=1 if attempt == 1 else None,
                )
            )
    app = create_app(Settings(data_root=tmp_path / "data"), session_factory=session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        url = f"/api/runs/{persisted_run.id}/projection"
        assert (await client.get(url)).status_code == 401
        assert (await client.get(f"/api/runs/{persisted_run.id}/checks")).status_code == 401
        token = await app.state.auth_service.issue_bootstrap()
        assert (
            await client.post(
                "/api/auth/bootstrap",
                json={"token": token},
                headers={"Origin": "http://127.0.0.1:3000"},
            )
        ).status_code == 200
        response = await client.get(url)
        history = await client.get(f"/api/runs/{persisted_run.id}/checks?limit=1")
        assert history.status_code == 200
        assert history.json()["truncated"] is True
        first = history.json()["items"][0]
        assert first["attempt"] == 1 and first["duration_ms"] == 1000
        assert first["status"] == "FAILED" and first["head_sha"] is None
        second = (await client.get(f"/api/runs/{persisted_run.id}/checks?offset=1&limit=1")).json()
        assert second["truncated"] is False
        assert second["items"][0]["attempt"] == 2
        assert second["items"][0]["duration_ms"] is None
        assert (await client.get(f"/api/runs/{uuid4()}/checks")).status_code == 404
        assert (
            await client.get(f"/api/runs/{persisted_run.id}/checks?limit=101")
        ).status_code == 422
        if recovery_hold:
            from forge.domain.operation import OperationOutcome

            async with PostgresUnitOfWork(session_factory) as work:
                claim = await work.operations.claim_for_recovery(
                    intent.id, owner_id="test-recovery", lease_seconds=30
                )
                assert claim.acquired
                await work.operations.complete(
                    intent.id,
                    OperationOutcome(payload={"observed": True}),
                    owner_id="test-recovery",
                )
                await work.commit()
            assert (await client.get(url)).json()["recovery_hold"] is True
            async with session_factory() as session, session.begin():
                remaining_step = await session.get(Step, step.id)
                remaining_step.status = "CANCELLED"
                remaining_check = await session.get(
                    ValidationResult, UUID(second["items"][0]["id"])
                )
                remaining_check.status = "CANCELLED"
                remaining_check.completed_at = now + timedelta(seconds=4)
            released = (await client.get(url)).json()
            assert released["recovery_hold"] is False
            assert released["run"]["state"] == "PAUSED"
            assert {item["name"] for item in released["available_commands"]} == {"resume", "cancel"}
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
        "remote_observation",
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
    assert projection["security"]["secret_paths"] == [".env", ".env.local", ".env.worker"]
    assert projection["security"]["commands"] == [
        {"name": "unit", "network_enabled": False},
        {"name": "install", "network_enabled": True},
    ]
    assert projection["resource"]["database_state"] == "DISABLED"
    assert projection["resource"]["database_name"] is None
    assert projection["resource"]["database_role"] is None
    from forge.domain.teardown import teardown_confirmation

    async with PostgresUnitOfWork(session_factory) as work:
        current_resource_run = await work.runs.get(persisted_run.id)
    assert projection["resource"]["teardown_confirmation"] == teardown_confirmation(
        current_resource_run
    )
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
