"""Trusted source boundary for current official-client capability evidence."""

from typing import Protocol
from uuid import UUID

from forge.domain.capability_evidence import (
    CapabilityEvidenceIdentity,
    ResolvedCapabilityEvidence,
)


class CapabilityEvidenceSourceError(RuntimeError):
    """Capability evidence could not be safely persisted or read."""


class CapabilityEvidenceUnavailable(CapabilityEvidenceSourceError):
    """No current artifact-verified evidence matches the exact identity."""


class CapabilityEvidenceMissing(CapabilityEvidenceUnavailable):
    """No evidence record exists for the exact identity."""


class CapabilityEvidenceInvalid(CapabilityEvidenceUnavailable):
    """Evidence exists but is stale, replaced, or cannot be verified."""


class CapabilityEvidenceConflict(CapabilityEvidenceSourceError):
    """An immutable evidence identity or replay differs."""


class CapabilityEvidenceSource(Protocol):
    async def resolve(
        self,
        identity: CapabilityEvidenceIdentity,
        *,
        evidence_id: UUID | None = None,
    ) -> ResolvedCapabilityEvidence: ...


__all__ = [
    "CapabilityEvidenceConflict",
    "CapabilityEvidenceInvalid",
    "CapabilityEvidenceMissing",
    "CapabilityEvidenceSource",
    "CapabilityEvidenceSourceError",
    "CapabilityEvidenceUnavailable",
]
