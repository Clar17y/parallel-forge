"""Task HTTP schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from forge.application.ports.tasks import TaskRecord
from forge.application.services.tasks import PlainTextTaskRequest


class TaskCreateRequest(PlainTextTaskRequest):
    """Closed plain-text task creation body."""


TaskCreate = TaskCreateRequest


class TaskResponse(BaseModel):
    """Exact task source fields and their deterministic derived values."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    project_id: UUID
    title: str
    body: str
    source_url: str | None
    source_updated_at: datetime | None
    untrusted_external_content: bool
    normalized_text: str
    task_digest: str
    external_source: str | None
    external_id: str | None

    @classmethod
    def from_record(cls, task: TaskRecord) -> TaskResponse:
        return cls(
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
        )


__all__ = ["TaskCreate", "TaskCreateRequest", "TaskResponse"]
