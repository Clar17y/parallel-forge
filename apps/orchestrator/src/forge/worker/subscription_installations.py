"""Resolve closed operator installation records into trusted runtime adapters."""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from forge.domain.subscription_installations import (
    ClaudeInstallationSpec,
    CodexInstallationSpec,
    GeminiInstallationSpec,
    load_subscription_installation_manifest,
    quota_route_for,
)
from forge.persistence.repositories.capability_evidence import PostgresCapabilityEvidenceSource

if TYPE_CHECKING:
    from forge.settings import Settings

logger = logging.getLogger(__name__)


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
        # Issue #10 established no admissible Antigravity/Gemini production verifier.
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
            adapter = _adapter_for(item, verifiers)
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


def _adapter_for(
    item: CodexInstallationSpec | ClaudeInstallationSpec | GeminiInstallationSpec,
    verifiers: SubscriptionVerifierDependencies,
) -> SubscriptionRuntimeAdapter | None:
    actual_digest = stable_executable_digest(item.executable)
    if actual_digest is None or not hmac.compare_digest(actual_digest, item.executable_digest):
        return None
    if isinstance(item, CodexInstallationSpec):
        if verifiers.codex is None:
            return None
        return CodexRuntimeAdapter(
            CodexInstallation(
                executable=item.executable,
                cwd=item.cwd,
                client_home=item.home,
                model=item.model,
                effort=item.effort,
                account=item.account,
                executable_digest=item.executable_digest,
                client_version=item.client_version,
                quota_limit_id=item.quota_limit_id,
                disabled_mcp_servers=item.disabled_mcp_servers,
            ),
            verifiers.codex,
        )
    if isinstance(item, ClaudeInstallationSpec):
        if verifiers.claude is None:
            return None
        return ClaudeRuntimeAdapter(
            ClaudeInstallation(
                executable=item.executable,
                cwd=item.cwd,
                client_home=item.home,
                model=item.model,
                effort=item.effort,
                account=item.account,
                executable_digest=item.executable_digest,
                client_version=item.client_version,
                quota_limit_types=frozenset(item.quota_limit_types),
            ),
            verifiers.claude,
        )
    if verifiers.gemini is None:
        return None
    return GeminiRuntimeAdapter(
        GeminiInstallation(
            executable=item.executable,
            cwd=item.cwd,
            home=item.home,
            model=item.model,
            effort=item.effort,
            account=item.account,
            executable_digest=item.executable_digest,
        ),
        verifiers.gemini,
    )


__all__ = [
    "SubscriptionVerifierDependencies",
    "load_subscription_installations",
    "production_subscription_verifiers",
]
