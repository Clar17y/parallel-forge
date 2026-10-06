"""Closed operator request contracts for epic discovery."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1


class ConversationCreate(_Request):
    project_id: UUID
    text: str = Field(min_length=1, max_length=8000)


class TurnAppend(_Request):
    project_id: UUID
    expected_conversation_version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=8000)
    pending: bool = False


class JobSubmit(_Request):
    project_id: UUID
    prompt_turn_id: UUID
    expected_epic_version: int = Field(ge=1)
    expected_conversation_version: int = Field(ge=1)


class JobControl(_Request):
    project_id: UUID
    expected_job_version: int = Field(ge=1)


class JobRetry(JobControl):
    owner_override: bool = False
    override_note: str | None = Field(default=None, max_length=2048)


class ProposalAdopt(JobControl):
    expected_epic_version: int = Field(ge=1)
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
