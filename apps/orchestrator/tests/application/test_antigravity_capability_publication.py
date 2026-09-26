"""Providerless publication contracts; all observations below are synthetic."""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest
from forge.agents.capability_publication import (
    AccountAuthenticationObservation,
    AntigravityCallbackObservation,
    AntigravityPolicyObservation,
    CapabilityObservationSet,
    ClientIdentityObservation,
    RouteIdentityObservation,
    SubscriptionRouteBindingObservation,
)
from forge.application.services.subscription_capability_evidence import (
    CapabilityPublicationError,
    SubscriptionCapabilityEvidenceService,
)
from forge.domain.capability_evidence import CapabilityEvidenceIdentity, CapabilityProofKind
from forge.domain.capability_proof import validate_proof
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof


class Artifacts:
    def __init__(self):
        self.wires = {}

    async def put_bytes(self, wire, *, media_type):
        digest = sha256(wire).hexdigest()
        self.wires[digest] = wire
        return SimpleNamespace(
            digest=digest, media_type=media_type, byte_count=len(wire), truncated=False
        )

    async def verify(self, digest):
        return digest in self.wires


class Publisher:
    async def publish(self, manifest):
        return manifest


def identity():
    return CapabilityEvidenceIdentity(
        provider="google",
        client="antigravity_cli",
        client_version="1.2.7",
        executable_digest="a" * 64,
        client_home_digest="b" * 64,
        account="c" * 64,
        model="gemini-3.8-flash-medium",
        effort="medium",
        role="routine_implementation",
        tool_surface=("repository.read_file",),
        auth_mode="subscription",
        billing_mode="allowance_only",
    )


def terminal(outcome):
    return SubscriptionLaunchTerminalProof(
        launch_id=f"launch-{outcome}",
        pid=42,
        process_identity=f"process-{outcome}",
        outcome=outcome,
        return_code=0,
        stop_confirmed=True,
        stdout_bytes=100,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )


def observations():
    i = identity()
    return CapabilityObservationSet(
        client_identity=ClientIdentityObservation(
            i.client,
            i.client_version,
            i.executable_digest,
            i.client_home_digest,
            reported_client_version=i.client_version,
        ),
        account_authentication=AccountAuthenticationObservation(i.account, "subscription"),
        route_identity=RouteIdentityObservation(
            i.model, "medium", turn_observation_digest="d" * 64
        ),
        subscription_route_binding=SubscriptionRouteBindingObservation(
            "subscription",
            "allowance_only",
            True,
            True,
        ),
        tool_isolation=AntigravityCallbackObservation(
            tool_surface=("repository.read_file",),
            callback_identity_bound=True,
            remote_mcp_collision_rejected=True,
            callback_observation_digest="e" * 64,
            structured_output_validated=True,
            usage_bounded=True,
            completion=terminal("exited"),
            cancellation=terminal("cancelled"),
            deadline=terminal("timeout"),
        ),
        antigravity_policy=AntigravityPolicyObservation(
            authentication_source="google_subscription",
            main_and_auxiliary_routes_bound=True,
            fallback_chain_bound=True,
            effective_use_g1_credits=False,
            home_policy_observed=True,
            system_policy_observed=True,
            remote_policy_observed=True,
            alternate_credentials_excluded=True,
            effective_configuration_digest="f" * 64,
        ),
        verifier_id="forge-antigravity-official",
        verifier_version="1",
    )


async def publish(observation, artifacts):
    now = datetime.now(UTC)
    service = SubscriptionCapabilityEvidenceService(artifacts, Publisher(), clock=lambda: now)
    return await service.publish(identity=identity(), observations=observation, observed_at=now)


async def test_complete_synthetic_evidence_publishes_warning_and_all_five_proofs():
    artifacts = Artifacts()
    manifest = await publish(observations(), artifacts)
    assert len(manifest.proofs) == 5
    for proof in manifest.proofs:
        validate_proof(artifacts.wires[proof.artifact_digest], proof.kind, manifest)
        if proof.kind is CapabilityProofKind.TOOL_ISOLATION:
            assert b'"approved_tools_unproved":true' in artifacts.wires[proof.artifact_digest]


@pytest.mark.parametrize(
    "field",
    [
        "home_policy_observed",
        "system_policy_observed",
        "remote_policy_observed",
        "main_and_auxiliary_routes_bound",
        "fallback_chain_bound",
        "alternate_credentials_excluded",
    ],
)
async def test_incomplete_policy_never_writes_an_artifact(field):
    artifacts = Artifacts()
    value = observations()
    value = replace(value, antigravity_policy=replace(value.antigravity_policy, **{field: False}))
    with pytest.raises(CapabilityPublicationError):
        await publish(value, artifacts)
    assert artifacts.wires == {}


@pytest.mark.parametrize(
    "field",
    [
        "callback_identity_bound",
        "remote_mcp_collision_rejected",
        "structured_output_validated",
        "usage_bounded",
    ],
)
async def test_incomplete_callback_never_writes_an_artifact(field):
    artifacts = Artifacts()
    value = observations()
    value = replace(value, tool_isolation=replace(value.tool_isolation, **{field: False}))
    with pytest.raises(CapabilityPublicationError):
        await publish(value, artifacts)
    assert artifacts.wires == {}
