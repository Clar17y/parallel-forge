"""Closed, bounded identities for durable epic discovery."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.epic_decomposition import DecompositionProposal
from forge.domain.operation import canonical_digest
from forge.domain.payload import validate_durable_payload
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget


class BrainstormConflict(ValueError):
    """The request no longer has the authority it claims."""


class BrainstormNotFound(ValueError):
    """The requested subject does not exist under this epic."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _bounded(value: str, *, maximum: int = 8000) -> str:
    if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > maximum:
        raise ValueError("brainstorm text is invalid or too large")
    validate_durable_payload(value)
    return value


def _brief_text(value: str, maximum: int) -> str:
    if not value.strip() or "\x00" in value or len(value) > maximum:
        raise ValueError("brief text is invalid or too large")
    validate_durable_payload(value)
    return value


class BrainstormTurn(_Closed):
    schema_version: Literal[1] = 1
    turn_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    role: Literal["operator", "assistant"]
    text: str
    pending: bool = False
    proposal: BrainstormProposal | DecompositionProposal | None = None

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return _bounded(value)


class BrainstormEvidence(_Closed):
    schema_version: Literal[1] = 1
    path: str
    content_digest: str
    excerpt: str

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            not value
            or value.startswith("/")
            or ":" in value
            or any(
                part in ("", ".", "..") or part.startswith(".") for part in normalized.split("/")
            )
            or len(value.encode("utf-8")) > 512
        ):
            raise ValueError("invalid evidence path")
        return normalized

    @field_validator("excerpt")
    @classmethod
    def valid_excerpt(cls, value: str) -> str:
        return _bounded(value, maximum=2048)

    @field_validator("content_digest")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("invalid evidence digest")
        return value


class FrozenRequirement(_Closed):
    requirement_id: UUID
    text: str
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return _brief_text(value, 5000)

    @field_validator("acceptance_criteria")
    @classmethod
    def valid_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_brief_text(item, 5000) for item in value)


class FrozenBriefContent(_Closed):
    schema_version: Literal[1] = 1
    problem: str = ""
    outcomes: tuple[str, ...] = Field(default=(), max_length=64)
    scope: tuple[str, ...] = Field(default=(), max_length=64)
    exclusions: tuple[str, ...] = Field(default=(), max_length=64)
    requirements: tuple[FrozenRequirement, ...] = Field(default=(), max_length=64)
    decisions: tuple[str, ...] = Field(default=(), max_length=64)
    assumptions: tuple[str, ...] = Field(default=(), max_length=64)
    open_questions: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("problem")
    @classmethod
    def valid_problem(cls, value: str) -> str:
        if len(value) > 10000 or "\x00" in value:
            raise ValueError("brief problem is too large")
        return value

    @field_validator(
        "outcomes", "scope", "exclusions", "decisions", "assumptions", "open_questions"
    )
    @classmethod
    def valid_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_brief_text(item, 5000) for item in value)

    @model_validator(mode="after")
    def producer_document_limit(self) -> FrozenBriefContent:
        document = self.model_dump(mode="json")
        validate_durable_payload(document)
        if (
            len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            > 131072
        ):
            raise ValueError("brief document is too large")
        return self


class BrainstormProposal(_Closed):
    schema_version: Literal[1] = 1
    turn_id: UUID
    problem: str
    outcomes: tuple[str, ...] = Field(default=(), max_length=32)
    scope: tuple[str, ...] = Field(default=(), max_length=32)
    exclusions: tuple[str, ...] = Field(default=(), max_length=32)
    requirements: tuple[str, ...] = Field(default=(), max_length=32)
    requirement_criteria: dict[str, tuple[str, ...]] = Field(default_factory=dict, max_length=32)
    decisions: tuple[str, ...] = Field(default=(), max_length=32)
    assumptions: tuple[str, ...] = Field(default=(), max_length=32)
    open_questions: tuple[str, ...] = Field(default=(), max_length=32)
    resolved_turn_ids: tuple[UUID, ...] = ()
    evidence: tuple[BrainstormEvidence, ...] = Field(default=(), max_length=16)

    @field_validator("problem")
    @classmethod
    def valid_problem(cls, value: str) -> str:
        return _bounded(value)

    @field_validator(
        "outcomes",
        "scope",
        "exclusions",
        "requirements",
        "decisions",
        "assumptions",
        "open_questions",
    )
    @classmethod
    def valid_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_bounded(item, maximum=2048) for item in value)

    @model_validator(mode="after")
    def no_silent_resolution(self) -> BrainstormProposal:
        if self.resolved_turn_ids:
            raise ValueError("proposal cannot resolve pending choices")
        if set(self.requirement_criteria) - set(self.requirements):
            raise ValueError("criteria require a proposed requirement")
        for values in self.requirement_criteria.values():
            if len(values) > 64:
                raise ValueError("too many criteria")
            for value in values:
                _bounded(value, maximum=5000)
        validate_durable_payload(self.model_dump(mode="json"))
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


BrainstormTurn.model_rebuild()


class AuthoringJobSnapshot(_Closed):
    schema_version: Literal[1] = 1
    job_id: UUID
    epic_id: UUID
    project_id: UUID
    conversation_id: UUID
    kind: Literal["brainstorm", "decomposition"] = "brainstorm"
    input_brief_revision_id: UUID | None
    input_brief_digest: str | None
    input_draft_digest: str
    draft_content: FrozenBriefContent = Field(default_factory=FrozenBriefContent)
    accepted_content: FrozenBriefContent | None = None
    input_graph_revision_id: UUID | None = None
    input_graph_digest: str | None = None
    expected_epic_version: int = Field(ge=1)
    conversation_version: int = Field(ge=1)
    prompt_turn_id: UUID
    profile_id: UUID | None = None
    profile_version: int | None = None
    route: RouteBinding
    budget: TaskBudget
    reservation_id: UUID

    @field_validator("input_brief_digest", "input_draft_digest", "input_graph_digest")
    @classmethod
    def valid_input_digest(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("invalid input digest")
        return value

    @model_validator(mode="after")
    def valid_profile_binding(self) -> AuthoringJobSnapshot:
        if (self.profile_id is None) != (self.profile_version is None) or (
            self.profile_version is not None and self.profile_version < 1
        ):
            raise ValueError("authoring profile binding is invalid")
        if (self.input_graph_revision_id is None) != (self.input_graph_digest is None):
            raise ValueError("authoring graph binding is invalid")
        return self


def validate_invocation_context(
    snapshot: AuthoringJobSnapshot, turns: tuple[BrainstormTurn, ...]
) -> None:
    """Reject oversized provider input while retaining complete durable history."""
    payload = {
        "draft": snapshot.draft_content.model_dump(mode="json"),
        "accepted": snapshot.accepted_content.model_dump(mode="json")
        if snapshot.accepted_content
        else None,
        "turns": [turn.model_dump(mode="json") for turn in turns],
    }
    if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 131072:
        raise BrainstormConflict("authoring invocation context exceeds limit")


class AuthoringReceipt(_Closed):
    schema_version: Literal[1] = 1
    job_id: UUID
    job_version: int = Field(ge=1)
    state: Literal[
        "queued",
        "running",
        "quota_wait",
        "capacity_wait",
        "cancel_requested",
        "cancelled",
        "proposed",
        "failed",
        "reconciling",
    ]
    replay_key: str


type BrainstormDimension = Literal[
    "duration_ms", "tool_call_count", "input_tokens", "output_tokens", "estimated_api_cost_minor"
]
type BrainstormState = Literal[
    "queued",
    "running",
    "quota_wait",
    "capacity_wait",
    "cancel_requested",
    "cancelled",
    "proposed",
    "failed",
    "reconciling",
]
type BrainstormFailure = Literal[
    "cancelled",
    "lost_result",
    "process_unsettled",
    "budget_exhausted",
    "quota_exhausted",
    "unavailable",
    "timeout",
    "invalid_output",
    "interrupted",
    "input_conflict",
]
_MAX_USAGE = 2**63 - 1


def validate_brainstorm_budget(budget: TaskBudget) -> None:
    """Reject limits that cannot be represented in the frozen usage record."""
    limits = (
        budget.max_duration_seconds * 1000,
        budget.max_tool_calls,
        budget.max_input_tokens,
        budget.max_output_tokens,
        budget.max_cost_minor,
    )
    if any(value is not None and value > _MAX_USAGE for value in limits):
        raise BrainstormConflict("brainstorm budget exceeds supported usage range")


class BrainstormMeasuredUsage(_Closed):
    schema_version: Literal[1] = 1
    duration_ms: int | None = Field(default=None, ge=0, le=_MAX_USAGE, strict=True)
    duration_lower_bound_ms: int = Field(default=0, ge=0, le=_MAX_USAGE, strict=True)
    tool_call_count: int = Field(ge=0, le=_MAX_USAGE, strict=True)
    input_tokens: int | None = Field(default=None, ge=0, le=_MAX_USAGE, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=_MAX_USAGE, strict=True)
    estimated_api_cost_minor: int | None = Field(default=None, ge=0, le=_MAX_USAGE, strict=True)
    unknown_fields: tuple[BrainstormDimension, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def explicit_unknowns(self) -> BrainstormMeasuredUsage:
        if self.duration_ms is not None and self.duration_lower_bound_ms > self.duration_ms:
            raise ValueError("duration lower bound exceeds measured duration")
        missing = {
            name
            for name in ("duration_ms", "input_tokens", "output_tokens", "estimated_api_cost_minor")
            if getattr(self, name) is None
        }
        if (
            len(set(self.unknown_fields)) != len(self.unknown_fields)
            or set(self.unknown_fields) != missing
        ):
            raise ValueError("unknown usage dimensions must be explicit")
        return self


class BrainstormReservation(_Closed):
    schema_version: Literal[1] = 1
    duration_ms: int = Field(ge=0, le=_MAX_USAGE, strict=True)
    tool_call_count: int = Field(ge=0, le=_MAX_USAGE, strict=True)
    input_tokens: int | None = Field(default=None, ge=0, le=_MAX_USAGE, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=_MAX_USAGE, strict=True)
    estimated_api_cost_minor: int | None = Field(default=None, ge=0, le=_MAX_USAGE, strict=True)


class BrainstormAmounts(_Closed):
    schema_version: Literal[1] = 1
    duration_ms: int = Field(default=0, ge=0, le=_MAX_USAGE, strict=True)
    tool_call_count: int = Field(default=0, ge=0, le=_MAX_USAGE, strict=True)
    input_tokens: int = Field(default=0, ge=0, le=_MAX_USAGE, strict=True)
    output_tokens: int = Field(default=0, ge=0, le=_MAX_USAGE, strict=True)
    estimated_api_cost_minor: int = Field(default=0, ge=0, le=_MAX_USAGE, strict=True)


def duration_floor(usage: Mapping[str, object] | None) -> int:
    if not usage:
        return 0
    return max(
        (
            candidate
            for candidate in (
                usage.get("duration_ms"),
                usage.get("duration_lower_bound_ms"),
            )
            if type(candidate) is int and 0 <= candidate <= _MAX_USAGE
        ),
        default=0,
    )


class BrainstormHeldReasons(_Closed):
    schema_version: Literal[1] = 1
    duration_ms: Literal["unsettled_or_unknown"] | None = None
    tool_call_count: Literal["unsettled_or_unknown"] | None = None
    input_tokens: Literal["unsettled_or_unknown"] | None = None
    output_tokens: Literal["unsettled_or_unknown"] | None = None
    estimated_api_cost_minor: Literal["unsettled_or_unknown"] | None = None


class AuthoringOutcome(_Closed):
    schema_version: Literal[1] = 1
    job_id: UUID
    job_version: int = Field(ge=1)
    state: BrainstormState
    route: RouteSpec | None = None
    proposal_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", max_length=64)
    proposal: BrainstormProposal | DecompositionProposal | None = None
    adopted_revision_id: UUID | None = None
    failure: BrainstormFailure | None = None
    usage_known: bool | None = None
    process_settled: bool = False
    usage: BrainstormMeasuredUsage | None = None
    reservation: BrainstormReservation | None = None
    cumulative_usage: BrainstormAmounts = Field(default_factory=BrainstormAmounts)
    held_reservations: BrainstormAmounts = Field(default_factory=BrainstormAmounts)
    uncertain_attempts: int = Field(default=0, ge=0, le=_MAX_USAGE, strict=True)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$", max_length=3)
    unknown_usage_fields: tuple[BrainstormDimension, ...] = Field(default=(), max_length=4)
    held_reasons: BrainstormHeldReasons = Field(default_factory=BrainstormHeldReasons)

    @model_validator(mode="after")
    def consistent_unknowns(self) -> AuthoringOutcome:
        if self.unknown_usage_fields != (self.usage.unknown_fields if self.usage else ()):
            raise ValueError("outcome unknown usage fields conflict")
        return self


class BrainstormThread(_Closed):
    conversation_id: UUID
    conversation_version: int = Field(ge=1)
    job_ids: tuple[UUID, ...] = ()


__all__ = [
    "AuthoringJobSnapshot",
    "AuthoringOutcome",
    "AuthoringReceipt",
    "BrainstormAmounts",
    "BrainstormConflict",
    "BrainstormEvidence",
    "BrainstormHeldReasons",
    "BrainstormMeasuredUsage",
    "BrainstormNotFound",
    "BrainstormProposal",
    "BrainstormReservation",
    "BrainstormThread",
    "BrainstormTurn",
    "FrozenBriefContent",
    "FrozenRequirement",
    "duration_floor",
    "validate_brainstorm_budget",
    "validate_invocation_context",
]
