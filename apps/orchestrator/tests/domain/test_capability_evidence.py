"""Closed, canonical contracts for trusted official-client capability evidence."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.capability_evidence import (
    CapabilityEvidenceError,
    CapabilityEvidenceIdentity,
    CapabilityEvidenceManifest,
    CapabilityEvidenceScope,
    CapabilityProof,
    CapabilityProofKind,
    ResolvedCapabilityEvidence,
    capability_home_digest,
    decode_capability_evidence,
    encode_capability_evidence,
)
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    SpecialistPurpose,
)
from forge.domain.tool import ToolName
from pydantic import ValidationError


def _identity() -> CapabilityEvidenceIdentity:
    return CapabilityEvidenceIdentity(
        provider="openai",
        client="codex_app_server",
        client_version="0.153.4",
        executable_digest="1" * 64,
        client_home_digest="2" * 64,
        account="personal-chatgpt",
        model="gpt-6-astra",
        effort=ReasoningEffort.LOW,
        role=SpecialistPurpose.PRIMARY,
        tool_surface=(
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
        ),
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


def test_capability_manifest_is_canonical_closed_and_proof_backed() -> None:
    observed = datetime(2026, 9, 13, 10, tzinfo=UTC)
    manifest = CapabilityEvidenceManifest(
        evidence_id=uuid4(),
        identity=_identity(),
        verifier_id="codex-conformance",
        verifier_version="1",
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
        proofs=tuple(
            CapabilityProof(kind=kind, artifact_digest=f"{index:x}" * 64)
            for index, kind in enumerate(CapabilityProofKind, start=3)
        ),
    )

    wire = encode_capability_evidence(manifest)
    assert decode_capability_evidence(wire) == manifest
    assert decode_capability_evidence(wire).identity.tool_surface == (
        ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH,
    )
    assert b"personal-chatgpt" in wire
    assert b'"client_home_digest"' in wire
    assert b"fixture-home" not in wire
    assert b"billing_allowance_enforced" not in wire
    with pytest.raises(CapabilityEvidenceError, match="not canonical"):
        decode_capability_evidence(wire + b"\n")
    with pytest.raises(CapabilityEvidenceError, match="duplicate key"):
        decode_capability_evidence(b'{"schema_version":1,"schema_version":1}')

    payload = manifest.model_dump(mode="json")
    payload["raw_diagnostics"] = "provider payload must remain excluded"
    with pytest.raises(ValidationError):
        CapabilityEvidenceManifest.model_validate(payload)

    payload = manifest.model_dump(mode="json")
    payload["proofs"] = payload["proofs"][:-1]
    with pytest.raises(ValidationError, match="proof set"):
        CapabilityEvidenceManifest.model_validate(payload)


def test_subscription_route_binding_keeps_legacy_schema_v1_wire_value() -> None:
    """The historical wire label must not imply provider-wide billing control."""

    assert CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING.value == "billing_enforcement"
    assert len(CapabilityProofKind) == 5
    assert [kind.value for kind in CapabilityProofKind].count("billing_enforcement") == 1
    schema = CapabilityProof.model_json_schema()["$defs"]["CapabilityProofKind"]
    assert schema["enum"].count("billing_enforcement") == 1


def test_literal_schema_v1_subscription_route_proof_wire_decodes_and_round_trips() -> None:
    observed = datetime(2026, 9, 13, 10, tzinfo=UTC)
    manifest = CapabilityEvidenceManifest(
        evidence_id=uuid4(),
        identity=_identity(),
        verifier_id="codex-conformance",
        verifier_version="1",
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
        proofs=tuple(
            CapabilityProof(kind=kind, artifact_digest=f"{index:x}" * 64)
            for index, kind in enumerate(CapabilityProofKind, start=3)
        ),
    )
    wire = encode_capability_evidence(manifest)
    assert b'"schema_version":1' in wire
    assert b'"kind":"billing_enforcement"' in wire
    assert encode_capability_evidence(decode_capability_evidence(wire)) == wire


def test_resolved_evidence_permits_only_its_exact_role_and_tool_surface() -> None:
    observed = datetime(2026, 9, 13, 10, tzinfo=UTC)
    manifest = CapabilityEvidenceManifest(
        evidence_id=uuid4(),
        identity=_identity(),
        verifier_id="codex-conformance",
        verifier_version="1",
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
        proofs=tuple(
            CapabilityProof(kind=kind, artifact_digest=f"{index:x}" * 64)
            for index, kind in enumerate(CapabilityProofKind, start=3)
        ),
    )
    wire = encode_capability_evidence(manifest)
    resolved = ResolvedCapabilityEvidence(
        manifest=manifest,
        artifact_digest=hashlib.sha256(wire).hexdigest(),
        revision=4,
    )
    scope = CapabilityEvidenceScope(
        route=manifest.identity.route,
        role=manifest.identity.role,
        tool_surface=manifest.identity.tool_surface,
    )

    assert resolved.matches(manifest.identity)
    assert resolved.permits(scope)
    assert not resolved.permits(replace(scope, role=SpecialistPurpose.SECURITY))
    assert not resolved.permits(replace(scope, tool_surface=()))

    for changes in (
        {"provider": "anthropic"},
        {"client": "claude_code"},
        {"client_version": "0.153.5"},
        {"executable_digest": "9" * 64},
        {"client_home_digest": "8" * 64},
        {"account": "other-account"},
        {"model": "gpt-5.6-sol"},
        {"effort": ReasoningEffort.HIGH},
        {"auth_mode": AuthMode.API_KEY},
        {"billing_mode": BillingMode.PAID_OPT_IN},
        {"role": SpecialistPurpose.SECURITY},
        {"tool_surface": ()},
    ):
        assert not resolved.matches(manifest.identity.model_copy(update=changes))


def test_home_identity_is_canonical_and_account_identity_is_opaque(tmp_path) -> None:
    home = tmp_path / "client-home"
    home.mkdir()
    same_home = home / ".." / home.name
    other = tmp_path / "other-home"
    other.mkdir()

    assert capability_home_digest(str(home)) == capability_home_digest(str(same_home))
    assert capability_home_digest(str(home)) != capability_home_digest(str(other))
    payload = _identity().model_dump(mode="python")
    payload["account"] = "person@example.com"
    with pytest.raises(ValidationError, match="opaque lowercase label"):
        CapabilityEvidenceIdentity.model_validate(payload)
