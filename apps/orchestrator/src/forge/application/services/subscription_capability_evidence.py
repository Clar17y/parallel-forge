"""Fail-closed assembly of identity-bound subscription capability evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from forge.agents.capability_publication import CapabilityObservationSet
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.capability_evidence_publication import CapabilityEvidencePublisher
from forge.artifacts._errors import ArtifactIntegrityError, ArtifactStoreError
from forge.domain.capability_evidence import (
    CapabilityEvidenceIdentity,
    CapabilityEvidenceManifest,
    CapabilityProof,
    CapabilityProofKind,
    ResolvedCapabilityEvidence,
)
from forge.domain.capability_proof import CapabilityProofError, encode_proof

PROOF_MEDIA_TYPE = "application/vnd.forge.client-capability-proof+json"
_DEFAULT_TTL = timedelta(hours=1)
_MAX_TTL = timedelta(hours=24)
_FRESHNESS_WINDOW = timedelta(minutes=5)
_EVIDENCE_NAMESPACE = UUID("90d6c24f-e74f-5d1b-a2e8-4f5ff9c0b83d")


class CapabilityPublicationError(ValueError):
    """Publication input or its artifact boundary failed closed."""


class SubscriptionCapabilityEvidenceService:
    """Create all proof bytes before writing any immutable proof artifact."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        publisher: CapabilityEvidencePublisher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not all(callable(getattr(artifacts, name, None)) for name in ("put_bytes", "verify")):
            raise TypeError("capability publication requires an artifact store")
        if not callable(getattr(publisher, "publish", None)) or not callable(clock):
            raise TypeError("capability publication dependencies are invalid")
        self._artifacts = artifacts
        self._publisher = publisher
        self._clock = clock

    async def publish(
        self,
        *,
        identity: CapabilityEvidenceIdentity,
        observations: CapabilityObservationSet,
        observed_at: datetime,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> ResolvedCapabilityEvidence:
        """Publish a complete five-proof manifest, or reject before any input write."""
        now = _utc_time(self._clock())
        observed = _utc_time(observed_at)
        if abs(now - observed) > _FRESHNESS_WINDOW:
            raise CapabilityPublicationError("observation is not fresh")
        if type(ttl) is not timedelta or not timedelta() < ttl <= _MAX_TTL:
            raise CapabilityPublicationError(
                "capability evidence TTL must be from 1 microsecond to 24 hours"
            )
        payloads = _observation_payloads(identity, observations)
        verifier_id = observations.verifier_id
        verifier_version = observations.verifier_version
        try:
            expires = observed + ttl
        except OverflowError:
            raise CapabilityPublicationError("capability evidence expiry is invalid") from None
        draft = _manifest(
            identity,
            verifier_id,
            verifier_version,
            observed,
            expires,
            UUID(int=1),
            tuple(
                CapabilityProof(kind=kind, artifact_digest="0" * 64) for kind in CapabilityProofKind
            ),
        )
        try:
            wires = {
                kind: encode_proof(kind, payloads[kind], draft) for kind in CapabilityProofKind
            }
        except CapabilityProofError as error:
            raise CapabilityPublicationError("capability proof payload is invalid") from error

        proofs: list[CapabilityProof] = []
        for kind in CapabilityProofKind:
            wire = wires[kind]
            try:
                descriptor = await self._artifacts.put_bytes(wire, media_type=PROOF_MEDIA_TYPE)
                if (
                    descriptor.digest != hashlib.sha256(wire).hexdigest()
                    or descriptor.media_type != PROOF_MEDIA_TYPE
                    or descriptor.byte_count != len(wire)
                    or descriptor.truncated
                    or not await self._artifacts.verify(descriptor.digest)
                ):
                    raise CapabilityPublicationError("capability proof artifact differs")
            except ArtifactIntegrityError, ArtifactStoreError, OSError, AttributeError, TypeError:
                raise CapabilityPublicationError(
                    "capability proof artifact is unavailable"
                ) from None
            proofs.append(CapabilityProof(kind=kind, artifact_digest=descriptor.digest))

        evidence_id = _evidence_id(
            identity, verifier_id, verifier_version, observed, expires, proofs
        )
        manifest = _manifest(
            identity, verifier_id, verifier_version, observed, expires, evidence_id, proofs
        )
        return await self._publisher.publish(manifest)


def _utc_time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CapabilityPublicationError("observation time is invalid")
    return value.astimezone(UTC)


def _digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _observation_payloads(
    identity: object, observations: object
) -> dict[CapabilityProofKind, dict[str, object]]:
    if not isinstance(identity, CapabilityEvidenceIdentity) or not isinstance(
        observations, CapabilityObservationSet
    ):
        raise CapabilityPublicationError("typed capability observations are required")
    client = observations.client_identity
    account = observations.account_authentication
    route = observations.route_identity
    binding = observations.subscription_route_binding
    tools = observations.tool_isolation
    strings = (
        client.client,
        client.client_version,
        client.executable_digest,
        client.client_home_digest,
        account.account,
        account.auth_mode,
        route.model,
        route.effort,
        binding.auth_mode,
        binding.billing_mode,
    )
    if any(type(value) is not str for value in strings):
        raise CapabilityPublicationError("typed capability observations are invalid")
    if (
        client.client,
        client.client_version,
        client.executable_digest,
        client.client_home_digest,
    ) != (
        identity.client,
        identity.client_version,
        identity.executable_digest,
        identity.client_home_digest,
    ):
        raise CapabilityPublicationError("client identity differs")
    if (
        client.executable_unchanged is not True
        or client.reported_client_version != identity.client_version
    ):
        raise CapabilityPublicationError("client identity observation differs")
    if (account.account, account.auth_mode) != (identity.account, identity.auth_mode.value):
        raise CapabilityPublicationError("account authentication differs")
    if account.account_kind not in ("chatgpt", "subscription") or account.authenticated is not True:
        raise CapabilityPublicationError("account authentication observation differs")
    if (route.model, route.effort) != (identity.model, identity.effort.value):
        raise CapabilityPublicationError("route identity differs")
    if (
        route.catalog_supported is not True
        or route.turn_completed is not True
        or not _digest(route.turn_observation_digest)
    ):
        raise CapabilityPublicationError("route identity observation differs")
    if (binding.auth_mode, binding.billing_mode) != (
        identity.auth_mode.value,
        identity.billing_mode.value,
    ):
        raise CapabilityPublicationError("subscription binding differs")
    if binding.paid_credential_names_scrubbed is not True or binding.fallback_disabled is not True:
        raise CapabilityPublicationError("subscription binding differs")
    if binding.subscription_route_observed is not True:
        raise CapabilityPublicationError("subscription binding differs")
    expected_tools = tuple(tool.value for tool in identity.tool_surface)
    expected_tool_digest = hashlib.sha256(
        json.dumps(list(expected_tools), separators=(",", ":")).encode()
    ).hexdigest()
    if (
        tools.tool_surface != expected_tools
        or tools.isolated is not True
        or tools.advertised_tool_surface_digest != expected_tool_digest
        or tools.forbidden_tool_calls != 0
        or type(tools.forbidden_tool_calls) is not int
        or tools.side_effect_canaries_clear is not True
    ):
        raise CapabilityPublicationError("tool isolation differs")
    return {
        CapabilityProofKind.CLIENT_IDENTITY: {
            "client": client.client,
            "client_version": client.client_version,
            "executable_digest": client.executable_digest,
            "client_home_digest": client.client_home_digest,
            "executable_unchanged": True,
            "reported_client_version": client.reported_client_version,
        },
        CapabilityProofKind.ACCOUNT_AUTHENTICATION: {
            "account": account.account,
            "auth_mode": account.auth_mode,
            "account_kind": account.account_kind,
            "authenticated": True,
        },
        CapabilityProofKind.ROUTE_IDENTITY: {
            "model": route.model,
            "effort": route.effort,
            "catalog_supported": True,
            "turn_completed": True,
            "turn_observation_digest": route.turn_observation_digest,
        },
        CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING: {
            "auth_mode": binding.auth_mode,
            "billing_mode": binding.billing_mode,
            "paid_credential_names_scrubbed": binding.paid_credential_names_scrubbed,
            "fallback_disabled": binding.fallback_disabled,
            "subscription_route_observed": True,
        },
        CapabilityProofKind.TOOL_ISOLATION: {
            "tool_surface": list(tools.tool_surface),
            "isolated": tools.isolated,
            "advertised_tool_surface_digest": tools.advertised_tool_surface_digest,
            "forbidden_tool_calls": 0,
            "side_effect_canaries_clear": True,
        },
    }


def _manifest(
    identity: CapabilityEvidenceIdentity,
    verifier_id: str,
    verifier_version: str,
    observed: datetime,
    expires: datetime,
    evidence_id: UUID,
    proofs: tuple[CapabilityProof, ...] | list[CapabilityProof],
) -> CapabilityEvidenceManifest:
    try:
        return CapabilityEvidenceManifest(
            evidence_id=evidence_id,
            identity=identity,
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            observed_at=observed,
            expires_at=expires,
            proofs=tuple(proofs),
        )
    except (TypeError, ValueError) as error:
        raise CapabilityPublicationError("capability evidence input is invalid") from error


def _evidence_id(
    identity: CapabilityEvidenceIdentity,
    verifier_id: str,
    verifier_version: str,
    observed: datetime,
    expires: datetime,
    proofs: list[CapabilityProof],
) -> UUID:
    payload = {
        "identity": identity.digest,
        "verifier": [verifier_id, verifier_version],
        "observed_at": observed.isoformat(),
        "expires_at": expires.isoformat(),
        "proofs": [(proof.kind.value, proof.artifact_digest) for proof in proofs],
    }
    return uuid5(
        _EVIDENCE_NAMESPACE,
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )


__all__ = [
    "PROOF_MEDIA_TYPE",
    "CapabilityPublicationError",
    "SubscriptionCapabilityEvidenceService",
]
