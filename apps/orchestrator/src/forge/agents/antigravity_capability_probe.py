"""Offline Antigravity stream parser and explicitly-gated live probe shape.

This intentionally does not guess an undocumented callback protocol.  Version
1.2.7's offline material proves that default components can be excluded, but it
does not prove a receipt-bound Forge MCP dispatcher.  Consequently a clean
zero-turn stream is useful diagnostic evidence only and live admission remains
fail-closed with ``forge_callback_unproved``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forge.agents.antigravity_configuration import ANTIGRAVITY_ISOLATION_AGENT

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_VERSION = re.compile(r"\A[0-9]+\.[0-9]+\.[0-9]+\Z", re.ASCII)
_FORBIDDEN_COMPONENTS = frozenset(
    {
        "tools",
        "native_tools",
        "hooks",
        "customizations",
        "mcp",
        "mcp_servers",
        "plugins",
        "skills",
        "subagents",
        "tasks",
        "inbox",
        "ask",
        "files",
        "shell",
        "web",
        "search",
        "project",
    }
)


class AntigravityProbeError(RuntimeError):
    """Sanitized non-publishable Antigravity probe outcome."""


class AntigravityProbeFailure(StrEnum):
    PROTOCOL = "probe_protocol_failed"
    CALLBACK_UNPROVED = "forge_callback_unproved"
    ACCOUNT_UNBOUND = "account_identity_unbound"
    PROVIDER_NOT_AUTHORIZED = "provider_contact_not_authorized"


@dataclass(frozen=True, slots=True)
class AntigravityProbeInstallation:
    executable: str
    executable_digest: str
    client_version: str
    home: str
    model: str
    effort: str
    account: str

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(item, str) and item
                for item in (
                    self.executable,
                    self.client_version,
                    self.home,
                    self.model,
                    self.effort,
                    self.account,
                )
            )
            or _SHA256.fullmatch(self.executable_digest) is None
            or _VERSION.fullmatch(self.client_version) is None
        ):
            raise ValueError("Antigravity installation identity is invalid")


@dataclass(frozen=True, slots=True)
class AntigravityZeroTurnObservation:
    """Only the fields required to prove no turn or client surface was exposed."""

    client_version: str
    executable_digest: str
    account_kind: str
    account_identity_digest: str
    model: str
    effort: str
    use_g1_credits: bool
    components: tuple[str, ...]
    terminal_confirmed: bool


def parse_zero_turn_frames(
    frames: Iterable[Mapping[str, Any]], installation: AntigravityProbeInstallation
) -> AntigravityZeroTurnObservation:
    """Validate a fake/archived initialization stream without launching anything.

    The small schema deliberately rejects unknown frame kinds and *any* payload
    that looks like a request, a turn, or a surface-bearing component.  It is
    therefore safe to tighten after a separately authorized installed-client
    observation, but not to use as an optimistic compatibility parser.
    """
    if not isinstance(installation, AntigravityProbeInstallation):
        raise TypeError("Antigravity installation is required")
    init: Mapping[str, Any] | None = None
    terminal = False
    for frame in frames:
        if not isinstance(frame, Mapping):
            raise AntigravityProbeError(AntigravityProbeFailure.PROTOCOL.value)
        kind = frame.get("type")
        if kind == "initialized" and init is None and not terminal:
            init = frame
            continue
        if (
            kind == "stopped"
            and init is not None
            and not terminal
            and frame == {"type": "stopped", "confirmed": True}
        ):
            terminal = True
            continue
        raise AntigravityProbeError(AntigravityProbeFailure.PROTOCOL.value)
    if init is None or not terminal:
        raise AntigravityProbeError(AntigravityProbeFailure.PROTOCOL.value)
    required = {
        "type",
        "client_version",
        "executable_digest",
        "agent",
        "account_kind",
        "account_identity_digest",
        "model",
        "effort",
        "useG1Credits",
        "components",
    }
    if set(init) != required:
        raise AntigravityProbeError(AntigravityProbeFailure.PROTOCOL.value)
    components = init["components"]
    if (
        init["client_version"] != installation.client_version
        or not hmac.compare_digest(str(init["executable_digest"]), installation.executable_digest)
        or init["agent"] != ANTIGRAVITY_ISOLATION_AGENT
        or init["account_kind"] != "google_subscription"
        or init["model"] != installation.model
        or init["effort"] != installation.effort
        or init["useG1Credits"] is not False
        or type(components) is not list
        or any(type(item) is not str for item in components)
        or components
    ):
        raise AntigravityProbeError(AntigravityProbeFailure.PROTOCOL.value)
    account = init["account_identity_digest"]
    if type(account) is not str or _SHA256.fullmatch(account) is None:
        raise AntigravityProbeError(AntigravityProbeFailure.ACCOUNT_UNBOUND.value)
    if not hmac.compare_digest(account, installation.account):
        raise AntigravityProbeError(AntigravityProbeFailure.ACCOUNT_UNBOUND.value)
    return AntigravityZeroTurnObservation(
        client_version=installation.client_version,
        executable_digest=installation.executable_digest,
        account_kind="google_subscription",
        account_identity_digest=account,
        model=installation.model,
        effort=installation.effort,
        use_g1_credits=False,
        components=(),
        terminal_confirmed=True,
    )


def zero_turn_observation_digest(observation: AntigravityZeroTurnObservation) -> str:
    """Stable opaque evidence handle; no raw frame/provider output survives."""
    if not isinstance(observation, AntigravityZeroTurnObservation):
        raise TypeError("Antigravity observation is required")
    payload = {
        "client_version": observation.client_version,
        "executable_digest": observation.executable_digest,
        "account_kind": observation.account_kind,
        "account_identity_digest": observation.account_identity_digest,
        "model": observation.model,
        "effort": observation.effort,
        "use_g1_credits": observation.use_g1_credits,
        "components": list(observation.components),
        "terminal_confirmed": observation.terminal_confirmed,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AntigravityCapabilityProbe:
    """An opt-in probe whose live turn is deliberately unavailable for now."""

    def __init__(self, installation: AntigravityProbeInstallation) -> None:
        self.installation = installation

    def observe_offline(
        self, frames: Iterable[Mapping[str, Any]]
    ) -> AntigravityZeroTurnObservation:
        return parse_zero_turn_frames(frames, self.installation)

    async def observe(self, *, authorize_provider_contact: bool = False) -> None:
        if authorize_provider_contact is not True:
            raise AntigravityProbeError(AntigravityProbeFailure.PROVIDER_NOT_AUTHORIZED.value)
        # No generic dispatcher/receipt correlation has been established from
        # the installed client.  Never launch merely to discover it implicitly.
        raise AntigravityProbeError(AntigravityProbeFailure.CALLBACK_UNPROVED.value)


__all__ = [
    "AntigravityCapabilityProbe",
    "AntigravityProbeError",
    "AntigravityProbeFailure",
    "AntigravityProbeInstallation",
    "AntigravityZeroTurnObservation",
    "parse_zero_turn_frames",
    "zero_turn_observation_digest",
]
