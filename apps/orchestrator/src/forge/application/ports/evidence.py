"""Typed immutable evidence-set persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from forge.domain.artifact import ArtifactDescriptor
from forge.domain.evidence import (
    EvidenceManifest,
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    ValidationEvidenceMember,
)


class EvidenceKind(StrEnum):
    VALIDATION = "validation"
    REVIEW = "review"


class EvidenceInputPurpose(StrEnum):
    VALIDATION_RESULTS = "validation_results"
    PRIOR_REVIEW = "prior_review"


class EvidenceError(RuntimeError):
    pass


class EvidenceNotFound(EvidenceError):
    pass


class EvidenceConflict(EvidenceError):
    pass


class EvidenceCorruptLineage(EvidenceError):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceReadScope:
    run_id: UUID
    policy_version: int
    consumer_execution_id: UUID
    consumer_step_id: UUID
    head_sha: str


@dataclass(frozen=True, slots=True)
class EvidenceSetDescriptor:
    evidence_set_id: UUID
    run_id: UUID
    step_id: UUID
    kind: EvidenceKind
    policy_version: int
    head_sha: str
    producer_execution_id: UUID | None
    manifest_artifact_id: UUID
    manifest_digest: str
    manifest_media_type: str
    manifest_byte_count: int
    manifest_schema_version: int
    validation_evidence_set_id: UUID | None
    prior_review_evidence_set_id: UUID | None
    review_finding_ids: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class CanonicalEvidenceArtifact:
    descriptor: ArtifactDescriptor
    manifest: EvidenceManifest
    canonical_bytes: bytes


@dataclass(frozen=True, slots=True)
class ValidationProjectionMember:
    member: ValidationEvidenceMember
    output_artifact_id: UUID


@dataclass(frozen=True, slots=True)
class ValidationEvidenceDraft:
    manifest: ValidationEvidenceManifest
    members: tuple[ValidationProjectionMember, ...]


@dataclass(frozen=True, slots=True)
class ReviewEvidenceDraft:
    manifest: ReviewEvidenceManifest


class EvidenceRepository(Protocol):
    async def input_for_execution(
        self, purpose: EvidenceInputPurpose, scope: EvidenceReadScope
    ) -> EvidenceSetDescriptor | None: ...
    async def get_by_id(self, evidence_set_id: UUID, *, run_id: UUID) -> EvidenceSetDescriptor: ...
    async def record_set(
        self,
        draft: ValidationEvidenceDraft | ReviewEvidenceDraft,
        artifact: CanonicalEvidenceArtifact,
    ) -> EvidenceSetDescriptor: ...
    async def bind_input(
        self,
        consumer_execution_id: UUID,
        purpose: EvidenceInputPurpose,
        evidence_set_id: UUID,
        *,
        run_id: UUID,
    ) -> None: ...


__all__ = [
    "CanonicalEvidenceArtifact",
    "EvidenceConflict",
    "EvidenceCorruptLineage",
    "EvidenceError",
    "EvidenceInputPurpose",
    "EvidenceKind",
    "EvidenceNotFound",
    "EvidenceReadScope",
    "EvidenceRepository",
    "EvidenceSetDescriptor",
    "ReviewEvidenceDraft",
    "ValidationEvidenceDraft",
    "ValidationProjectionMember",
]
