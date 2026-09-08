"""Safe wire schemas for immutable artifacts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge.domain.agent import ReviewDecision


class ArtifactLineageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    producer_type: str
    producer_id: UUID | None
    created_at: datetime
    parent_digests: list[str]


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str
    media_type: str
    byte_count: int
    schema_version: int
    created_at: datetime
    metadata: dict[str, object]
    truncated: bool
    original_byte_count: int
    truncation_policy: str
    lineage: list[ArtifactLineageResponse]


class ArtifactTextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str
    text: str


class ReviewArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str
    run_id: UUID
    producer_execution_id: UUID
    head_sha: str
    policy_version: int
    decision: ReviewDecision
    summary: str
    tested_claims: list[str]
    missing_evidence: list[str]


class ReviewerDiffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str
    run_id: UUID
    producer_execution_id: UUID
    validation_evidence_set_id: UUID
    head_sha: str
    policy_version: int
    diff_digest: str
    text: str
    original_byte_count: int
    truncated: bool


class ProtectionSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    strict_required_checks: bool
    merge_queue_enabled: bool
    actor_can_bypass: bool
    evidence_source: str = Field(min_length=1, max_length=4096)
    verified: bool
    required_check_names: list[str] = Field(max_length=100)


class MergeProtectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str
    protection_digest: str
    protection: ProtectionSnapshotResponse
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_ref: str
    observed_base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


__all__ = ["ArtifactLineageResponse", "ArtifactResponse", "ArtifactTextResponse"]
