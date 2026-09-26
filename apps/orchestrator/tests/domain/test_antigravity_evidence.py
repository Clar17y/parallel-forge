"""Antigravity permits degraded tools, never degraded identity or lifecycle proof."""

from copy import deepcopy
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
from forge.domain.subscription import ReasoningEffort, SpecialistPurpose
from forge.domain.tool import ToolName


def manifest():
    return CapabilityEvidenceManifest(
        evidence_id=uuid4(),
        identity=CapabilityEvidenceIdentity(
            provider="google",
            client="antigravity_cli",
            client_version="1.2.7",
            executable_digest="a" * 64,
            client_home_digest="b" * 64,
            account="c" * 64,
            model="gemini-3.8-flash-medium",
            effort=ReasoningEffort.MEDIUM,
            role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
            tool_surface=(ToolName.REPOSITORY_READ_FILE,),
            auth_mode="subscription",
            billing_mode="allowance_only",
        ),
        verifier_id="forge-antigravity-official",
        verifier_version="1",
        observed_at=datetime(2026, 9, 22, tzinfo=UTC),
        expires_at=datetime(2026, 9, 22, tzinfo=UTC) + timedelta(hours=1),
        proofs=tuple(
            CapabilityProof(kind=k, artifact_digest="d" * 64) for k in CapabilityProofKind
        ),
    )


def terminal(outcome):
    return {
        "schema_version": 1,
        "launch_id": f"launch-{outcome}",
        "pid": 42,
        "process_identity": f"process-{outcome}",
        "outcome": outcome,
        "return_code": 0 if outcome == "exited" else 1,
        "stop_confirmed": True,
        "stdout_bytes": 128,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def tool_payload():
    return {
        "tool_surface": ["repository.read_file"],
        "approved_tools_unproved": True,
        "callback_identity_bound": True,
        "remote_mcp_collision_rejected": True,
        "callback_observation_digest": "e" * 64,
        "structured_output_validated": True,
        "usage_bounded": True,
        "completion": terminal("exited"),
        "cancellation": terminal("cancelled"),
        "deadline": terminal("timeout"),
    }


def test_antigravity_callback_proof_preserves_warning_without_native_isolation_claim():
    m = manifest()
    wire = encode_proof(CapabilityProofKind.TOOL_ISOLATION, tool_payload(), m)
    validate_proof(wire, CapabilityProofKind.TOOL_ISOLATION, m)
    assert b'"approved_tools_unproved":true' in wire
    assert b'"isolated"' not in wire


@pytest.mark.parametrize(
    "field",
    [
        "approved_tools_unproved",
        "callback_identity_bound",
        "remote_mcp_collision_rejected",
        "structured_output_validated",
        "usage_bounded",
    ],
)
@pytest.mark.parametrize("value", [None, False, 1, "true"])
def test_warning_and_each_callback_contract_are_mandatory(field, value):
    payload = tool_payload()
    payload[field] = value
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.TOOL_ISOLATION, payload, manifest())


@pytest.mark.parametrize("kind", ["completion", "cancellation", "deadline"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("stop_confirmed", False),
        ("stdout_truncated", True),
        ("stderr_truncated", True),
        ("outcome", "stop_uncertain"),
    ],
)
def test_each_required_process_tree_must_be_settled(kind, field, value):
    payload = tool_payload()
    payload[kind][field] = value
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.TOOL_ISOLATION, payload, manifest())


def test_completion_proof_cannot_be_reused_as_cancellation_or_deadline_evidence():
    payload = tool_payload()
    payload["cancellation"] = deepcopy(payload["completion"])
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.TOOL_ISOLATION, payload, manifest())


def test_degraded_policy_cannot_be_applied_to_another_client():
    m = manifest()
    m = m.model_copy(update={"identity": m.identity.model_copy(update={"client": "gemini_cli"})})
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.TOOL_ISOLATION, tool_payload(), m)


def binding_payload():
    return {
        "auth_mode": "subscription",
        "billing_mode": "allowance_only",
        "paid_credential_names_scrubbed": True,
        "fallback_disabled": True,
        "subscription_route_observed": True,
        "effective_use_g1_credits": False,
        "home_policy_observed": True,
        "system_policy_observed": True,
        "remote_policy_observed": True,
        "alternate_credentials_excluded": True,
        "effective_configuration_digest": "f" * 64,
    }


def test_subscription_route_binding_alone_cannot_prove_effective_antigravity_credits():
    legacy = {
        key: binding_payload()[key]
        for key in (
            "auth_mode",
            "billing_mode",
            "paid_credential_names_scrubbed",
            "fallback_disabled",
            "subscription_route_observed",
        )
    }
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING, legacy, manifest())


@pytest.mark.parametrize(
    "field",
    [
        "effective_use_g1_credits",
        "home_policy_observed",
        "system_policy_observed",
        "remote_policy_observed",
        "alternate_credentials_excluded",
        "effective_configuration_digest",
    ],
)
def test_every_effective_policy_contract_is_required(field):
    payload = binding_payload()
    m = manifest()
    validate_proof(
        encode_proof(CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING, payload, m),
        CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING,
        m,
    )
    del payload[field]
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING, payload, m)


@pytest.mark.parametrize("value", [True, None, 0, "false"])
def test_paid_or_unknown_credit_setting_is_rejected(value):
    payload = binding_payload()
    payload["effective_use_g1_credits"] = value
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING, payload, manifest())


def test_main_model_success_does_not_prove_auxiliary_or_fallback_identity():
    payload = {
        "model": "gemini-3.8-flash-medium",
        "effort": "medium",
        "catalog_supported": True,
        "turn_completed": True,
        "turn_observation_digest": "f" * 64,
    }
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.ROUTE_IDENTITY, payload, manifest())
    payload.update({"main_and_auxiliary_routes_bound": True, "fallback_chain_bound": True})
    encode_proof(CapabilityProofKind.ROUTE_IDENTITY, payload, manifest())


def test_authentication_requires_positive_source_observation_and_exact_account_digest():
    m = manifest()
    payload = {
        "account": m.identity.account,
        "auth_mode": "subscription",
        "authenticated": True,
        "account_kind": "subscription",
    }
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.ACCOUNT_AUTHENTICATION, payload, m)
    payload["authentication_source"] = "google_subscription"
    encode_proof(CapabilityProofKind.ACCOUNT_AUTHENTICATION, payload, m)
    payload["authentication_source"] = "api_key"
    with pytest.raises(CapabilityProofError):
        encode_proof(CapabilityProofKind.ACCOUNT_AUTHENTICATION, payload, m)
