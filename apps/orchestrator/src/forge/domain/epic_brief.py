"""Bounded epic briefs and immutable revision identities."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.payload import validate_durable_payload


class EpicBriefError(RuntimeError):
    """A safe epic brief domain failure."""


class EpicNotFound(EpicBriefError):
    """Epic was not found."""


class BriefRevisionNotFound(EpicBriefError):
    """Revision was not found under the epic."""


class EpicVersionConflict(EpicBriefError):
    """The expected epic version is stale."""


class BriefBindingConflict(EpicBriefError):
    """The selected revision or digest does not match."""


class BriefNotAccepted(EpicBriefError):
    """The epic has no accepted brief."""


def _text(value: str, maximum: int, *, blank: bool = False) -> str:
    if "\x00" in value or len(value) > maximum or (not blank and not value.strip()):
        raise ValueError("brief text is invalid")
    validate_durable_payload(value)
    return value


class BriefRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: UUID
    text: str
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        return _text(value, 5000)

    @field_validator("acceptance_criteria")
    @classmethod
    def bounded_criteria(cls, value: list[str]) -> list[str]:
        return [_text(item, 5000) for item in value]


class BriefContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    problem: str = ""
    outcomes: list[str] = Field(default_factory=list, max_length=64)
    scope: list[str] = Field(default_factory=list, max_length=64)
    exclusions: list[str] = Field(default_factory=list, max_length=64)
    requirements: list[BriefRequirement] = Field(default_factory=list, max_length=64)
    decisions: list[str] = Field(default_factory=list, max_length=64)
    assumptions: list[str] = Field(default_factory=list, max_length=64)
    open_questions: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("problem")
    @classmethod
    def bounded_problem(cls, value: str) -> str:
        return _text(value, 10000, blank=True)

    @field_validator(
        "outcomes", "scope", "exclusions", "decisions", "assumptions", "open_questions"
    )
    @classmethod
    def bounded_items(cls, value: list[str]) -> list[str]:
        return [_text(item, 5000) for item in value]

    @model_validator(mode="after")
    def validate_content(self) -> BriefContent:
        ids = [requirement.requirement_id for requirement in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("brief requirement identifiers must be unique")
        document = self.model_dump(
            mode="json",
            include={
                "schema_version",
                "problem",
                "outcomes",
                "scope",
                "exclusions",
                "requirements",
                "decisions",
                "assumptions",
                "open_questions",
            },
        )
        validate_durable_payload(document)
        if (
            len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            > 131072
        ):
            raise ValueError("brief document is too large")
        return self

    def require_adoptable(self) -> None:
        if (
            not self.problem.strip()
            or not self.outcomes
            or not self.requirements
            or any(not item.acceptance_criteria for item in self.requirements)
        ):
            raise ValueError("brief revision is incomplete")


class EpicRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    epic_id: UUID
    project_id: UUID
    version: int = Field(ge=1, strict=True)
    title: str
    draft: BriefContent
    accepted_brief_revision_id: UUID | None
    accepted_brief_digest: str | None
    accepted_graph_revision_id: UUID | None
    accepted_graph_digest: str | None
    created_at: datetime
    updated_at: datetime


class BriefRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    brief_revision_id: UUID
    epic_id: UUID
    revision_number: int = Field(ge=1, strict=True)
    epic_version: int = Field(ge=1, strict=True)
    content_digest: str
    source_job_id: UUID | None
    content: BriefContent
    created_at: datetime


class AcceptedBrief(BriefContent):
    epic_id: UUID
    project_id: UUID
    epic_version: int = Field(ge=1, strict=True)
    brief_revision_id: UUID
    brief_digest: str


class EpicCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    project_id: UUID
    title: str
    draft: BriefContent = Field(default_factory=BriefContent)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 256:
            raise ValueError("epic title is too long")
        return _text(value, 256)


class EpicDraftUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    expected_epic_version: int = Field(ge=1, strict=True)
    title: str
    draft: BriefContent

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        return EpicCreateRequest.valid_title(value)


class BriefRevisionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    expected_epic_version: int = Field(ge=1, strict=True)
    content: BriefContent


class BriefAdoptionRequest(BaseModel):
    """Bind a complete immutable revision and its exact digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    expected_epic_version: int = Field(ge=1, strict=True)
    brief_revision_id: UUID
    brief_digest: str

    @field_validator("brief_digest")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("brief digest is invalid")
        return value


__all__ = [
    "AcceptedBrief",
    "BriefAdoptionRequest",
    "BriefBindingConflict",
    "BriefContent",
    "BriefNotAccepted",
    "BriefRequirement",
    "BriefRevisionCreateRequest",
    "BriefRevisionNotFound",
    "BriefRevisionRecord",
    "EpicCreateRequest",
    "EpicDraftUpdateRequest",
    "EpicNotFound",
    "EpicRecord",
    "EpicVersionConflict",
]
