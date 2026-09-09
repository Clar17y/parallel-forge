from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.projects import RepositoryInspection
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.projects import ProjectRegistrationRequest, ProjectService
from forge.application.services.tasks import PlainTextTaskRequest, TaskService


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
            base_sha="a" * 40,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_and_task_services_share_uow_receipts_and_audit(
    session_factory: object, tmp_path: Path
) -> None:
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    repository = tmp_path / "repo"
    data_root = tmp_path / "data"
    repository.mkdir()
    data_root.mkdir()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    project_service = ProjectService(
        lambda: PostgresUnitOfWork(session_factory),
        repository_inspector=StableInspector(repository),
        data_root=data_root,
    )
    project = await project_service.register(
        actor=actor,
        idempotency_key="integration-project",
        request=ProjectRegistrationRequest(
            name="Forge",
            repository_path=str(repository),
            github_repository="owner/repo",
            default_branch="main",
        ),
    )

    task_service = TaskService(lambda: PostgresUnitOfWork(session_factory))
    task = await task_service.create_plain_text(
        actor=actor,
        idempotency_key="integration-task",
        request=PlainTextTaskRequest(
            project_id=project.id,
            title="Exact\r\nTitle",
            body="Body\r\ntext",
        ),
    )
    replay = await task_service.create_plain_text(
        actor=actor,
        idempotency_key="integration-task",
        request=PlainTextTaskRequest(
            project_id=project.id,
            title="Exact\r\nTitle",
            body="Body\r\ntext",
        ),
    )

    assert replay.id == task.id
    assert task.title == "Exact\r\nTitle"
    assert task.body == "Body\r\ntext"
    assert task.normalized_text == "Exact\nTitle\n\nBody\ntext"
    async with PostgresUnitOfWork(session_factory) as work:
        audit = await work.audit.list_for_subject(subject_type="task", subject_id=task.id)
        assert len(audit) == 1
        await work.commit()
