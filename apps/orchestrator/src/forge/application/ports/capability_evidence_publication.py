"""Publication boundary for newly observed, immutable capability evidence."""

from __future__ import annotations

from typing import Protocol

from forge.domain.capability_evidence import CapabilityEvidenceManifest, ResolvedCapabilityEvidence


class CapabilityEvidencePublisher(Protocol):
    """Persist a complete manifest after its referenced artifacts exist."""

    async def publish(self, manifest: CapabilityEvidenceManifest) -> ResolvedCapabilityEvidence: ...


__all__ = ["CapabilityEvidencePublisher"]
