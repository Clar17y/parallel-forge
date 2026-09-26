"""Resolve exact Antigravity evidence without activating an unproved runtime.

No model is contacted here. The trusted source verifies all five proof artifacts,
their current PostgreSQL revision and expiry. A caller cannot substitute init-only
diagnostics or the retired Gemini ACP evidence for this distinct client contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path

from forge.agents.antigravity_capability_probe import AntigravityProbeInstallation
from forge.agents.antigravity_configuration import (
    AntigravityIsolationHome,
    antigravity_launch_argv,
    antigravity_settings,
)
from forge.agents.capability_verification import stable_executable_digest
from forge.application.ports.capability_evidence import CapabilityEvidenceSource
from forge.domain.capability_evidence import (
    CapabilityEvidenceScope,
    ResolvedCapabilityEvidence,
    capability_identity,
)
from forge.domain.subscription import ReasoningEffort, RouteSpec, SpecialistPurpose
from forge.domain.subscription_installations import is_account_identity_digest
from forge.domain.subscription_readiness import ReadinessWarning
from forge.domain.tool import ToolName

ANTIGRAVITY_VERIFIER_ID = "forge-antigravity-official"
_MODEL, _EFFORT = "gemini-3.8-flash-medium", "medium"


def _policy_digest() -> str:
    # No local path enters the digest. All generated file contents and launch
    # arguments are bound, so configuration repairs invalidate old evidence.
    from uuid import UUID

    home = AntigravityIsolationHome(Path(__file__).resolve().parent, UUID(int=1))
    payload = {
        "proof_contract": 1,
        "settings": antigravity_settings(),
        "environment_keys": ["HOME", "USERPROFILE"],
        "argv": antigravity_launch_argv(str(Path(__file__).resolve()), _MODEL, _EFFORT)[1:],
        "files": {
            str(path.relative_to(home.path).as_posix()): data.decode()
            for path, data in home.expected_files.items()
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


ANTIGRAVITY_VERIFIER_VERSION = f"1-{_policy_digest()[:16]}"


def antigravity_writer_scope() -> CapabilityEvidenceScope:
    """Only the separately proved bounded read/write/named-check writer scope."""
    return CapabilityEvidenceScope(
        route=RouteSpec(
            provider="google", client="antigravity_cli", model=_MODEL, effort=ReasoningEffort.MEDIUM
        ),
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        tool_surface=(
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_WRITE_FILE,
            ToolName.BUILD_RUN_NAMED_CHECK,
        ),
    )


@dataclass(frozen=True, slots=True)
class AntigravityCapabilityReport:
    evidence: ResolvedCapabilityEvidence | None = field(default=None, repr=False)
    warnings: tuple[ReadinessWarning, ...] = field(
        default=(ReadinessWarning.APPROVED_TOOLS_UNPROVED,),
        init=False,
    )

    def admits(
        self, installation: AntigravityProbeInstallation, scope: CapabilityEvidenceScope
    ) -> bool:
        if not _eligible(installation, scope) or not isinstance(
            self.evidence, ResolvedCapabilityEvidence
        ):
            return False
        try:
            identity = capability_identity(
                scope=scope,
                client_version=installation.client_version,
                executable_digest=installation.executable_digest,
                client_home=installation.home,
                account=installation.account,
            )
        except OSError, TypeError, ValueError:
            return False
        return (
            self.evidence.matches(identity)
            and self.evidence.permits(scope)
            and self.evidence.manifest.verifier_id == ANTIGRAVITY_VERIFIER_ID
            and self.evidence.manifest.verifier_version == ANTIGRAVITY_VERIFIER_VERSION
        )


@dataclass(frozen=True, slots=True)
class AntigravityEvidenceVerifier:
    source: CapabilityEvidenceSource = field(repr=False)

    def __post_init__(self) -> None:
        if not callable(getattr(self.source, "resolve", None)):
            raise TypeError("Antigravity verification requires a trusted evidence source")

    async def verify(
        self, installation: AntigravityProbeInstallation, scope: CapabilityEvidenceScope
    ) -> AntigravityCapabilityReport:
        denied = AntigravityCapabilityReport()
        if not _eligible(installation, scope):
            return denied
        try:
            digest = await asyncio.to_thread(stable_executable_digest, installation.executable)
            if digest is None or not hmac.compare_digest(digest, installation.executable_digest):
                return denied
            identity = capability_identity(
                scope=scope,
                client_version=installation.client_version,
                executable_digest=digest,
                client_home=installation.home,
                account=installation.account,
            )
            evidence = await self.source.resolve(identity)
            confirmed = await asyncio.to_thread(stable_executable_digest, installation.executable)
            if confirmed is None or not hmac.compare_digest(confirmed, digest):
                return denied
            report = AntigravityCapabilityReport(evidence)
            return report if report.admits(installation, scope) else denied
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - verification outages never authorize a launch
            return denied


def _eligible(installation: object, scope: object) -> bool:
    return (
        isinstance(installation, AntigravityProbeInstallation)
        and scope == antigravity_writer_scope()
        and (installation.model, installation.effort) == (_MODEL, _EFFORT)
        and is_account_identity_digest(installation.account)
        and Path(installation.executable).is_absolute()
        and Path(installation.home).is_absolute()
    )
