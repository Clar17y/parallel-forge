"""Closed, bounded domain models and invariants for epic decomposition."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.epic_brief import BriefContent
from forge.domain.epic_items import (
    GraphValidationError,
    ItemInput,
    validate_graph,
)
from forge.domain.operation import canonical_digest
from forge.domain.payload import validate_durable_payload

if TYPE_CHECKING:
    from forge.domain.epic_brainstorm import FrozenBriefContent


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _bounded(value: str, *, maximum: int = 8000) -> str:
    if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > maximum:
        raise ValueError("decomposition text is invalid or too large")
    validate_durable_payload(value)
    return value


def _brief_text(value: str, maximum: int) -> str:
    if not value.strip() or "\x00" in value or len(value) > maximum:
        raise ValueError("decomposition text is invalid or too large")
    validate_durable_payload(value)
    return value


def _digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid digest")
    return value


class DecompositionEvidence(_Closed):
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



class DecompositionConflict(ValueError):
    """The request conflicts with existing epic or job state."""


class DecompositionNotFound(ValueError):
    """The requested decomposition subject was not found."""


class DecompositionValidationError(ValueError):
    """Validation failure for decomposition proposals or items."""





class DecompositionProposal(_Closed):
    schema_version: Literal[1] = 1
    turn_id: UUID
    epic_id: UUID
    project_id: UUID
    brief_revision_id: UUID
    brief_digest: str
    items: tuple[ItemInput, ...] = Field(default=(), max_length=128)
    summary: str = ""
    problem: str = ""
    assumptions: tuple[str, ...] = Field(default=(), max_length=64)
    open_questions: tuple[str, ...] = Field(default=(), max_length=64)
    resolved_turn_ids: tuple[UUID, ...] = ()
    evidence: tuple[DecompositionEvidence, ...] = Field(default=(), max_length=16)

    _valid_brief_digest = field_validator("brief_digest")(_digest)

    @field_validator("summary", "problem")
    @classmethod
    def valid_text_fields(cls, value: str) -> str:
        if not value:
            return value
        return _bounded(value)

    @field_validator("assumptions", "open_questions")
    @classmethod
    def valid_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_brief_text(item, 5000) for item in value)

    @model_validator(mode="after")
    def validate_proposal(self) -> DecompositionProposal:
        if self.resolved_turn_ids:
            raise ValueError("proposal cannot resolve pending choices")
        if not self.problem and not self.summary:
            raise ValueError("decomposition proposal requires summary text")
        # Ensure problem is populated from summary for worker turn compatibility if empty
        if not self.problem and self.summary:
            object.__setattr__(self, "problem", self.summary)
        document = self.model_dump(mode="json")
        validate_durable_payload(document)
        if len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 262144:
            raise ValueError("decomposition proposal is too large")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def require_brief_sources(
    items: Iterable[ItemInput],
    brief: BriefContent | FrozenBriefContent,
) -> None:
    """Validate that all items reference only source requirements present in the accepted brief."""
    allowed_req_ids = {req.requirement_id for req in brief.requirements}
    for item in items:
        for ref in item.source_requirement_ids:
            if ref not in allowed_req_ids:
                raise DecompositionValidationError(
                    f"source requirement is missing from accepted brief: {ref}"
                )


def validate_decomposition_proposal(
    proposal: DecompositionProposal,
    brief: BriefContent | FrozenBriefContent,
) -> DecompositionProposal:
    """Validate a decomposition proposal against graph invariants and the accepted brief."""
    try:
        proposal = DecompositionProposal.model_validate(proposal.model_dump(mode="json"))
    except (TypeError, ValueError) as err:
        raise DecompositionValidationError("decomposition proposal is invalid") from err
    if not proposal.items:
        raise DecompositionValidationError("decomposition proposal has no items")
    if not set(brief.assumptions).issubset(proposal.assumptions) or not set(
        brief.open_questions
    ).issubset(proposal.open_questions):
        raise DecompositionValidationError("accepted brief choices must remain visible")

    # Validate graph structure (acyclic, bounds, no self-edges, unique items)
    try:
        validate_graph(list(proposal.items), adoption=False)
    except GraphValidationError as err:
        raise DecompositionValidationError(str(err)) from err

    # Validate source requirements against accepted brief
    require_brief_sources(proposal.items, brief)
    return proposal


__all__ = [
    "DecompositionConflict",
    "DecompositionEvidence",
    "DecompositionNotFound",
    "DecompositionProposal",
    "DecompositionValidationError",
    "require_brief_sources",
    "validate_decomposition_proposal",
]
