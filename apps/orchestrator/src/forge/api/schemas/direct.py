"""Direct-run HTTP schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge.domain.direct import DirectRunRecord, DirectSessionRecord


class DirectRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    task: str = Field(min_length=1, max_length=65536)
    client: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=255)
    effort: str | None = Field(default=None, max_length=32)


class DirectSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    state: str
    client: str
    model: str
    effort: str | None
    pid: int | None
    output_bytes: int
    exit_code: int | None
    started_at: datetime | None
    ended_at: datetime | None
    last_output_at: datetime | None
    failure_reason: str | None

    @classmethod
    def from_record(cls, record: DirectSessionRecord) -> DirectSessionResponse:
        return cls(
            id=record.id, run_id=record.run_id, state=record.state, client=record.client,
            model=record.model, effort=record.effort, pid=record.pid,
            output_bytes=record.output_bytes, exit_code=record.exit_code,
            started_at=record.started_at, ended_at=record.ended_at,
            last_output_at=record.last_output_at, failure_reason=record.failure_reason,
        )


class DirectRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    project_id: UUID
    task: str
    client: str
    model: str
    effort: str | None
    state: str
    branch: str | None
    worktree_path: str | None
    base_sha: str | None
    head_sha: str | None
    summary: str | None
    question: str | None
    options: tuple[str, ...]
    failure_reason: str | None
    result_degraded: bool
    cancel_requested: bool
    created_at: datetime | None
    updated_at: datetime | None
    sessions: tuple[DirectSessionResponse, ...] = ()

    @classmethod
    def from_record(
        cls,
        record: DirectRunRecord,
        sessions: tuple[DirectSessionRecord, ...] = (),
    ) -> DirectRunResponse:
        return cls(
            id=record.id, project_id=record.project_id, task=record.task_text,
            client=record.client, model=record.model, effort=record.effort,
            state=record.state, branch=record.branch, worktree_path=record.worktree_path,
            base_sha=record.base_sha, head_sha=record.head_sha, summary=record.summary,
            question=record.question, options=record.options,
            failure_reason=record.failure_reason, result_degraded=record.result_degraded,
            cancel_requested=record.cancel_requested, created_at=record.created_at,
            updated_at=record.updated_at,
            sessions=tuple(DirectSessionResponse.from_record(s) for s in sessions),
        )


class DirectRouteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client: str
    model: str
    effort: str | None


class DirectTranscriptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class DirectDiffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diff: str


__all__ = [
    "DirectDiffResponse",
    "DirectRouteResponse",
    "DirectRunCreateRequest",
    "DirectRunResponse",
    "DirectSessionResponse",
    "DirectTranscriptResponse",
]
