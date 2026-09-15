"""Trusted evidence resolver for the pinned official Codex client."""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass, field

from forge.agents.capability_verification import stable_executable_digest
from forge.agents.codex_gateway import (
    CODEX_CLIENT_VERSION,
    CODEX_ISOLATION_POLICY_DIGEST,
    CODEX_MODEL_CATALOG_DIGEST,
    CodexCapabilityReport,
    CodexInstallation,
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

CODEX_VERIFIER_ID = "forge-codex-official"
CODEX_VERIFIER_VERSION = f"2-{CODEX_ISOLATION_POLICY_DIGEST[:16]}-{CODEX_MODEL_CATALOG_DIGEST[:16]}"


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexVerificationScope:
    """One exact Codex role/tool identity requiring its own conformance proof."""

    name: str
    model: str
    effort: str
    role: SpecialistPurpose
    tool_surface: tuple[ToolName, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.model or not isinstance(self.role, SpecialistPurpose):
            raise ValueError("Codex verification scope is invalid")
        ReasoningEffort(self.effort)
        canonical = tuple(sorted(set(self.tool_surface), key=lambda item: item.value))
        if canonical != self.tool_surface or ToolName.REPOSITORY_READ_FILE not in canonical:
            raise ValueError(
                "Codex verification tools must be canonical and support a read callback"
            )
        for tool in canonical:
            if tool not in SPECIALIST_ALLOWED_TOOLS[self.role]:
                raise ValueError("Codex verification tool is not allowed for the role")

    @property
    def identity_key(self) -> tuple[str, str, SpecialistPurpose, tuple[ToolName, ...]]:
        return self.model, self.effort, self.role, self.tool_surface

    def evidence_scope(self) -> CapabilityEvidenceScope:
        return CapabilityEvidenceScope(
            route=RouteSpec(
                provider="openai",
                client="codex_app_server",
                model=self.model,
                effort=ReasoningEffort(self.effort),
            ),
            role=self.role,
            tool_surface=self.tool_surface,
        )


def _verification_scope(
    name: str,
    model: str,
    effort: str,
    role: SpecialistPurpose,
    tools: frozenset[ToolName] | set[ToolName],
) -> CodexVerificationScope:
    return CodexVerificationScope(
        name=name,
        model=model,
        effort=effort,
        role=role,
        tool_surface=tuple(sorted(tools, key=lambda item: item.value)),
    )


_DIRECT_REPAIR_TOOLS = {
    ToolName.REPOSITORY_READ_FILE,
    ToolName.REPOSITORY_WRITE_FILE,
    ToolName.GIT_COMMIT,
    ToolName.GIT_DIFF,
    ToolName.BUILD_RUN_NAMED_CHECK,
}
_REQUIRED_SCOPES = (
    _verification_scope(
        "astra-primary",
        "gpt-6-astra",
        "low",
        SpecialistPurpose.PRIMARY,
        SPECIALIST_ALLOWED_TOOLS[SpecialistPurpose.PRIMARY],
    ),
    _verification_scope(
        "astra-direct-repair",
        "gpt-6-astra",
        "low",
        SpecialistPurpose.PRIMARY,
        _DIRECT_REPAIR_TOOLS,
    ),
    _verification_scope(
        "sol-planning-adversarial",
        "gpt-5.6-sol",
        "low",
        SpecialistPurpose.PLANNING,
        SPECIALIST_ALLOWED_TOOLS[SpecialistPurpose.PLANNING],
    ),
    _verification_scope(
        "sol-high-risk-correctness",
        "gpt-5.6-sol",
        "high",
        SpecialistPurpose.VERIFICATION,
        SPECIALIST_ALLOWED_TOOLS[SpecialistPurpose.VERIFICATION],
    ),
    _verification_scope(
        "sol-security",
        "gpt-5.6-sol",
        "high",
        SpecialistPurpose.SECURITY,
        SPECIALIST_ALLOWED_TOOLS[SpecialistPurpose.SECURITY],
    ),
)
_REQUIRED_EVIDENCE_SCOPES = frozenset(item.evidence_scope() for item in _REQUIRED_SCOPES)


def required_codex_verification_scopes() -> tuple[CodexVerificationScope, ...]:
    """Return the closed set of Codex route identities eligible for evidence."""

    return _REQUIRED_SCOPES


@dataclass(frozen=True, slots=True)
class CodexEvidenceVerifier:
    """Admit only current official-client evidence for the exact executable and scope."""

    source: CapabilityEvidenceSource = field(repr=False)

    def __post_init__(self) -> None:
        if not callable(getattr(self.source, "resolve", None)):
            raise TypeError("Codex verification requires a trusted evidence source")

    async def verify(
        self, installation: CodexInstallation, scope: CapabilityEvidenceScope
    ) -> CodexCapabilityReport:
        if not isinstance(installation, CodexInstallation) or not isinstance(
            scope, CapabilityEvidenceScope
        ):
            raise TypeError("Codex installation and capability scope are required")
        route = scope.route
        if (
            installation.script != ("app-server", "--stdio")
            or scope not in _REQUIRED_EVIDENCE_SCOPES
            or route.provider != "openai"
            or route.client != "codex_app_server"
            or route.model != installation.model
            or route.effort.value != installation.effort
            or route.auth_mode is not AuthMode.SUBSCRIPTION
            or route.billing_mode is not BillingMode.ALLOWANCE_ONLY
        ):
            return CodexCapabilityReport.unavailable("Codex evidence scope differs")

        actual_digest = await asyncio.to_thread(codex_executable_digest, installation.executable)
        if actual_digest is None or not hmac.compare_digest(
            actual_digest, installation.executable_digest
        ):
            return CodexCapabilityReport.unavailable("Codex executable identity differs")

        identity = capability_identity(
            scope=scope,
            client_version=CODEX_CLIENT_VERSION,
            executable_digest=actual_digest,
            client_home=installation.client_home,
            account=installation.account,
        )
        evidence = await self.source.resolve(identity)
        confirmed_digest = await asyncio.to_thread(codex_executable_digest, installation.executable)
        if (
            confirmed_digest is None
            or not hmac.compare_digest(confirmed_digest, actual_digest)
            or not isinstance(evidence, ResolvedCapabilityEvidence)
            or not evidence.matches(identity)
            or evidence.manifest.verifier_id != CODEX_VERIFIER_ID
            or evidence.manifest.verifier_version != CODEX_VERIFIER_VERSION
        ):
            return CodexCapabilityReport.unavailable("Codex capability evidence is not trusted")
        return CodexCapabilityReport(
            supported=True,
            installed_version=CODEX_CLIENT_VERSION,
            account_kind="chatgpt",
            billing_allowance_enforced=True,
            native_tools_isolated=True,
            model=installation.model,
            effort=installation.effort,
            quota_limit_id=installation.quota_limit_id,
            client_home=installation.client_home,
            account=installation.account,
            executable_digest=actual_digest,
            evidence=evidence,
        )


def codex_executable_digest(filename: str) -> str | None:
    """Hash one stable regular file without exposing filesystem diagnostics."""

    return stable_executable_digest(filename)


__all__ = [
    "CODEX_CLIENT_VERSION",
    "CODEX_VERIFIER_ID",
    "CODEX_VERIFIER_VERSION",
    "CodexEvidenceVerifier",
    "CodexVerificationScope",
    "codex_executable_digest",
    "required_codex_verification_scopes",
]
