"""Canonical, identity-bound proof envelopes for capability evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from forge.domain.capability_evidence import CapabilityEvidenceManifest, CapabilityProofKind

MAX_PROOF_BYTES = 16 * 1024
_MAX_DEPTH = 8
_MAX_STRING = 512
_MAX_ITEMS = 64
_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "identity_digest",
        "verifier_id",
        "verifier_version",
        "observed_at",
        "expires_at",
        "payload",
    }
)


class CapabilityProofError(ValueError):
    """A proof artifact is malformed, non-canonical, or not bound to its manifest."""


def encode_proof(
    kind: CapabilityProofKind, payload: Mapping[str, object], manifest: CapabilityEvidenceManifest
) -> bytes:
    """Validate and canonically encode one closed proof envelope."""
    _validate_kind_manifest(kind, manifest)
    envelope = _envelope(kind, _validate_payload(kind, payload, manifest), manifest)
    try:
        wire = json.dumps(
            envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except TypeError, ValueError, RecursionError:
        raise CapabilityProofError("capability proof is invalid") from None
    if len(wire) > MAX_PROOF_BYTES:
        raise CapabilityProofError("capability proof exceeds limit")
    return wire


def validate_proof(
    wire: bytes, kind: CapabilityProofKind, manifest: CapabilityEvidenceManifest
) -> None:
    """Require exact canonical bytes and semantics for ``kind`` and ``manifest``."""
    _validate_kind_manifest(kind, manifest)
    if type(wire) is not bytes or not wire or len(wire) > MAX_PROOF_BYTES:
        raise CapabilityProofError("capability proof is invalid")
    try:
        value = json.loads(
            wire.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError:
        raise CapabilityProofError("capability proof is invalid") from None
    _validate_shape(value)
    if (
        value["schema_version"] != 1
        or value["kind"] != kind.value
        or value["identity_digest"] != manifest.identity.digest
        or value["verifier_id"] != manifest.verifier_id
        or value["verifier_version"] != manifest.verifier_version
        or value["observed_at"] != manifest.observed_at.isoformat()
        or value["expires_at"] != manifest.expires_at.isoformat()
    ):
        raise CapabilityProofError("capability proof binding differs")
    if encode_proof(kind, value["payload"], manifest) != wire:
        raise CapabilityProofError("capability proof is not canonical")


def _envelope(
    kind: CapabilityProofKind, payload: dict[str, object], manifest: CapabilityEvidenceManifest
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": kind.value,
        "identity_digest": manifest.identity.digest,
        "verifier_id": manifest.verifier_id,
        "verifier_version": manifest.verifier_version,
        "observed_at": manifest.observed_at.isoformat(),
        "expires_at": manifest.expires_at.isoformat(),
        "payload": payload,
    }


def _validate_kind_manifest(kind: object, manifest: object) -> None:
    if not isinstance(kind, CapabilityProofKind) or not isinstance(
        manifest, CapabilityEvidenceManifest
    ):
        raise CapabilityProofError("capability proof binding is invalid")


def _validate_shape(value: object) -> None:
    _validate_json_value(value)
    if (
        not isinstance(value, dict)
        or set(value) != _ENVELOPE_KEYS
        or type(value["schema_version"]) is not int
    ):
        raise CapabilityProofError("capability proof is invalid")
    for key in _ENVELOPE_KEYS - {"schema_version", "payload"}:
        if type(value[key]) is not str:
            raise CapabilityProofError("capability proof is invalid")
    if not isinstance(value["payload"], dict):
        raise CapabilityProofError("capability proof is invalid")


def _validate_payload(
    kind: CapabilityProofKind, payload: Mapping[str, object], manifest: CapabilityEvidenceManifest
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise CapabilityProofError("capability proof payload is invalid")
    result = dict(payload)
    _validate_json_value(result)
    identity = manifest.identity
    expected: dict[CapabilityProofKind, dict[str, object]] = {
        CapabilityProofKind.CLIENT_IDENTITY: {
            "client": identity.client,
            "client_version": identity.client_version,
            "executable_digest": identity.executable_digest,
            "client_home_digest": identity.client_home_digest,
            "executable_unchanged": True,
        },
        CapabilityProofKind.ACCOUNT_AUTHENTICATION: {
            "account": identity.account,
            "auth_mode": identity.auth_mode.value,
            "authenticated": True,
        },
        CapabilityProofKind.ROUTE_IDENTITY: {
            "model": identity.model,
            "effort": identity.effort.value,
            "catalog_supported": True,
            "turn_completed": True,
        },
        CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING: {
            "auth_mode": identity.auth_mode.value,
            "billing_mode": identity.billing_mode.value,
            "paid_credential_names_scrubbed": True,
            "fallback_disabled": True,
            "subscription_route_observed": True,
        },
        CapabilityProofKind.TOOL_ISOLATION: {
            "tool_surface": [tool.value for tool in identity.tool_surface],
            "isolated": True,
            "forbidden_tool_calls": 0,
            "side_effect_canaries_clear": True,
        },
    }
    dynamic = {
        CapabilityProofKind.CLIENT_IDENTITY: {"reported_client_version"},
        CapabilityProofKind.ACCOUNT_AUTHENTICATION: {"account_kind"},
        CapabilityProofKind.ROUTE_IDENTITY: {"turn_observation_digest"},
        CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING: set(),
        CapabilityProofKind.TOOL_ISOLATION: {"advertised_tool_surface_digest"},
    }[kind]
    if set(result) != set(expected[kind]) | dynamic or any(
        result[key] != value for key, value in expected[kind].items()
    ):
        raise CapabilityProofError("capability proof payload differs")
    if (
        kind is CapabilityProofKind.CLIENT_IDENTITY
        and result["reported_client_version"] != identity.client_version
    ):
        raise CapabilityProofError("capability proof payload differs")
    if kind is CapabilityProofKind.ACCOUNT_AUTHENTICATION and result["account_kind"] not in (
        "chatgpt",
        "subscription",
    ):
        raise CapabilityProofError("capability proof payload differs")
    dynamic_value = result[next(iter(dynamic))] if dynamic else None
    if kind in (CapabilityProofKind.ROUTE_IDENTITY, CapabilityProofKind.TOOL_ISOLATION) and (
        type(dynamic_value) is not str
        or len(dynamic_value) != 64
        or any(character not in "0123456789abcdef" for character in dynamic_value)
    ):
        raise CapabilityProofError("capability proof payload differs")
    return result


def _validate_json_value(value: object, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise CapabilityProofError("capability proof is too deeply nested")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        raise CapabilityProofError("capability proof contains a number")
    if type(value) is str:
        if len(value) > _MAX_STRING:
            raise CapabilityProofError("capability proof string exceeds limit")
        return
    if isinstance(value, dict):
        if len(value) > _MAX_ITEMS or any(type(key) is not str for key in value):
            raise CapabilityProofError("capability proof object is invalid")
        for key, item in value.items():
            _validate_json_value(key, depth + 1)
            _validate_json_value(item, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            raise CapabilityProofError("capability proof collection exceeds limit")
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    raise CapabilityProofError("capability proof contains an invalid value")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapabilityProofError("capability proof contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON constant")


__all__ = ["MAX_PROOF_BYTES", "CapabilityProofError", "encode_proof", "validate_proof"]
