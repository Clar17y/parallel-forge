"""Read/write boundary for safe, non-authoritative capability diagnostics."""

from datetime import datetime
from typing import Protocol

from forge.domain.capability_diagnostics import CapabilityProbeDiagnostic
from forge.domain.capability_evidence import CapabilityEvidenceIdentity
from forge.domain.subscription_readiness import ReadinessReason


class CapabilityProbeDiagnosticSource(Protocol):
    async def resolve(
        self, identity: CapabilityEvidenceIdentity
    ) -> CapabilityProbeDiagnostic | None: ...


class CapabilityProbeDiagnosticSink(CapabilityProbeDiagnosticSource, Protocol):
    async def report(
        self,
        identity: CapabilityEvidenceIdentity,
        reason: ReadinessReason,
        *,
        observed_at: datetime,
        expires_at: datetime,
    ) -> CapabilityProbeDiagnostic: ...


__all__ = ["CapabilityProbeDiagnosticSink", "CapabilityProbeDiagnosticSource"]
