"""Fail-closed preflight for Antigravity, the supported Gemini successor client.

This module deliberately does not produce trusted capability evidence. A clean
zero-turn inventory is only permission to attempt separately authorized live
conformance; runtime admission remains unavailable until that evidence exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from forge.agents.gemini_gateway import GeminiCapabilityReport, GeminiInstallation
from forge.domain.capability_evidence import CapabilityEvidenceScope

ANTIGRAVITY_CLIENT_VERSION = "1.2.4"
ANTIGRAVITY_ISOLATION_AGENT = "forge-isolation-probe"
ANTIGRAVITY_UPSTREAM_ISOLATION_ISSUE = (
    "https://github.com/google-antigravity/antigravity-cli/issues/1015"
)

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_CLIENT_VERSION = re.compile(r"\A[0-9]+\.[0-9]+\.[0-9]+\Z", re.ASCII)
_AGENT_NAME = re.compile(r"\A[a-z][a-z0-9-]{0,95}\Z", re.ASCII)
_TOOL_NAME = re.compile(r"\A[a-z][a-z0-9_]{0,127}\Z", re.ASCII)


def _validated_tools(name: str, tools: tuple[str, ...]) -> tuple[str, ...]:
    if type(tools) is not tuple or any(
        type(tool) is not str or _TOOL_NAME.fullmatch(tool) is None for tool in tools
    ):
        raise ValueError(f"Antigravity {name} tools are invalid")
    canonical = tuple(sorted(set(tools)))
    if canonical != tools:
        raise ValueError(f"Antigravity {name} tools must be canonical")
    return canonical


@dataclass(frozen=True, slots=True, kw_only=True)
class AntigravityInitObservation:
    """Sanitized init-only output; it contains no prompt, response, or account data."""

    client_version: str
    executable_digest: str
    selected_agent: str
    declared_tools: tuple[str, ...]
    observed_tools: tuple[str, ...]
    prompt_count: int
    provider_turn_count: int

    def __post_init__(self) -> None:
        if (
            type(self.client_version) is not str
            or _CLIENT_VERSION.fullmatch(self.client_version) is None
            or type(self.executable_digest) is not str
            or _SHA256.fullmatch(self.executable_digest) is None
            or type(self.selected_agent) is not str
            or _AGENT_NAME.fullmatch(self.selected_agent) is None
            or type(self.prompt_count) is not int
            or self.prompt_count < 0
            or type(self.provider_turn_count) is not int
            or self.provider_turn_count < 0
        ):
            raise ValueError("Antigravity init observation is invalid")
        object.__setattr__(
            self, "declared_tools", _validated_tools("declared", self.declared_tools)
        )
        object.__setattr__(
            self, "observed_tools", _validated_tools("observed", self.observed_tools)
        )


class AntigravityPreflightFailure(StrEnum):
    CLIENT_VERSION = "client_version_differs"
    EXECUTABLE_IDENTITY = "executable_identity_differs"
    AGENT_SELECTION = "isolation_agent_differs"
    DECLARED_NATIVE_TOOLS = "native_tools_were_declared"
    OBSERVED_NATIVE_TOOLS = "native_tool_surface_not_isolated"
    ZERO_TURN_VIOLATION = "probe_was_not_zero_turn"


@dataclass(frozen=True, slots=True)
class AntigravityReadinessReport:
    """Diagnostic preflight result; never itself authorizes a production route."""

    observation: AntigravityInitObservation
    preflight_failures: tuple[AntigravityPreflightFailure, ...]
    runtime_admissible: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.observation, AntigravityInitObservation):
            raise TypeError("Antigravity readiness requires an init observation")
        if type(self.preflight_failures) is not tuple or any(
            not isinstance(item, AntigravityPreflightFailure) for item in self.preflight_failures
        ):
            raise TypeError("Antigravity readiness failures are invalid")
        if len(set(self.preflight_failures)) != len(self.preflight_failures):
            raise ValueError("Antigravity readiness failures must be unique")

    @property
    def ready_for_live_conformance(self) -> bool:
        return not self.preflight_failures

    @property
    def native_tool_extras(self) -> tuple[str, ...]:
        declared = frozenset(self.observation.declared_tools)
        return tuple(tool for tool in self.observation.observed_tools if tool not in declared)


@dataclass(frozen=True, slots=True)
class AntigravityReadinessGate:
    """Check immutable identity and an init-only empty-tool canary."""

    expected_executable_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.expected_executable_digest) is not str
            or _SHA256.fullmatch(self.expected_executable_digest) is None
        ):
            raise ValueError("Antigravity executable digest must be SHA-256")

    def assess(self, observation: AntigravityInitObservation) -> AntigravityReadinessReport:
        if not isinstance(observation, AntigravityInitObservation):
            raise TypeError("Antigravity readiness requires an init observation")
        failures: list[AntigravityPreflightFailure] = []
        if observation.client_version != ANTIGRAVITY_CLIENT_VERSION:
            failures.append(AntigravityPreflightFailure.CLIENT_VERSION)
        if observation.executable_digest != self.expected_executable_digest:
            failures.append(AntigravityPreflightFailure.EXECUTABLE_IDENTITY)
        if observation.selected_agent != ANTIGRAVITY_ISOLATION_AGENT:
            failures.append(AntigravityPreflightFailure.AGENT_SELECTION)
        if observation.declared_tools:
            failures.append(AntigravityPreflightFailure.DECLARED_NATIVE_TOOLS)
        if observation.observed_tools:
            failures.append(AntigravityPreflightFailure.OBSERVED_NATIVE_TOOLS)
        if observation.prompt_count or observation.provider_turn_count:
            failures.append(AntigravityPreflightFailure.ZERO_TURN_VIOLATION)
        return AntigravityReadinessReport(observation, tuple(failures))


@dataclass(frozen=True, slots=True)
class AntigravityReadinessVerifier:
    """Protocol-compatible denial until live evidence replaces the readiness report."""

    report: AntigravityReadinessReport = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.report, AntigravityReadinessReport):
            raise TypeError("Antigravity verifier requires a readiness report")

    async def verify(
        self, installation: GeminiInstallation, scope: CapabilityEvidenceScope
    ) -> GeminiCapabilityReport:
        if not isinstance(installation, GeminiInstallation) or not isinstance(
            scope, CapabilityEvidenceScope
        ):
            raise TypeError("Gemini installation and capability scope are required")
        # A zero-turn preflight can disprove isolation, but it cannot establish
        # authenticated model, billing, MCP, telemetry, or stopped-tree evidence.
        return GeminiCapabilityReport()


__all__ = [
    "ANTIGRAVITY_CLIENT_VERSION",
    "ANTIGRAVITY_ISOLATION_AGENT",
    "ANTIGRAVITY_UPSTREAM_ISOLATION_ISSUE",
    "AntigravityInitObservation",
    "AntigravityPreflightFailure",
    "AntigravityReadinessGate",
    "AntigravityReadinessReport",
    "AntigravityReadinessVerifier",
]
