"""Safe wire schemas for immutable artifacts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

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


__all__ = ["ArtifactLineageResponse", "ArtifactResponse", "ArtifactTextResponse"]
