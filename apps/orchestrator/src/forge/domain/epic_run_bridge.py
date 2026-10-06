"""Immutable epic execution and work-item attempt identities."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.payload import validate_durable_payload


class EpicLaunchConflict(RuntimeError):
    """A default workflow control blocks launch until an explicit owner action."""

    def __init__(self, blocker_codes: list[str], *, actual_epic_version: int) -> None:
        self.blocker_codes = tuple(blocker_codes)
        self.actual_epic_version = actual_epic_version
        super().__init__(",".join(blocker_codes))


class EpicAttemptNotFound(RuntimeError):
    """Attempt is not bound to the requested epic."""


class EpicExecutionNotFound(RuntimeError):
    """Execution snapshot does not exist."""


class EpicExecutionBindingConflict(RuntimeError):
    """Requested execution snapshot does not match the saved source."""


class LaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    expected_epic_version: int = Field(ge=1, strict=True)
    execution_id: UUID | None = None
    brief_revision_id: UUID
    brief_digest: str
    graph_revision_id: UUID
    graph_digest: str
    item_id: UUID
    profile_id: UUID | None = None
    profile_version: int | None = Field(default=None, ge=1, strict=True)
    owner_override: bool = Field(default=False, strict=True)
    override_note: str | None = Field(default=None, max_length=2048)

    @field_validator("brief_digest", "graph_digest")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("digest is invalid")
        return value


class ExecutionStartRequest(BaseModel):
    """Select and freeze one existing matching brief/graph pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    expected_epic_version: int = Field(ge=1, strict=True)
    brief_revision_id: UUID | None = None
    brief_digest: str | None = None
    graph_revision_id: UUID | None = None
    graph_digest: str | None = None
    owner_override: bool = Field(default=False, strict=True)
    override_note: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def selected_pair_is_complete(self) -> ExecutionStartRequest:
        selected = (
            self.brief_revision_id,
            self.brief_digest,
            self.graph_revision_id,
            self.graph_digest,
        )
        if any(value is not None for value in selected) and not all(
            value is not None for value in selected
        ):
            raise ValueError("selected source pair must be complete")
        for digest in (self.brief_digest, self.graph_digest):
            if digest is not None and (
                len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("digest is invalid")
        if self.override_note is not None:
            if not self.override_note.strip() or "\x00" in self.override_note:
                raise ValueError("note is invalid")
            validate_durable_payload(self.override_note)
        return self

    @field_validator("override_note")
    @classmethod
    def note_is_safe(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("note is invalid")
        validate_durable_payload(value)
        return value


class EpicExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: UUID
    epic_id: UUID
    brief_revision_id: UUID
    brief_digest: str
    graph_revision_id: UUID
    graph_digest: str
    created_at: datetime


class DependencyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: UUID
    status: Literal["verified", "unknown", "unverified"]
    predecessor_run_id: UUID | None = None
    integrated_sha: str | None = None

    @field_validator("integrated_sha")
    @classmethod
    def sha_is_hex(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 40 or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("integrated SHA is invalid")
        return value

    @model_validator(mode="after")
    def verified_requires_proof(self) -> DependencyEvidence:
        if self.status == "verified" and (
            self.predecessor_run_id is None or self.integrated_sha is None
        ):
            raise ValueError("verified dependency evidence needs run and integrated SHA")
        return self


class EpicAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    attempt_id: UUID
    execution_id: UUID
    epic_id: UUID
    item_id: UUID
    attempt_number: int = Field(ge=1, strict=True)
    item_disposition: Literal["required", "deferred"]
    actor_id: UUID
    expected_epic_version: int = Field(ge=1, strict=True)
    actual_epic_version: int = Field(ge=1, strict=True)
    task_id: UUID
    run_id: UUID
    brief_revision_id: UUID
    brief_digest: str
    graph_revision_id: UUID
    graph_digest: str
    item_digest: str
    context_digest: str
    task_digest: str
    base_ref: str
    base_sha: str
    owner_override: bool
    override_note: str | None
    blocker_codes: list[str]
    dependency_evidence: list[DependencyEvidence]
    created_at: datetime
