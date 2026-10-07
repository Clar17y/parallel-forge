"""Request and response schemas for epic decomposition endpoints."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge.domain.epic_items import ItemInput


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1


class DecompositionConversationCreate(_Request):
    project_id: UUID
    text: str = Field(min_length=1, max_length=8000)


class DecompositionTurnAppend(_Request):
    project_id: UUID
    expected_conversation_version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=8000)
    pending: bool = False


class DecompositionJobSubmit(_Request):
    project_id: UUID
    prompt_turn_id: UUID
    expected_epic_version: int = Field(ge=1)
    expected_conversation_version: int = Field(ge=1)


class DecompositionJobControl(_Request):
    project_id: UUID
    expected_job_version: int = Field(ge=1)


class DecompositionJobRetry(DecompositionJobControl):
    owner_override: bool = False
    override_note: str | None = Field(default=None, max_length=2048)


class DecompositionProposalAdopt(DecompositionJobControl):
    expected_epic_version: int = Field(ge=1)
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[ItemInput] | None = Field(default=None, max_length=128)


class DecompositionAdoptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    graph_revision_id: UUID
    graph_digest: str
    epic_version: int
    job_version: int


__all__ = [
    "DecompositionAdoptionResponse",
    "DecompositionConversationCreate",
    "DecompositionJobControl",
    "DecompositionJobSubmit",
    "DecompositionProposalAdopt",
    "DecompositionTurnAppend",
]
