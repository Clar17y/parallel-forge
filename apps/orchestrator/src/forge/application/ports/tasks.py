"""Task source application records and persistence contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """Exact immutable source fields plus derived agent-facing text."""

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
    created_at: datetime | None = None


class TaskRepository(Protocol):
    """Append-only task source persistence operations."""

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
    ) -> TaskRecord: ...

    async def get(self, task_id: UUID) -> TaskRecord: ...

    async def list(self, project_id: UUID) -> Sequence[TaskRecord]: ...


__all__ = ["TaskRecord", "TaskRepository"]
