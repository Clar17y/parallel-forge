"""Canonical, credential-free identity for official-client capability evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteSpec,
    SpecialistPurpose,
    validate_tool_permission,
)
from forge.domain.tool import ToolName

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_OPAQUE_LABEL = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,95}\Z", re.ASCII)
_VERSION = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,95}\Z", re.ASCII)
_MODEL = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/+-]{0,254}\Z", re.ASCII)
_MAX_WIRE_BYTES = 64 * 1024


class CapabilityEvidenceError(ValueError):
    """A capability evidence document is malformed or non-canonical."""


class CapabilityProofKind(StrEnum):
    """Distinct proof classes required before a route can be admitted."""

    CLIENT_IDENTITY = "client_identity"
    ACCOUNT_AUTHENTICATION = "account_authentication"
    ROUTE_IDENTITY = "route_identity"
    # Schema-v1 called this wire value ``billing_enforcement``.  It never
    # established a provider-wide account billing guarantee: it binds Forge's
    # subscription route and excludes Forge-selected paid API credentials.
    # Keep the wire value so stored manifests remain decodable.
    SUBSCRIPTION_ROUTE_BINDING = "billing_enforcement"
    TOOL_ISOLATION = "tool_isolation"


class CapabilityEvidenceIdentity(BaseModel):
    """Exact route, installation and bounded authority covered by evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    client: str
    client_version: str
    executable_digest: str
    client_home_digest: str
    account: str
    model: str
    effort: ReasoningEffort
    role: SpecialistPurpose
    tool_surface: tuple[ToolName, ...] = ()
    auth_mode: AuthMode
    billing_mode: BillingMode

    @field_validator("provider", "client", "account", mode="before")
    @classmethod
    def _opaque_labels(cls, value: Any, info: Any) -> str:
        if type(value) is not str or _OPAQUE_LABEL.fullmatch(value) is None:
            raise ValueError(f"{info.field_name} must be an opaque lowercase label")
        return value

    @field_validator("client_version", mode="before")
    @classmethod
    def _client_version(cls, value: Any) -> str:
        if type(value) is not str or _VERSION.fullmatch(value) is None:
            raise ValueError("client_version must be a bounded version identifier")
        return value

    @field_validator("model", mode="before")
    @classmethod
    def _model(cls, value: Any) -> str:
        if type(value) is not str or _MODEL.fullmatch(value) is None:
            raise ValueError("model must be a bounded route identifier")
        return value

    @field_validator("executable_digest", "client_home_digest", mode="before")
    @classmethod
    def _identity_digests(cls, value: Any, info: Any) -> str:
        return _digest(value, info.field_name)

    @field_validator("tool_surface", mode="before")
    @classmethod
    def _tools(cls, value: Any) -> tuple[ToolName, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise TypeError("tool_surface must be a sequence")
        if len(value) > 32:
            raise ValueError("tool_surface exceeds 32 tools")
        try:
            tools = tuple(item if isinstance(item, ToolName) else ToolName(item) for item in value)
        except TypeError, ValueError:
            raise ValueError("tool_surface contains an unknown tool") from None
        canonical = tuple(sorted(set(tools), key=lambda item: item.value))
        if len(canonical) != len(tools):
            raise ValueError("tool_surface contains duplicate tools")
        return canonical

    @model_validator(mode="after")
    def _role_tools(self) -> Self:
        for tool in self.tool_surface:
            validate_tool_permission(self.role, tool)
        return self

    @property
    def route(self) -> RouteSpec:
        return RouteSpec(
            provider=self.provider,
            client=self.client,
            model=self.model,
            effort=self.effort,
            auth_mode=self.auth_mode,
            billing_mode=self.billing_mode,
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(_identity_payload(self))).hexdigest()


class CapabilityProof(BaseModel):
    """Reference to sanitized provider-specific proof, never a capability boolean."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CapabilityProofKind
    artifact_digest: str

    @field_validator("artifact_digest", mode="before")
    @classmethod
    def _artifact_digest(cls, value: Any) -> str:
        return _digest(value, "artifact_digest")


class CapabilityEvidenceManifest(BaseModel):
    """Immutable artifact payload selected by the PostgreSQL evidence source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["client_capability"] = "client_capability"
    evidence_id: UUID
    identity: CapabilityEvidenceIdentity
    verifier_id: str
    verifier_version: str
    observed_at: datetime
    expires_at: datetime
    proofs: tuple[CapabilityProof, ...]

    @field_validator("evidence_id", mode="before")
    @classmethod
    def _evidence_id(cls, value: Any) -> UUID:
        if isinstance(value, str):
            try:
                value = UUID(value)
            except ValueError:
                raise ValueError("evidence_id must be a UUID") from None
        if not isinstance(value, UUID) or value.int == 0:
            raise ValueError("evidence_id must be a non-nil UUID")
        return value

    @field_validator("verifier_id", mode="before")
    @classmethod
    def _verifier_id(cls, value: Any) -> str:
        if type(value) is not str or _OPAQUE_LABEL.fullmatch(value) is None:
            raise ValueError("verifier_id must be an opaque lowercase label")
        return value

    @field_validator("verifier_version", mode="before")
    @classmethod
    def _verifier_version(cls, value: Any) -> str:
        if type(value) is not str or _VERSION.fullmatch(value) is None:
            raise ValueError("verifier_version must be a bounded version identifier")
        return value

    @field_validator("observed_at", "expires_at", mode="before")
    @classmethod
    def _timestamps(cls, value: Any, info: Any) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError(f"{info.field_name} must be an ISO datetime") from None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("proofs", mode="before")
    @classmethod
    def _proofs(cls, value: Any) -> tuple[CapabilityProof, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise TypeError("proofs must be a sequence")
        try:
            proofs = tuple(
                item if isinstance(item, CapabilityProof) else CapabilityProof.model_validate(item)
                for item in value
            )
        except TypeError, ValueError:
            raise ValueError("capability proof set is invalid") from None
        kinds = {proof.kind for proof in proofs}
        if kinds != set(CapabilityProofKind) or len(proofs) != len(kinds):
            raise ValueError("capability proof set must contain each required proof exactly once")
        return tuple(sorted(proofs, key=lambda item: item.kind.value))

    @model_validator(mode="after")
    def _time_order(self) -> Self:
        if self.expires_at <= self.observed_at:
            raise ValueError("capability evidence must expire after observation")
        return self


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityEvidenceScope:
    """Invocation-time route and controlled authority that evidence must cover."""

    route: RouteSpec
    role: SpecialistPurpose
    tool_surface: tuple[ToolName, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.route, RouteSpec) or not isinstance(self.role, SpecialistPurpose):
            raise TypeError("capability scope requires a route and specialist role")
        tools = tuple(self.tool_surface)
        if len(tools) > 32 or any(not isinstance(tool, ToolName) for tool in tools):
            raise ValueError("capability scope tools are invalid")
        canonical = tuple(sorted(set(tools), key=lambda item: item.value))
        if len(canonical) != len(tools):
            raise ValueError("capability scope tools contain duplicates")
        for tool in canonical:
            validate_tool_permission(self.role, tool)
        object.__setattr__(self, "tool_surface", canonical)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedCapabilityEvidence:
    """Artifact-verified current evidence returned by the trusted source."""

    manifest: CapabilityEvidenceManifest
    artifact_digest: str
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, CapabilityEvidenceManifest):
            raise TypeError("resolved capability evidence requires a manifest")
        wire = encode_capability_evidence(self.manifest)
        if _digest(self.artifact_digest, "artifact_digest") != hashlib.sha256(wire).hexdigest():
            raise ValueError("resolved capability artifact digest differs from its manifest")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("resolved capability revision must be positive")

    def matches(self, identity: CapabilityEvidenceIdentity) -> bool:
        return (
            isinstance(identity, CapabilityEvidenceIdentity) and self.manifest.identity == identity
        )

    def permits(self, scope: CapabilityEvidenceScope) -> bool:
        identity = self.manifest.identity
        return isinstance(scope, CapabilityEvidenceScope) and (
            identity.route,
            identity.role,
            identity.tool_surface,
        ) == (scope.route, scope.role, scope.tool_surface)


def capability_home_digest(path: str) -> str:
    """Bind an existing canonical home without persisting the sensitive path."""

    if type(path) is not str or not path:
        raise ValueError("capability home must be an existing absolute directory")
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise ValueError("capability home must be an existing absolute directory")
    canonical = os.path.normcase(str(candidate.resolve(strict=True)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_capability_installation_identity(
    account: Any, executable_digest: Any
) -> tuple[str, str]:
    """Validate the non-secret installation pins used to resolve evidence."""

    if type(account) is not str or _OPAQUE_LABEL.fullmatch(account) is None:
        raise ValueError("capability account requires an opaque lowercase label")
    return account, _digest(executable_digest, "capability executable")


def capability_identity(
    *,
    scope: CapabilityEvidenceScope,
    client_version: str,
    executable_digest: str,
    client_home: str,
    account: str,
) -> CapabilityEvidenceIdentity:
    """Complete invocation scope with the exact trusted installation identity."""

    if not isinstance(scope, CapabilityEvidenceScope):
        raise TypeError("capability scope is required")
    return CapabilityEvidenceIdentity(
        provider=scope.route.provider,
        client=scope.route.client,
        client_version=client_version,
        executable_digest=executable_digest,
        client_home_digest=capability_home_digest(client_home),
        account=account,
        model=scope.route.model,
        effort=scope.route.effort,
        role=scope.role,
        tool_surface=scope.tool_surface,
        auth_mode=scope.route.auth_mode,
        billing_mode=scope.route.billing_mode,
    )


def encode_capability_evidence(manifest: CapabilityEvidenceManifest) -> bytes:
    """Deeply validate and encode one deterministic UTF-8 JSON artifact."""

    try:
        validated = CapabilityEvidenceManifest.model_validate(
            manifest.model_dump(mode="python")
            if isinstance(manifest, CapabilityEvidenceManifest)
            else manifest
        )
        wire = _canonical_json(_manifest_payload(validated))
    except (AttributeError, TypeError, ValueError) as error:
        raise CapabilityEvidenceError("capability evidence validation failed") from error
    if len(wire) > _MAX_WIRE_BYTES:
        raise CapabilityEvidenceError("capability evidence exceeds 64 KiB")
    return wire


def decode_capability_evidence(data: bytes) -> CapabilityEvidenceManifest:
    """Decode only canonical, closed capability evidence documents."""

    if not isinstance(data, bytes) or len(data) > _MAX_WIRE_BYTES:
        raise CapabilityEvidenceError("capability evidence wire is invalid")
    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CapabilityEvidenceError(f"illegal JSON constant: {value}")
            ),
        )
        if not isinstance(payload, dict):
            raise CapabilityEvidenceError("capability evidence root must be an object")
        manifest = CapabilityEvidenceManifest.model_validate(payload)
    except CapabilityEvidenceError:
        raise
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError:
        raise CapabilityEvidenceError("capability evidence wire is invalid") from None
    if encode_capability_evidence(manifest) != data:
        raise CapabilityEvidenceError("capability evidence wire is not canonical")
    return manifest


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _identity_payload(value: CapabilityEvidenceIdentity) -> dict[str, object]:
    return {
        "account": value.account,
        "auth_mode": value.auth_mode.value,
        "billing_mode": value.billing_mode.value,
        "client": value.client,
        "client_home_digest": value.client_home_digest,
        "client_version": value.client_version,
        "effort": value.effort.value,
        "executable_digest": value.executable_digest,
        "model": value.model,
        "provider": value.provider,
        "role": value.role.value,
        "tool_surface": [tool.value for tool in value.tool_surface],
    }


def _manifest_payload(value: CapabilityEvidenceManifest) -> dict[str, object]:
    return {
        "evidence_id": str(value.evidence_id),
        "expires_at": value.expires_at.isoformat(),
        "identity": _identity_payload(value.identity),
        "kind": value.kind,
        "observed_at": value.observed_at.isoformat(),
        "proofs": [
            {"artifact_digest": proof.artifact_digest, "kind": proof.kind.value}
            for proof in value.proofs
        ],
        "schema_version": value.schema_version,
        "verifier_id": value.verifier_id,
        "verifier_version": value.verifier_version,
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapabilityEvidenceError("capability evidence contains a duplicate key")
        result[key] = value
    return result


__all__ = [
    "CapabilityEvidenceError",
    "CapabilityEvidenceIdentity",
    "CapabilityEvidenceManifest",
    "CapabilityEvidenceScope",
    "CapabilityProof",
    "CapabilityProofKind",
    "ResolvedCapabilityEvidence",
    "capability_home_digest",
    "capability_identity",
    "decode_capability_evidence",
    "encode_capability_evidence",
    "validate_capability_installation_identity",
]
