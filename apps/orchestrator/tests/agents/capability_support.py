"""Deterministic fake source records; never installed-client evidence."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from forge.domain.capability_evidence import (
    CapabilityEvidenceManifest,
    CapabilityEvidenceScope,
    CapabilityProof,
    CapabilityProofKind,
    ResolvedCapabilityEvidence,
    capability_identity,
    encode_capability_evidence,
)


def fake_capability_evidence(
    *,
    scope: CapabilityEvidenceScope,
    client_version: str,
    executable_digest: str,
    client_home: str,
    account: str,
    verifier_id: str,
) -> ResolvedCapabilityEvidence:
    observed = datetime(2026, 9, 13, tzinfo=UTC)
    identity = capability_identity(
        scope=scope,
        client_version=client_version,
        executable_digest=executable_digest,
        client_home=client_home,
        account=account,
    )
    manifest = CapabilityEvidenceManifest(
        evidence_id=uuid5(NAMESPACE_URL, f"forge-fake:{identity.digest}:{verifier_id}"),
        identity=identity,
        verifier_id=verifier_id,
        verifier_version="1",
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
        proofs=tuple(
            CapabilityProof(kind=kind, artifact_digest=f"{index:x}" * 64)
            for index, kind in enumerate(CapabilityProofKind, start=3)
        ),
    )
    wire = encode_capability_evidence(manifest)
    return ResolvedCapabilityEvidence(
        manifest=manifest,
        artifact_digest=hashlib.sha256(wire).hexdigest(),
        revision=1,
    )


def bind_fake_capability_report[Report](
    report: Report,
    *,
    scope: CapabilityEvidenceScope,
    client_version: str,
    executable_digest: str,
    client_home: str,
    account: str,
    verifier_id: str,
) -> Report:
    """Bind a report to deterministic test evidence with no admission authority."""

    return replace(
        report,
        evidence=fake_capability_evidence(
            scope=scope,
            client_version=client_version,
            executable_digest=executable_digest,
            client_home=client_home,
            account=account,
            verifier_id=verifier_id,
        ),
    )


__all__ = ["bind_fake_capability_report", "fake_capability_evidence"]
