"""Trusted evidence resolver for the pinned official Claude client."""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass, field

from forge.agents import claude_gateway
from forge.agents.capability_verification import stable_executable_digest
from forge.agents.claude_gateway import (
    CLAUDE_CLIENT_VERSION,
    CLAUDE_ISOLATION_POLICY_DIGEST,
    ClaudeCapabilityReport,
    ClaudeInstallation,
)
from forge.application.ports.capability_evidence import CapabilityEvidenceSource
from forge.domain.capability_evidence import (
    CapabilityEvidenceScope,
    ResolvedCapabilityEvidence,
    capability_identity,
)
from forge.domain.subscription import (
    SPECIALIST_ALLOWED_TOOLS,
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteSpec,
    SpecialistPurpose,
)
from forge.domain.tool import ToolName

CLAUDE_VERIFIER_ID = "forge-claude-official"
CLAUDE_VERIFIER_VERSION = f"1-{CLAUDE_ISOLATION_POLICY_DIGEST[:16]}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeVerificationScope:
    """One exact Claude role/tool identity requiring its own conformance proof."""

    name: str
    model: str
    effort: str
    role: SpecialistPurpose
    tool_surface: tuple[ToolName, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.model or not isinstance(self.role, SpecialistPurpose):
            raise ValueError("Claude verification scope is invalid")
        ReasoningEffort(self.effort)
        canonical = tuple(sorted(set(self.tool_surface), key=lambda item: item.value))
        if canonical != self.tool_surface or ToolName.REPOSITORY_READ_FILE not in canonical:
            raise ValueError(
                "Claude verification tools must be canonical and support a read callback"
            )
        for tool in canonical:
            if tool not in SPECIALIST_ALLOWED_TOOLS[self.role]:
                raise ValueError("Claude verification tool is not allowed for the role")

    def evidence_scope(self) -> CapabilityEvidenceScope:
        return CapabilityEvidenceScope(
            route=RouteSpec(
                provider="anthropic",
                client="claude_code",
                model=self.model,
                effort=ReasoningEffort(self.effort),
            ),
            role=self.role,
            tool_surface=self.tool_surface,
        )


_REQUIRED_SCOPES = (
    ClaudeVerificationScope(
        name="opus-independent-review",
        model="claude-opus-5",
        effort="medium",
        role=SpecialistPurpose.INDEPENDENT_REVIEW,
        tool_surface=tuple(
            sorted(
                SPECIALIST_ALLOWED_TOOLS[SpecialistPurpose.INDEPENDENT_REVIEW],
                key=lambda item: item.value,
            )
        ),
    ),
)
_REQUIRED_EVIDENCE_SCOPES = frozenset(item.evidence_scope() for item in _REQUIRED_SCOPES)


def required_claude_verification_scopes() -> tuple[ClaudeVerificationScope, ...]:
    """Return the sole Claude route identity eligible for evidence."""

    return _REQUIRED_SCOPES


@dataclass(frozen=True, slots=True)
class ClaudeEvidenceVerifier:
    """Admit current evidence for one exact official-client review scope."""

    source: CapabilityEvidenceSource = field(repr=False)

    def __post_init__(self) -> None:
        if not callable(getattr(self.source, "resolve", None)):
            raise TypeError("Claude verification requires a trusted evidence source")

    async def verify(
        self, installation: ClaudeInstallation, scope: CapabilityEvidenceScope
    ) -> ClaudeCapabilityReport:
        if not isinstance(installation, ClaudeInstallation) or not isinstance(
            scope, CapabilityEvidenceScope
        ):
            raise TypeError("Claude installation and capability scope are required")
        if not await asyncio.to_thread(claude_gateway.claude_isolation_platform_supported):
            return ClaudeCapabilityReport()
        route = scope.route
        if (
            installation.script
            != ("-p", "--input-format", "stream-json", "--output-format", "stream-json")
            or scope not in _REQUIRED_EVIDENCE_SCOPES
            or route.provider != "anthropic"
            or route.client != "claude_code"
            or route.model != installation.model
            or route.effort.value != installation.effort
            or route.auth_mode is not AuthMode.SUBSCRIPTION
            or route.billing_mode is not BillingMode.ALLOWANCE_ONLY
        ):
            return ClaudeCapabilityReport()

        actual_digest = await asyncio.to_thread(stable_executable_digest, installation.executable)
        if actual_digest is None or not hmac.compare_digest(
            actual_digest, installation.executable_digest
        ):
            return ClaudeCapabilityReport()

        identity = capability_identity(
            scope=scope,
            client_version=CLAUDE_CLIENT_VERSION,
            executable_digest=actual_digest,
            client_home=installation.client_home,
            account=installation.account,
        )
        evidence = await self.source.resolve(identity)
        confirmed_digest = await asyncio.to_thread(
            stable_executable_digest, installation.executable
        )
        if (
            confirmed_digest is None
            or not hmac.compare_digest(confirmed_digest, actual_digest)
            or not isinstance(evidence, ResolvedCapabilityEvidence)
            or not evidence.matches(identity)
            or evidence.manifest.verifier_id != CLAUDE_VERIFIER_ID
            or evidence.manifest.verifier_version != CLAUDE_VERIFIER_VERSION
        ):
            return ClaudeCapabilityReport()
        return ClaudeCapabilityReport(
            installed_version=CLAUDE_CLIENT_VERSION,
            subscription_auth=True,
            model=installation.model,
            effort=installation.effort,
            builtins_disabled=True,
            hooks_disabled=True,
            strict_mcp=True,
            allowance_only_enforced=True,
            quota_limit_types=installation.quota_limit_types,
            client_home=installation.client_home,
            account=installation.account,
            executable_digest=actual_digest,
            evidence=evidence,
        )


__all__ = [
    "CLAUDE_VERIFIER_ID",
    "CLAUDE_VERIFIER_VERSION",
    "ClaudeEvidenceVerifier",
    "ClaudeVerificationScope",
    "required_claude_verification_scopes",
]
