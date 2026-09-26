"""Resolve closed operator installation records into trusted runtime adapters."""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.agents.antigravity_runtime import AntigravityInstallation, AntigravityRuntimeAdapter
from forge.agents.capability_verification import stable_executable_digest
from forge.agents.claude_gateway import ClaudeCapabilityVerifier, ClaudeInstallation
from forge.agents.claude_runtime import ClaudeRuntimeAdapter
from forge.agents.claude_verification import ClaudeEvidenceVerifier
from forge.agents.codex_gateway import CodexCapabilityVerifier, CodexInstallation
from forge.agents.codex_runtime import CodexRuntimeAdapter
from forge.agents.codex_verification import CodexEvidenceVerifier
from forge.agents.gemini_gateway import GeminiCapabilityVerifier, GeminiInstallation
from forge.agents.gemini_runtime import GeminiRuntimeAdapter
from forge.agents.runtime_factory import SubscriptionRuntimeAdapter
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.local_cli import LocalCliTrust
from forge.domain.subscription import ReasoningEffort, RouteSpec
from forge.domain.subscription_installations import (
    AntigravityInstallationSpec,
    ClaudeInstallationSpec,
    CodexInstallationSpec,
    SubscriptionInstallationSpec,
    load_subscription_installation_manifest,
    quota_route_for,
)
from forge.domain.subscription_readiness import (
    ReadinessReason,
    ReadinessWarning,
    SubscriptionRouteReadiness,
)
from forge.persistence.repositories.capability_evidence import PostgresCapabilityEvidenceSource

if TYPE_CHECKING:
    from forge.settings import Settings

logger = logging.getLogger(__name__)
_DIGEST_UNSET = object()


@dataclass(frozen=True, slots=True)
class SubscriptionInstallationLoad:
    adapters: tuple[SubscriptionRuntimeAdapter, ...]
    readiness: tuple[SubscriptionRouteReadiness, ...]
    specs: tuple[SubscriptionInstallationSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class SubscriptionVerifierDependencies:
    """Code-owned verifier dependencies; operator data cannot select implementations."""

    codex: CodexCapabilityVerifier | None = field(default=None, repr=False)
    claude: ClaudeCapabilityVerifier | None = field(default=None, repr=False)
    gemini: GeminiCapabilityVerifier | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for verifier in (self.codex, self.claude, self.gemini):
            if verifier is not None and not callable(getattr(verifier, "verify", None)):
                raise TypeError("subscription verifier must implement the trusted boundary")


def production_subscription_verifiers(
    session_factory: async_sessionmaker[AsyncSession], artifact_root: Path
) -> SubscriptionVerifierDependencies:
    """Compose code-owned production verifiers over durable trusted evidence."""

    source = PostgresCapabilityEvidenceSource(
        session_factory, FilesystemArtifactStore(artifact_root)
    )
    return SubscriptionVerifierDependencies(
        codex=CodexEvidenceVerifier(source),
        claude=ClaudeEvidenceVerifier(source),
        # Strict admission remains separate from the personal Antigravity
        # adapter. Never attach its verifier to the legacy Gemini ACP protocol.
        gemini=None,
    )


def load_subscription_installations(
    settings: Settings,
    verifiers: SubscriptionVerifierDependencies,
) -> tuple[SubscriptionRuntimeAdapter, ...]:
    """Load only stable files, fixed client scripts and exact quota mappings."""

    if not isinstance(verifiers, SubscriptionVerifierDependencies):
        raise TypeError("subscription verifier dependencies are required")
    configured_path = getattr(settings, "subscription_installations_path", None)
    manifest = load_subscription_installation_manifest(configured_path)
    if manifest is None:
        if configured_path is not None:
            logger.warning("Subscription installation manifest is unavailable")
        return ()
    adapters: list[SubscriptionRuntimeAdapter] = []
    for item in manifest.installations:
        try:
            adapter = _adapter_for(
                item,
                verifiers,
                trust=settings.subscription_client_trust,
                duration_seconds=settings.subscription_attempt_budget.max_duration_seconds,
            )
            if adapter is None:
                logger.warning("Subscription installation is unavailable")
                continue
            expected = quota_route_for(item)
            actual = settings.subscription_quota_policy.key_for(adapter.route)
            if (actual.provider, actual.account, actual.pool) != (
                expected.provider,
                expected.account,
                expected.pool,
            ):
                logger.warning("Subscription installation quota mapping is unavailable")
                return ()
            adapters.append(adapter)
        except OSError, TypeError, ValueError:
            logger.warning("Subscription installation is unavailable")
    return tuple(adapters)


def load_subscription_installations_diagnostic(
    settings: Settings, verifiers: SubscriptionVerifierDependencies
) -> SubscriptionInstallationLoad:
    """Load adapters while retaining a redacted diagnostic for every valid item."""
    configured_path = getattr(settings, "subscription_installations_path", None)
    manifest = load_subscription_installation_manifest(configured_path)
    if manifest is None:
        if configured_path is not None:
            logger.warning("Subscription installation manifest is unavailable")
        return SubscriptionInstallationLoad((), ())
    adapters: list[SubscriptionRuntimeAdapter] = []
    admitted: set[RouteSpec] = set()
    diagnostics: list[SubscriptionRouteReadiness] = []
    trust = settings.subscription_client_trust
    for item in manifest.installations:
        warnings: tuple[ReadinessWarning, ...] = (
            (ReadinessWarning.APPROVED_TOOLS_UNPROVED,)
            if item.client in {"gemini_cli", "antigravity_cli"}
            else ()
        )
        if trust is LocalCliTrust.OPERATOR:
            warnings += (ReadinessWarning.OPERATOR_TRUSTED,)
        try:
            route = RouteSpec(
                provider={
                    "codex_app_server": "openai",
                    "claude_code": "anthropic",
                    "gemini_cli": "google",
                    "antigravity_cli": "google",
                }[item.client],
                client=item.client,
                model=item.model,
                effort=ReasoningEffort(item.effort),
            )
        except TypeError, ValueError:
            continue
        actual_digest = stable_executable_digest(item.executable)
        if (
            trust is LocalCliTrust.VERIFIED
            and item.client == "gemini_cli"
            and verifiers.gemini is None
        ):
            reason = ReadinessReason.PROVIDER_UNSUPPORTED
        elif actual_digest is None:
            reason = ReadinessReason.MISSING_EXECUTABLE
        elif trust is LocalCliTrust.VERIFIED and not hmac.compare_digest(
            actual_digest, item.executable_digest
        ):
            reason = ReadinessReason.EXECUTABLE_DIGEST_MISMATCH
        else:
            try:
                candidate = _adapter_for(
                    item,
                    verifiers,
                    actual_digest=actual_digest,
                    trust=trust,
                    duration_seconds=settings.subscription_attempt_budget.max_duration_seconds,
                )
                if candidate is None:
                    reason = ReadinessReason.PROVIDER_UNSUPPORTED
                else:
                    expected = quota_route_for(item)
                    actual = settings.subscription_quota_policy.key_for(candidate.route)
                    if (actual.provider, actual.account, actual.pool) != (
                        expected.provider,
                        expected.account,
                        expected.pool,
                    ):
                        reason = ReadinessReason.CONFIGURATION_INVALID
                    else:
                        adapters.append(candidate)
                        admitted.add(candidate.route)
                        reason = ReadinessReason.EVIDENCE_MISSING
            except TypeError, ValueError:
                reason = ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT
        if route in admitted:
            # Construction only proves local admission.  Evidence is deliberately
            # not invoked by this loader, so it cannot claim capability-ready.
            reason = (
                ReadinessReason.OPERATOR_TRUSTED
                if trust is LocalCliTrust.OPERATOR
                else ReadinessReason.EVIDENCE_MISSING
            )
            if actual_digest != item.executable_digest:
                warnings += (ReadinessWarning.CLIENT_BUILD_CHANGED,)
        diagnostics.append(
            SubscriptionRouteReadiness(
                route,
                True,
                route in admitted,
                reason,
                warnings=warnings,
            )
        )
    return SubscriptionInstallationLoad(tuple(adapters), tuple(diagnostics), manifest.installations)


def _adapter_for(
    item: SubscriptionInstallationSpec,
    verifiers: SubscriptionVerifierDependencies,
    *,
    actual_digest: str | None | object = _DIGEST_UNSET,
    trust: LocalCliTrust = LocalCliTrust.VERIFIED,
    duration_seconds: float = 300,
) -> SubscriptionRuntimeAdapter | None:
    if actual_digest is _DIGEST_UNSET:
        actual_digest = stable_executable_digest(item.executable)
    assert actual_digest is None or isinstance(actual_digest, str)
    if actual_digest is None or (
        trust is LocalCliTrust.VERIFIED
        and not hmac.compare_digest(actual_digest, item.executable_digest)
    ):
        return None
    if isinstance(item, AntigravityInstallationSpec):
        if trust is not LocalCliTrust.OPERATOR:
            return None
        return AntigravityRuntimeAdapter(
            AntigravityInstallation(
                executable=item.executable,
                cwd=item.cwd,
                home=item.home,
                model=item.model,
                effort=item.effort,
                executable_digest=actual_digest,
                duration_seconds=duration_seconds,
            )
        )
    if isinstance(item, CodexInstallationSpec):
        if verifiers.codex is None and trust is LocalCliTrust.VERIFIED:
            return None
        return CodexRuntimeAdapter(
            CodexInstallation(
                executable=item.executable,
                cwd=item.cwd,
                client_home=item.home,
                model=item.model,
                effort=item.effort,
                account=item.account,
                executable_digest=actual_digest,
                client_version=item.client_version,
                quota_limit_id=item.quota_limit_id,
                disabled_mcp_servers=item.disabled_mcp_servers,
                duration_seconds=duration_seconds,
            ),
            verifiers.codex,
            trust=trust,
        )
    if isinstance(item, ClaudeInstallationSpec):
        if verifiers.claude is None and trust is LocalCliTrust.VERIFIED:
            return None
        return ClaudeRuntimeAdapter(
            ClaudeInstallation(
                executable=item.executable,
                cwd=item.cwd,
                client_home=item.home,
                model=item.model,
                effort=item.effort,
                account=item.account,
                executable_digest=actual_digest,
                client_version=item.client_version,
                quota_limit_types=frozenset(item.quota_limit_types),
                duration_seconds=duration_seconds,
            ),
            verifiers.claude,
            trust=trust,
        )
    if verifiers.gemini is None and trust is LocalCliTrust.VERIFIED:
        return None
    return GeminiRuntimeAdapter(
        GeminiInstallation(
            executable=item.executable,
            cwd=item.cwd,
            home=item.home,
            model=item.model,
            effort=item.effort,
            account=item.account,
            executable_digest=actual_digest,
            duration_seconds=duration_seconds,
        ),
        verifiers.gemini,
        trust=trust,
    )


__all__ = [
    "SubscriptionInstallationLoad",
    "SubscriptionVerifierDependencies",
    "load_subscription_installations",
    "load_subscription_installations_diagnostic",
    "production_subscription_verifiers",
]
