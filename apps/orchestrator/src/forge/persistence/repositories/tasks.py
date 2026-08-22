"""PostgreSQL repository for immutable task source artifacts."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.tasks import TaskRecord
from forge.persistence.models import Project, Task

MAX_TITLE_BYTES = 512
MAX_BODY_BYTES = 1_048_576


class TaskRepositoryError(RuntimeError):
    """Base for safe task persistence errors."""


class TaskNotFound(TaskRepositoryError):
    """The requested task does not exist."""


class TaskProjectNotFound(TaskRepositoryError):
    """The task project does not exist."""


class TaskIdentityConflict(TaskRepositoryError):
    """An immutable external task identity is already registered."""


class PostgresTaskRepository:
    """Persist exact source fields and deterministic derived task text."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        task_id: UUID,
        project_id: UUID,
        title: str,
        body: str,
        source_url: str | None = None,
        source_updated_at: datetime | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
    ) -> TaskRecord:
        """Insert one plain or untrusted external task source."""

        _validate_source(title, body, source_url, source_updated_at, external_source, external_id)
        project = await self._session.get(Project, project_id)
        if project is None:
            raise TaskProjectNotFound("task project was not found")
        if await self._session.get(Task, task_id) is not None:
            raise TaskIdentityConflict("task identity is already registered")
        if external_source is not None:
            existing = (
                await self._session.execute(
                    select(Task.id).where(
                        Task.project_id == project_id,
                        Task.external_source == external_source,
                        Task.external_id == external_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise TaskIdentityConflict("external task identity is already registered")

        normalized = derive_normalized_text(title, body)
        digest = compute_task_digest(
            title=title,
            body=body,
            source_url=source_url,
            source_updated_at=source_updated_at,
            external_source=external_source,
            external_id=external_id,
        )
        stored = Task(
            id=task_id,
            project_id=project_id,
            title=title,
            body=body,
            source_url=source_url,
            source_updated_at=source_updated_at,
            untrusted_external_content=external_source is not None,
            normalized_text=normalized,
            task_digest=digest,
            external_source=external_source,
            external_id=external_id,
        )
        self._session.add(stored)
        await self._session.flush()
        return _task_from_record(stored)

    async def get(self, task_id: UUID) -> TaskRecord:
        """Load one immutable task source."""

        task = await self._session.get(Task, task_id)
        if task is None:
            raise TaskNotFound("task was not found")
        return _task_from_record(task)

    async def list(self, project_id: UUID) -> Sequence[TaskRecord]:
        """List one project's tasks in deterministic creation order."""

        result = await self._session.execute(
            select(Task).where(Task.project_id == project_id).order_by(Task.created_at, Task.id)
        )
        return [_task_from_record(task) for task in result.scalars().all()]


def derive_normalized_text(title: str, body: str) -> str:
    """Normalize line endings and Unicode without changing stored sources."""

    normalized_title = _normalize_text(title)
    normalized_body = _normalize_text(body)
    return normalized_title if not normalized_body else f"{normalized_title}\n\n{normalized_body}"


def compute_task_digest(
    *,
    title: str,
    body: str,
    source_url: str | None,
    source_updated_at: datetime | None,
    external_source: str | None,
    external_id: str | None,
) -> str:
    """Bind exact source fields and external identity to a SHA-256 digest."""

    canonical = {
        "title": title,
        "body": body,
        "source_url": source_url,
        "source_updated_at": _canonical_timestamp(source_updated_at),
        "external_source": external_source,
        "external_id": external_id,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _task_from_record(task: Task) -> TaskRecord:
    return TaskRecord(
        id=task.id,
        project_id=task.project_id,
        title=task.title,
        body=task.body,
        source_url=task.source_url,
        source_updated_at=task.source_updated_at,
        untrusted_external_content=task.untrusted_external_content,
        normalized_text=task.normalized_text,
        task_digest=task.task_digest,
        external_source=task.external_source,
        external_id=task.external_id,
        created_at=task.created_at,
    )


def _validate_source(
    title: str,
    body: str,
    source_url: str | None,
    source_updated_at: datetime | None,
    external_source: str | None,
    external_id: str | None,
) -> None:
    if not isinstance(title, str) or not title.strip():
        raise ValueError("task title must not be blank")
    if len(title.encode("utf-8")) > MAX_TITLE_BYTES:
        raise ValueError("task title is too long")
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("task body is too long")
    if source_url is not None and len(source_url.encode("utf-8")) > 2048:
        raise ValueError("task source URL is too long")
    if source_updated_at is not None and (
        source_updated_at.tzinfo is None or source_updated_at.utcoffset() is None
    ):
        raise ValueError("task source update time must be timezone-aware")
    if (external_source is None) != (external_id is None):
        raise ValueError("external source and identifier must be provided together")
    if external_source is None and (source_url is not None or source_updated_at is not None):
        raise ValueError("plain tasks cannot carry external source metadata")
    if external_source is not None and (
        not external_source.strip() or not external_id or not external_id.strip()
    ):
        raise ValueError("external source identity must not be blank")
    if external_source is not None and len(external_source) > 64:
        raise ValueError("external source identity is too long")
    if external_id is not None and len(external_id) > 255:
        raise ValueError("external task identifier is too long")


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _canonical_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


__all__ = [
    "MAX_BODY_BYTES",
    "MAX_TITLE_BYTES",
    "PostgresTaskRepository",
    "TaskIdentityConflict",
    "TaskNotFound",
    "TaskProjectNotFound",
    "TaskRepositoryError",
    "compute_task_digest",
    "derive_normalized_text",
]
