from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from forge.application.ports.projects import RepositoryInspection
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.projects import ProjectRegistrationRequest, ProjectService
from forge.application.services.runs import (
    RunCommandRequest,
    RunCommandService,
    RunService,
    hash_run_command_idempotency_key,
)
from forge.application.services.tasks import PlainTextTaskRequest, TaskService
from forge.persistence.models import ApiMutation, Run, RunCommand, RunEvent, Task
from forge.persistence.repositories.commands import IdempotencyConflict
from forge.persistence.repositories.mutations import MutationConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, select, update


class StableInspector:
    def __init__(self, repository: Path) -> None:
        self.repository = repository

    def inspect(self, **kwargs: str) -> RepositoryInspection:
        del kwargs
        return RepositoryInspection(
            canonical_path=str(self.repository.resolve()),
            github_repository="owner/repo",
            default_branch="main",
            base_ref="refs/heads/main",
            base_sha="d" * 40,
        )


async def _seed_project_task(
    session_factory: object, tmp_path: Path
) -> tuple[AuthenticatedActor, UUID, UUID]:
    repository = tmp_path / "repo"
    data_root = tmp_path / "data"
    repository.mkdir()
    data_root.mkdir()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    project = await ProjectService(
        lambda: PostgresUnitOfWork(session_factory),
        repository_inspector=StableInspector(repository),
        data_root=data_root,
    ).register(
        actor=actor,
        idempotency_key="seed-project",
        request=ProjectRegistrationRequest(
            name="Forge",
            repository_path=str(repository),
            github_repository="owner/repo",
            default_branch="main",
        ),
    )
    task = await TaskService(lambda: PostgresUnitOfWork(session_factory)).create_plain_text(
        actor=actor,
        idempotency_key="seed-task",
        request=PlainTextTaskRequest(project_id=project.id, title="Task", body="Body"),
    )
    return actor, project.id, task.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_run_is_atomic_and_idempotent(session_factory: object, tmp_path: Path) -> None:
    actor, project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    service = RunService(
        lambda: PostgresUnitOfWork(session_factory),
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    )
    first = await service.create_run(actor=actor, idempotency_key="run-one", task_id=task_id)
    replay = await service.create_run(actor=actor, idempotency_key="run-one", task_id=task_id)
    assert replay == first
    with pytest.raises(MutationConflict):
        await service.create_run(actor=actor, idempotency_key="run-one", task_id=uuid4())
    assert first.project_id == project_id
    assert first.policy_version == 1
    assert first.base_ref == "refs/heads/main"
    assert first.base_sha == "d" * 40
    assert await service.get(first.id) == first
    assert await service.list(project_id=project_id) == [first]

    async with PostgresUnitOfWork(session_factory) as work:
        event_count = await work.session.scalar(
            select(func.count()).select_from(RunEvent).where(RunEvent.run_id == first.id)
        )
        command_count = await work.session.scalar(
            select(func.count()).select_from(RunCommand).where(RunCommand.run_id == first.id)
        )
        assert event_count == 1
        assert command_count == 1
        event = await work.session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == first.id, RunEvent.event_type == "run.created"
            )
        )
        assert event is not None
        task_record = await work.session.get(Task, task_id)
        assert task_record is not None
        assert event.payload == {
            "project_id": str(project_id),
            "task_id": str(task_id),
            "task_digest": task_record.task_digest,
            "policy_version": 1,
            "base_ref": "refs/heads/main",
            "base_sha": "d" * 40,
        }
        assert "secret_id" not in event.payload
        command = await work.session.scalar(
            select(RunCommand).where(
                RunCommand.run_id == first.id, RunCommand.command_type == "start_planning"
            )
        )
        assert command is not None
        assert command.payload == {}
        assert command.expected_run_version == 0
        await work.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_run_failure_after_event_rolls_back_everything(
    session_factory: object, tmp_path: Path
) -> None:
    actor, project_id, task_id = await _seed_project_task(session_factory, tmp_path)

    class FailingEvents:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        async def append(self, event: object) -> object:
            await self._delegate.append(event)
            raise RuntimeError("injected failure after event creation")

    class FailingEventUow(PostgresUnitOfWork):
        async def __aenter__(self):
            work = await super().__aenter__()
            self.events = FailingEvents(self.events)
            return work

    service = RunService(
        lambda: FailingEventUow(session_factory),
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    )
    with pytest.raises(RuntimeError, match="after event creation"):
        await service.create_run(actor=actor, idempotency_key="run-fails", task_id=task_id)

    async with PostgresUnitOfWork(session_factory) as work:
        run_count = await work.session.scalar(
            select(func.count()).select_from(Run).where(Run.project_id == project_id)
        )
        event_count = await work.session.scalar(
            select(func.count()).select_from(RunEvent).where(RunEvent.actor_id == actor.actor_id)
        )
        command_count = await work.session.scalar(
            select(func.count())
            .select_from(RunCommand)
            .where(RunCommand.actor_id == actor.actor_id)
        )
        receipt_count = await work.session.scalar(
            select(func.count())
            .select_from(ApiMutation)
            .where(ApiMutation.actor_id == actor.actor_id, ApiMutation.action == "create_run")
        )
        assert run_count == 0
        assert event_count == 0
        assert command_count == 0
        assert receipt_count == 0
        await work.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_commands_lock_and_enqueue_without_transition(
    session_factory: object, tmp_path: Path
) -> None:
    actor, _, task_id = await _seed_project_task(session_factory, tmp_path)
    run_service = RunService(
        lambda: PostgresUnitOfWork(session_factory),
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    )
    run = await run_service.create_run(
        actor=actor, idempotency_key="run-command-seed", task_id=task_id
    )
    async with session_factory() as session, session.begin():
        await session.execute(update(Run).where(Run.id == run.id).values(state="PLANNING"))

    command_service = RunCommandService(lambda: PostgresUnitOfWork(session_factory))
    request = RunCommandRequest(command_type="pause", expected_run_version=0)
    first = await command_service.enqueue(
        actor=actor, run_id=run.id, idempotency_key="pause-1", request=request
    )
    replay = await command_service.enqueue(
        actor=actor, run_id=run.id, idempotency_key="pause-1", request=request
    )
    assert replay.id == first.id
    assert first.idempotency_key == hash_run_command_idempotency_key(
        "pause-1", actor_id=actor.actor_id, run_id=run.id
    )
    with pytest.raises(IdempotencyConflict):
        await command_service.enqueue(
            actor=actor,
            run_id=run.id,
            idempotency_key="pause-1",
            request=RunCommandRequest(command_type="cancel", expected_run_version=0),
        )

    async with PostgresUnitOfWork(session_factory) as work:
        stored = await work.session.get(Run, run.id)
        assert stored is not None
        assert stored.state == "PLANNING"
        assert stored.version == 0
        await work.commit()
