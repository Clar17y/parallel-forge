"""Shared authenticated client and service doubles for Task 10 route tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from forge.application.ports.projects import ProjectPolicyRecord, ProjectRecord
from forge.application.ports.tasks import TaskRecord
from forge.application.services.auth import (
    AuthenticatedActor,
    AuthenticationError,
    CsrfError,
)
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunSnapshot, RunState
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient


class FakeRouteAuthService:
    """Minimal server-side session boundary used by route tests."""

    def __init__(self) -> None:
        self.session_token = "route-session-token"
        self.csrf_token = "route-csrf-token"
        self.actor = AuthenticatedActor(
            actor_id=uuid4(), actor_class="operator", session_id=uuid4()
        )
        self.error: Exception | None = None
        self.calls = 0

    async def require_session(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedActor:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if token != self.session_token:
            raise AuthenticationError("invalid or expired session")
        if require_csrf and csrf_token != self.csrf_token:
            raise CsrfError("invalid csrf token")
        return self.actor


class FakeProjectService:
    def __init__(self, project: ProjectRecord) -> None:
        self.project = project
        self.register_calls: list[tuple[object, str, object]] = []
        self.update_calls: list[tuple[object, UUID, str, object]] = []
        self.list_calls = 0
        self.get_calls: list[UUID] = []

    async def register(
        self, *, actor: object, idempotency_key: str, request: object
    ) -> ProjectRecord:
        self.register_calls.append((actor, idempotency_key, request))
        return self.project

    async def update_policy(
        self, *, actor: object, project_id: UUID, idempotency_key: str, request: object
    ) -> ProjectPolicyRecord:
        self.update_calls.append((actor, project_id, idempotency_key, request))
        assert self.project.policy is not None
        return ProjectPolicyRecord(
            project_id=project_id,
            version=self.project.policy.version + 1,
            policy_digest="b" * 64,
            document_schema_version=1,
            document=self.project.policy.document,
        )

    async def list(self) -> Sequence[ProjectRecord]:
        self.list_calls += 1
        return [self.project]

    async def get(self, project_id: UUID) -> ProjectRecord:
        self.get_calls.append(project_id)
        if project_id != self.project.id:
            raise RuntimeError("missing project")
        return self.project


class FakeTaskService:
    def __init__(self, task: TaskRecord) -> None:
        self.task = task
        self.plain_calls: list[tuple[object, str, object]] = []
        self.external_calls: list[tuple[object, str, object]] = []
        self.list_calls: list[UUID] = []
        self.get_calls: list[UUID] = []

    async def create_plain_text(
        self, *, actor: object, idempotency_key: str, request: object
    ) -> TaskRecord:
        self.plain_calls.append((actor, idempotency_key, request))
        return self.task

    async def create_from_external(
        self, *, actor: object, idempotency_key: str, request: object
    ) -> TaskRecord:
        self.external_calls.append((actor, idempotency_key, request))
        return self.task

    async def list(self, project_id: UUID) -> Sequence[TaskRecord]:
        self.list_calls.append(project_id)
        return [self.task]

    async def get(self, task_id: UUID) -> TaskRecord:
        self.get_calls.append(task_id)
        if task_id != self.task.id:
            raise RuntimeError("missing task")
        return self.task


class FakeRunService:
    def __init__(self, run: RunSnapshot) -> None:
        self.run = run
        self.create_calls: list[tuple[object, str, UUID]] = []
        self.list_calls: list[tuple[UUID | None, UUID | None]] = []
        self.get_calls: list[UUID] = []

    async def create_run(
        self, *, actor: object, idempotency_key: str, task_id: UUID
    ) -> RunSnapshot:
        self.create_calls.append((actor, idempotency_key, task_id))
        return self.run

    async def list(
        self, *, project_id: UUID | None = None, task_id: UUID | None = None
    ) -> Sequence[RunSnapshot]:
        self.list_calls.append((project_id, task_id))
        return [self.run]

    async def get(self, run_id: UUID) -> RunSnapshot:
        self.get_calls.append(run_id)
        if run_id != self.run.id:
            raise RuntimeError("missing run")
        return self.run


class FakeRunCommandService:
    def __init__(self, command: CommandEnvelope) -> None:
        self.command = command
        self.calls: list[tuple[object, UUID, str, object]] = []

    async def enqueue(
        self, *, actor: object, run_id: UUID, idempotency_key: str, request: object
    ) -> CommandEnvelope:
        self.calls.append((actor, run_id, idempotency_key, request))
        return self.command


@pytest.fixture
def task10_route_context() -> SimpleNamespace:
    from forge.api.app import create_app

    project_id = uuid4()
    task_id = uuid4()
    run_id = uuid4()
    policy = ProjectPolicyRecord(
        project_id=project_id,
        version=1,
        policy_digest="a" * 64,
        document_schema_version=1,
        document={"runner_mode": "docker", "database": {"enabled": False}},
    )
    project = ProjectRecord(
        id=project_id,
        name="Parallel",
        canonical_path="D:/Code/Parallel",
        canonical_path_key="d:/code/parallel",
        github_repository="clar17y/parallel",
        default_branch="main",
        instructions_path=None,
        current_policy_version=1,
        policy=policy,
    )
    task = TaskRecord(
        id=task_id,
        project_id=project_id,
        title="Exact title",
        body="Exact body",
        source_url=None,
        source_updated_at=None,
        untrusted_external_content=False,
        normalized_text="Exact title\n\nExact body",
        task_digest="c" * 64,
        external_source=None,
        external_id=None,
    )
    run = RunSnapshot(
        id=run_id,
        project_id=project_id,
        task_id=task_id,
        state=RunState.CREATED,
        version=0,
        policy_version=1,
        base_ref="refs/heads/main",
        base_sha="d" * 40,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    command = CommandEnvelope(
        id=uuid4(),
        run_id=run_id,
        command_type="pause",
        idempotency_key="run-command:commands:hashed",
        payload={},
        status=CommandStatus.PENDING,
        expected_run_version=0,
        actor_id=uuid4(),
        payload_schema_version=1,
        attempt=0,
        available_at=now,
        lease_owner=None,
        lease_expires_at=None,
        created_at=now,
    )
    auth = FakeRouteAuthService()
    projects = FakeProjectService(project)
    tasks = FakeTaskService(task)
    runs = FakeRunService(run)
    commands = FakeRunCommandService(command)
    settings = Settings(web_origin="http://127.0.0.1:3000")
    app = create_app(
        settings,
        unit_of_work_factory=lambda: object(),
        auth_service=auth,
        approval_challenge_service=object(),
        approval_authorization_service=object(),
        project_service=projects,
        task_service=tasks,
        run_service=runs,
        run_command_service=commands,
    )
    return SimpleNamespace(
        app=app,
        auth=auth,
        projects=projects,
        tasks=tasks,
        runs=runs,
        commands=commands,
        project=project,
        task=task,
        run=run,
    )


@pytest.fixture
async def task10_client(task10_route_context: SimpleNamespace):
    settings = task10_route_context.app.state.settings
    async with AsyncClient(
        transport=ASGITransport(app=task10_route_context.app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", task10_route_context.auth.session_token)
        yield client


@pytest.fixture
def route_headers() -> dict[str, str]:
    return {
        "Host": "127.0.0.1:3000",
        "Origin": "http://127.0.0.1:3000",
        "X-CSRF-Token": "route-csrf-token",
    }
