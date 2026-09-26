"""Adversarial coverage for identity-bound capability proof envelopes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.capability_evidence import (
    CapabilityEvidenceIdentity,
    CapabilityEvidenceManifest,
    CapabilityProof,
    CapabilityProofKind,
)
from forge.domain.capability_proof import CapabilityProofError, encode_proof, validate_proof
from forge.domain.subscription import AuthMode, BillingMode, ReasoningEffort, SpecialistPurpose


def _manifest() -> CapabilityEvidenceManifest:
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    identity = CapabilityEvidenceIdentity(
        provider="openai",
        client="codex_app_server",
        client_version="0.153.4",
        executable_digest="a" * 64,
        client_home_digest="b" * 64,
        account="account-digest",
        model="gpt-6-astra",
        effort=ReasoningEffort.LOW,
        role=SpecialistPurpose.PRIMARY,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    return CapabilityEvidenceManifest(
        evidence_id=uuid4(),
        identity=identity,
        verifier_id="forge-codex-official",
        verifier_version="1",
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
        proofs=tuple(
            CapabilityProof(kind=kind, artifact_digest="0" * 64) for kind in CapabilityProofKind
        ),
    )


def _payload(manifest: CapabilityEvidenceManifest) -> dict[str, object]:
    return {
        "client": manifest.identity.client,
        "client_version": manifest.identity.client_version,
        "executable_digest": manifest.identity.executable_digest,
        "client_home_digest": manifest.identity.client_home_digest,
        "executable_unchanged": True,
        "reported_client_version": manifest.identity.client_version,
    }


def test_proof_requires_exact_semantics_and_canonical_wire() -> None:
    manifest = _manifest()
    wire = encode_proof(CapabilityProofKind.CLIENT_IDENTITY, _payload(manifest), manifest)
    validate_proof(wire, CapabilityProofKind.CLIENT_IDENTITY, manifest)
    changed = json.loads(wire)
    changed["payload"]["client"] = "other-client"
    with pytest.raises(CapabilityProofError):
        validate_proof(
            json.dumps(changed, sort_keys=True, separators=(",", ":")).encode(),
            CapabilityProofKind.CLIENT_IDENTITY,
            manifest,
        )
    with pytest.raises(CapabilityProofError):
        validate_proof(wire + b"\n", CapabilityProofKind.CLIENT_IDENTITY, manifest)


def test_proof_rejects_non_hex_observation_digest() -> None:
    manifest = _manifest()
    payload = {
        "model": manifest.identity.model,
        "effort": manifest.identity.effort.value,
        "catalog_supported": True,
        "turn_completed": True,
        "turn_observation_digest": "x" * 64,
    }

    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.ROUTE_IDENTITY, payload, manifest)


@pytest.mark.parametrize(
    "wire",
    [
        b'{"schema_version":NaN}',
        b'{"schema_version":1,"schema_version":1}',
        b"[1]",
        b"{" + b'"x":{' * 10 + b"0" + b"}" * 10 + b"}",
    ],
)
def test_proof_rejects_malformed_duplicate_nonfinite_and_deep_json(wire: bytes) -> None:
    with pytest.raises(CapabilityProofError):
        validate_proof(wire, CapabilityProofKind.CLIENT_IDENTITY, _manifest())
