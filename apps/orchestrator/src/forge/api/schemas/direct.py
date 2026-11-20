"""Direct-run HTTP schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge.domain.direct import (
    DirectCheckRecord,
    DirectChildTaskRecord,
    DirectReviewRecord,
    DirectRunRecord,
    DirectSessionRecord,
)


class DirectRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    task: str = Field(min_length=1, max_length=65536)
    client: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=255)
    effort: str | None = Field(default=None, max_length=32)
    shape: Literal["quick", "planned", "epic"] = "quick"
    review_plan_first: bool = False


class DirectTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    ordinal: int
    task: str
    shape: str
    state: str
    plan_text: str | None = None
    head_sha: str | None = None
    summary: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    plan_approved: bool = False

    @classmethod
    def from_record(cls, record: DirectChildTaskRecord) -> DirectTaskResponse:
        return cls(
            id=record.id,
            run_id=record.run_id,
            ordinal=record.ordinal,
            task=record.task_text,
            plan_approved=getattr(record, "plan_approved", False),
            shape=record.shape,
            state=record.state,
            plan_text=record.plan_text,
            head_sha=record.head_sha,
            summary=record.summary,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class DirectTaskItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    task: str = Field(min_length=1, max_length=65536)
    shape: Literal["quick", "planned"] = "quick"


class DirectTasksUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    tasks: list[DirectTaskItemUpdate] = Field(min_length=1, max_length=20)


class DirectApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=0)
    task_id: UUID | None = None


class DirectCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    round: int
    command_name: str
    kind: str
    tree_sha: str
    command_digest: str
    policy_version: int
    status: str
    exit_code: int | None
    duration_ms: int | None
    failure_excerpt: str | None
    created_at: datetime | None
    task_id: UUID | None = None

    @classmethod
    def from_record(cls, record: DirectCheckRecord) -> DirectCheckResponse:
        return cls(
            id=record.id, run_id=record.run_id, round=record.round,
            command_name=record.command_name, kind=record.kind,
            tree_sha=record.tree_sha, command_digest=record.command_digest,
            policy_version=record.policy_version, status=record.status,
            exit_code=record.exit_code, duration_ms=record.duration_ms,
            failure_excerpt=record.failure_excerpt, created_at=record.created_at,
            task_id=getattr(record, "task_id", None),
        )


class DirectFindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str
    file: str | None = None
    detail: str


class DirectReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    session_id: UUID
    round: int
    client: str
    model: str
    verdict: str
    findings: tuple[DirectFindingResponse, ...] = ()
    summary: str | None
    degraded: bool
    reviewer_modified_worktree: bool
    created_at: datetime | None
    task_id: UUID | None = None

    @classmethod
    def from_record(cls, record: DirectReviewRecord) -> DirectReviewResponse:
        return cls(
            id=record.id, run_id=record.run_id, session_id=record.session_id,
            round=record.round, client=record.client, model=record.model,
            verdict=record.verdict,
            findings=tuple(
                DirectFindingResponse(
                    severity=str(f.get("severity", "")), file=f.get("file"),  # type: ignore[arg-type]
                    detail=str(f.get("detail", "")),
                )
                for f in record.findings
            ),
            summary=record.summary, degraded=record.degraded,
            reviewer_modified_worktree=record.reviewer_modified_worktree,
            created_at=record.created_at,
            task_id=getattr(record, "task_id", None),
        )


class DirectSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    state: str
    kind: str
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
    task_id: UUID | None = None

    @classmethod
    def from_record(cls, record: DirectSessionRecord) -> DirectSessionResponse:
        return cls(
            id=record.id, run_id=record.run_id, state=record.state,
            kind=getattr(record, "kind", "implement"), client=record.client,
            model=record.model, effort=record.effort, pid=record.pid,
            output_bytes=record.output_bytes, exit_code=record.exit_code,
            started_at=record.started_at, ended_at=record.ended_at,
            last_output_at=record.last_output_at, failure_reason=record.failure_reason,
            task_id=getattr(record, "task_id", None),
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
    pr_number: int | None = None
    pr_url: str | None = None
    pr_rounds: int = 0
    shape: str = "quick"
    review_plan_first: bool = False
    plan_text: str | None = None
    plan_approved: bool = False
    control_version: int = 0
    approval_task_id: UUID | None = None
    owner_actions: tuple[dict[str, object], ...] = ()
    created_at: datetime | None
    updated_at: datetime | None
    sessions: tuple[DirectSessionResponse, ...] = ()
    checks: tuple[DirectCheckResponse, ...] = ()
    reviews: tuple[DirectReviewResponse, ...] = ()
    tasks: tuple[DirectTaskResponse, ...] = ()

    @classmethod
    def from_record(
        cls,
        record: DirectRunRecord,
        sessions: tuple[DirectSessionRecord, ...] = (),
        checks: tuple[DirectCheckRecord, ...] = (),
        reviews: tuple[DirectReviewRecord, ...] = (),
        tasks: tuple[DirectChildTaskRecord, ...] = (),
    ) -> DirectRunResponse:
        return cls(
            id=record.id, project_id=record.project_id, task=record.task_text,
            client=record.client, model=record.model, effort=record.effort,
            state=record.state, branch=record.branch, worktree_path=record.worktree_path,
            base_sha=record.base_sha, head_sha=record.head_sha, summary=record.summary,
            question=record.question, options=record.options,
            failure_reason=record.failure_reason, result_degraded=record.result_degraded,
            cancel_requested=record.cancel_requested,
            pr_number=record.pr_number, pr_url=record.pr_url, pr_rounds=record.pr_rounds,
            shape=getattr(record, "shape", "quick"),
            review_plan_first=getattr(record, "review_plan_first", False),
            plan_text=getattr(record, "plan_text", None),
            plan_approved=getattr(record, "plan_approved", False),
            control_version=getattr(record, "control_version", 0),
            approval_task_id=getattr(record, "approval_task_id", None),
            owner_actions=getattr(record, "owner_actions", ()),
            created_at=record.created_at,
            updated_at=record.updated_at,
            sessions=tuple(DirectSessionResponse.from_record(s) for s in sessions),
            checks=tuple(DirectCheckResponse.from_record(c) for c in checks),
            reviews=tuple(DirectReviewResponse.from_record(r) for r in reviews),
            tasks=tuple(DirectTaskResponse.from_record(t) for t in tasks),
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
    "DirectCheckResponse",
    "DirectDiffResponse",
    "DirectFindingResponse",
    "DirectReviewResponse",
    "DirectRouteResponse",
    "DirectRunCreateRequest",
    "DirectRunResponse",
    "DirectSessionResponse",
    "DirectTaskItemUpdate",
    "DirectTaskResponse",
    "DirectTasksUpdateRequest",
    "DirectTranscriptResponse",
]
