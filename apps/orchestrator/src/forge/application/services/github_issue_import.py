"""Import immutable GitHub source with stable intent and transactional deduplication."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from forge.application.ports.github import GitHubPort
from forge.application.ports.tasks import TaskRecord
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.tasks import (
    ExternalTaskRequest,
    TaskService,
    TaskServiceError,
    TaskUnitOfWork,
    _digest,
    _resource_id,
)
from forge.domain.github import GitHubIssue


class GitHubIssueImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    issue_number: int = Field(ge=1, le=9007199254740991, strict=True)


class GitHubIssueImportService:
    def __init__(
        self, unit_of_work_factory: Callable[[], TaskUnitOfWork], github: GitHubPort
    ) -> None:
        self._work = unit_of_work_factory
        self._github = github

    async def import_issue(
        self, *, actor: AuthenticatedActor, idempotency_key: str, request: GitHubIssueImportRequest
    ) -> TaskRecord:
        intent = _digest(request.model_dump(mode="json"))
        task, repository = await self._persist(actor, idempotency_key, request, intent)
        if task is not None:
            return task
        # The preliminary reservation was rolled back. No DB lock spans network I/O.
        async with asyncio.timeout(180):
            issue = await self._github.get_issue(repository, request.issue_number)
        _validate_issue(issue, repository, request.issue_number)
        source = ExternalTaskRequest(
            project_id=request.project_id,
            title=issue.title,
            body=issue.body or "",
            external_source="github",
            external_id=str(request.issue_number),
            source_url=issue.source_url,
            source_updated_at=issue.updated_at,
        )
        task, _ = await self._persist(actor, idempotency_key, request, intent, source, repository)
        if task is None:
            raise TaskServiceError("GitHub issue import failed")
        return task

    async def _persist(
        self,
        actor: AuthenticatedActor,
        key: str,
        request: GitHubIssueImportRequest,
        intent: str,
        source: ExternalTaskRequest | None = None,
        expected_repository: str | None = None,
    ) -> tuple[TaskRecord | None, str]:
        async with self._work() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="task.import_github",
                scope=f"project:{request.project_id}",
                idempotency_key=key,
                request_digest=intent,
            )
            if receipt.is_replay:
                replay = await work.tasks.get(_resource_id(receipt))
                await work.commit()
                return replay, ""
            # Serialize imports for this project, including different keys for one issue.
            project = await work.projects.get(request.project_id, for_update=True)
            if expected_repository is not None and project.github_repository != expected_repository:
                raise TaskServiceError("GitHub issue repository changed")
            task = await work.tasks.find_external(
                request.project_id, "github", str(request.issue_number)
            )
            if task is None:
                if source is None:
                    return None, project.github_repository
                task = await work.tasks.create(task_id=uuid4(), **source.model_dump())
                await TaskService._append_audit(work, actor, receipt, task, intent)
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload={"id": str(task.id), "task_digest": task.task_digest},
                resource_kind="task",
                resource_id=task.id,
            )
            await work.commit()
            return task, project.github_repository


def _validate_issue(issue: GitHubIssue, repository: str, number: int) -> None:
    if (
        issue.number != number
        or not issue.title
        or issue.updated_at is None
        or issue.updated_at.tzinfo is None
        or issue.updated_at.utcoffset() is None
        or issue.source_url != f"https://github.com/{repository}/issues/{number}"
    ):
        raise TaskServiceError("GitHub issue import failed")


__all__ = ["GitHubIssueImportRequest", "GitHubIssueImportService"]
