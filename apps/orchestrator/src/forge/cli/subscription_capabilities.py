"""Operator-facing Codex capability evidence inspection and publication."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import typer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from forge.agents.capability_verification import stable_executable_digest
from forge.agents.claude_capability_probe import (
    ClaudeCapabilityProbe,
    SupervisedClaudeAuthStatusRunner,
    SupervisedClaudeLiveRouteRunner,
    claude_offline_scope,
    validate_claude_offline_conformance,
)
from forge.agents.claude_conformance import (
    ClaudeConformanceResult,
    ClaudeOfficialConformanceHarness,
)
from forge.agents.claude_gateway import ClaudeInstallation
from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
    ClaudeVerificationScope,
    required_claude_verification_scopes,
)
from forge.agents.codex_capability_probe import (
    CodexCapabilityProbe,
    codex_offline_scope,
    validate_codex_offline_conformance,
)
from forge.agents.codex_conformance import CodexConformanceResult, CodexOfficialConformanceHarness
from forge.agents.codex_gateway import CodexInstallation
from forge.agents.codex_verification import (
    CODEX_VERIFIER_ID,
    CODEX_VERIFIER_VERSION,
    CodexVerificationScope,
    codex_executable_digest,
    required_codex_verification_scopes,
)
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.capability_diagnostics import (
    CapabilityProbeDiagnosticSink,
    CapabilityProbeDiagnosticSource,
)
from forge.application.ports.capability_evidence import (
    CapabilityEvidenceMissing,
    CapabilityEvidenceSource,
    CapabilityEvidenceUnavailable,
)
from forge.application.services.subscription_capability_evidence import (
    SubscriptionCapabilityEvidenceService,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.capability_evidence import (
    CapabilityEvidenceIdentity,
    CapabilityProofKind,
    ResolvedCapabilityEvidence,
    capability_identity,
)
from forge.domain.subscription_installations import (
    ClaudeInstallationSpec,
    CodexInstallationSpec,
    SubscriptionInstallationManifest,
    is_account_identity_digest,
    load_subscription_installation_manifest,
)
from forge.domain.subscription_readiness import ReadinessReason
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.capability_evidence import PostgresCapabilityEvidenceSource
from forge.persistence.repositories.capability_probe_diagnostics import (
    PostgresCapabilityProbeDiagnosticStore,
)
from forge.settings import Settings

capability_app = typer.Typer(add_completion=False, no_args_is_help=True)
type SupportedClient = Literal["codex_app_server", "claude_code"]
type SupportedInstallation = CodexInstallationSpec | ClaudeInstallationSpec
type SupportedScope = CodexVerificationScope | ClaudeVerificationScope
_SUPPORTED_CLIENTS: tuple[SupportedClient, ...] = ("codex_app_server", "claude_code")
_OFFLINE_MEDIA_TYPE = "application/vnd.forge.client-capability-offline+json"
_SAFE = frozenset(
    (
        "installation_missing",
        "installation_ambiguous",
        "installation_invalid",
        "scope_required",
        "scope_invalid",
        "executable_digest_mismatch",
        "executable_missing",
        "version_mismatch",
        "offline_conformance_failed",
        "subscription_authentication_failed",
        "model_or_effort_unavailable",
        "isolation_configuration_failed",
        "unexpected_tool_callback",
        "probe_timeout",
        "probe_stop_uncertain",
        "capability_publication_failed",
        "account_identity_unbound",
        "account_identity_missing",
        "subscription_signed_out",
        "subscription_type_unrecognized",
        "auth_status_invalid",
        "probe_protocol_failed",
        "offline_verification_failed",
    )
)


class CapabilityPublicationAttemptError(RuntimeError):
    """Sanitized failed publication with conservative contact/retention state."""

    def __init__(
        self,
        public_reason: str,
        *,
        provider_contact: bool,
        diagnostic_recorded: bool,
    ) -> None:
        super().__init__(public_reason)
        self.public_reason = public_reason
        self.provider_contact = provider_contact
        self.diagnostic_recorded = diagnostic_recorded


@dataclass(frozen=True, slots=True)
class CapabilityComposition:
    """Explicit dependencies: tests inject this object, never module state."""

    settings_factory: Callable[[], Settings] = lambda: Settings(process_role="cli")
    probe_factory: Callable[..., CodexCapabilityProbe] = CodexCapabilityProbe
    conformance_factory: Callable[[], CodexOfficialConformanceHarness] = (
        CodexOfficialConformanceHarness
    )
    engine_factory: Callable[[str], AsyncEngine] = create_engine
    session_factory: Callable[[AsyncEngine], async_sessionmaker[AsyncSession]] = (
        create_session_factory
    )
    diagnostic_factory: Callable[
        [async_sessionmaker[AsyncSession]], CapabilityProbeDiagnosticSink
    ] = PostgresCapabilityProbeDiagnosticStore
    claude_probe_factory: Callable[..., ClaudeCapabilityProbe] = ClaudeCapabilityProbe
    claude_conformance_factory: Callable[[], ClaudeOfficialConformanceHarness] = (
        ClaudeOfficialConformanceHarness
    )
    artifact_factory: Callable[[Path], ArtifactStore] = FilesystemArtifactStore
    evidence_source_factory: Callable[
        [async_sessionmaker[AsyncSession], ArtifactStore], PostgresCapabilityEvidenceSource
    ] = PostgresCapabilityEvidenceSource


def _emit(value: dict[str, object]) -> None:
    typer.echo(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _targets(
    manifest: SubscriptionInstallationManifest | None,
    client: SupportedClient = "codex_app_server",
) -> list[tuple[int, SupportedInstallation]]:
    return (
        []
        if manifest is None
        else [
            (n, x)
            for n, x in enumerate(manifest.installations, 1)
            if (client == "codex_app_server" and isinstance(x, CodexInstallationSpec))
            or (client == "claude_code" and isinstance(x, ClaudeInstallationSpec))
        ]
    )


def _select(
    targets: list[tuple[int, SupportedInstallation]],
    ordinal: int | None,
    scope_name: str | None,
    client: SupportedClient = "codex_app_server",
) -> tuple[int, SupportedInstallation, SupportedScope]:
    if not targets:
        raise ValueError("installation_missing")
    if ordinal is None:
        if len(targets) != 1:
            raise ValueError("installation_ambiguous")
        ordinal, item = targets[0]
    else:
        selected_targets = [x for x in targets if x[0] == ordinal]
        if len(selected_targets) != 1:
            raise ValueError("installation_invalid")
        ordinal, item = selected_targets[0]
    available_scopes: tuple[SupportedScope, ...]
    if client == "codex_app_server":
        available_scopes = required_codex_verification_scopes()
    else:
        available_scopes = required_claude_verification_scopes()
    matches = [
        selected
        for selected in available_scopes
        if (selected.model, selected.effort) == (item.model, item.effort)
    ]
    if scope_name is None:
        if len(matches) != 1:
            raise ValueError("scope_required")
        scope = matches[0]
    else:
        named_scopes = [selected for selected in matches if selected.name == scope_name]
        if len(named_scopes) != 1:
            raise ValueError("scope_invalid")
        scope = named_scopes[0]
    return ordinal, item, scope


def _installation(x: SupportedInstallation) -> CodexInstallation | ClaudeInstallation:
    if isinstance(x, ClaudeInstallationSpec):
        return ClaudeInstallation(
            executable=x.executable,
            cwd=x.cwd,
            client_home=x.home,
            model=x.model,
            effort=x.effort,
            account=x.account,
            executable_digest=x.executable_digest,
            client_version=x.client_version,
            quota_limit_types=frozenset(x.quota_limit_types),
        )
    return CodexInstallation(
        executable=x.executable,
        cwd=x.cwd,
        client_home=x.home,
        model=x.model,
        effort=x.effort,
        account=x.account,
        executable_digest=x.executable_digest,
        client_version=x.client_version,
        quota_limit_id=x.quota_limit_id,
        disabled_mcp_servers=x.disabled_mcp_servers,
    )


async def _executable_digest(item: SupportedInstallation) -> str | None:
    """Use the client-specific executable semantics everywhere in this CLI."""
    digest = (
        codex_executable_digest
        if isinstance(item, CodexInstallationSpec)
        else stable_executable_digest
    )
    return await asyncio.to_thread(digest, item.executable)


def _selected_target(
    settings: Settings,
    installation: int | None,
    scope: str | None,
    client: str | None,
) -> tuple[int, SupportedClient, SupportedInstallation, SupportedScope]:
    manifest = load_subscription_installation_manifest(settings.subscription_installations_path)
    if client is None:
        eligible: list[SupportedClient] = [
            name for name in _SUPPORTED_CLIENTS if _targets(manifest, name)
        ]
        if len(eligible) != 1:
            raise ValueError("installation_ambiguous" if eligible else "installation_missing")
        selected_client = eligible[0]
    else:
        if client not in set(_SUPPORTED_CLIENTS):
            raise ValueError("installation_invalid")
        selected_client = client
    ordinal, item, verification_scope = _select(
        _targets(manifest, selected_client), installation, scope, selected_client
    )
    return ordinal, selected_client, item, verification_scope


async def status_data(composition: CapabilityComposition) -> dict[str, object]:
    try:
        settings = composition.settings_factory()
        manifest = load_subscription_installation_manifest(settings.subscription_installations_path)
    except Exception:  # noqa: BLE001 - status is intentionally sanitized
        return {
            "provider_contact": False,
            "publication": "not_attempted",
            "reason": "installation_missing",
            "targets": [],
        }
    engine: AsyncEngine | None = None
    source: CapabilityEvidenceSource | None = None
    diagnostics: CapabilityProbeDiagnosticSource | None = None
    try:
        engine = composition.engine_factory(settings.database_url)
        sessions = composition.session_factory(engine)
        source = composition.evidence_source_factory(
            sessions,
            composition.artifact_factory(settings.artifact_root),
        )
        diagnostics = composition.diagnostic_factory(sessions)
    except Exception:  # noqa: BLE001 - database is optional for offline status
        source = None
    out: list[dict[str, object]] = []
    try:
        for client in ("codex_app_server", "claude_code"):
            for ordinal, item in _targets(manifest, client):
                actual = await _executable_digest(item)
                executable = (
                    "ready_for_probe"
                    if actual and hmac.compare_digest(actual, item.executable_digest)
                    else ("executable_missing" if actual is None else "executable_digest_mismatch")
                )
                scopes = [
                    s
                    for s in (
                        required_codex_verification_scopes()
                        if client == "codex_app_server"
                        else required_claude_verification_scopes()
                    )
                    if (s.model, s.effort) == (item.model, item.effort)
                ]
                evidence: list[dict[str, object]] = []
                for scope in scopes:
                    entry: dict[str, object] = {
                        "scope": scope.name,
                        "status": "status_unavailable",
                    }
                    try:
                        identity = capability_identity(
                            scope=scope.evidence_scope(),
                            client_version=item.client_version,
                            executable_digest=item.executable_digest,
                            client_home=item.home,
                            account=item.account,
                        )
                    except TypeError, ValueError:
                        entry["readiness_reason"] = ReadinessReason.CONFIGURATION_INVALID.value
                        evidence.append(entry)
                        continue
                    if source:
                        try:
                            resolved = await source.resolve(identity)
                            expected_verifier_id = (
                                CODEX_VERIFIER_ID
                                if client == "codex_app_server"
                                else CLAUDE_VERIFIER_ID
                            )
                            expected_verifier_version = (
                                CODEX_VERIFIER_VERSION
                                if client == "codex_app_server"
                                else CLAUDE_VERIFIER_VERSION
                            )
                            if (
                                not isinstance(resolved, ResolvedCapabilityEvidence)
                                or not resolved.matches(identity)
                                or not resolved.permits(scope.evidence_scope())
                                or resolved.manifest.verifier_id != expected_verifier_id
                                or resolved.manifest.verifier_version != expected_verifier_version
                            ):
                                entry = {"scope": scope.name, "status": "stale_or_invalid"}
                            else:
                                entry = {
                                    "scope": scope.name,
                                    "status": "current",
                                    "evidence_id": str(resolved.manifest.evidence_id),
                                    "revision": resolved.revision,
                                    "expires_at": resolved.manifest.expires_at.isoformat(),
                                }
                        except CapabilityEvidenceMissing:
                            entry = {"scope": scope.name, "status": "missing"}
                        except CapabilityEvidenceUnavailable:
                            entry = {"scope": scope.name, "status": "stale_or_invalid"}
                        except Exception:  # noqa: BLE001 - evidence status is sanitized
                            entry = {"scope": scope.name, "status": "stale_or_invalid"}
                    if entry["status"] != "current" and diagnostics is not None:
                        try:
                            diagnostic = await diagnostics.resolve(identity)
                            if diagnostic is not None:
                                entry.update(
                                    {
                                        "readiness_reason": diagnostic.reason.value,
                                        "diagnostic_revision": diagnostic.revision,
                                        "diagnostic_observed_at": diagnostic.observed_at.isoformat(),
                                        "diagnostic_expires_at": diagnostic.expires_at.isoformat(),
                                    }
                                )
                        except Exception:  # noqa: BLE001 - optional status stays sanitized
                            entry["diagnostic_status"] = "unavailable"
                    evidence.append(entry)
                out.append(
                    {
                        "ordinal": ordinal,
                        "client": item.client,
                        "model": item.model,
                        "effort": item.effort,
                        "client_version": item.client_version,
                        "executable": executable,
                        "scopes": [s.name for s in scopes],
                        "evidence": evidence,
                    }
                )
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001 - best-effort disposal after read-only status
                engine = None
    return {
        "provider_contact": False,
        "publication": "not_attempted",
        "reason": "offline_status" if out else "installation_missing",
        "targets": out,
    }


@capability_app.command("status")
def status() -> None:
    _emit(asyncio.run(status_data(CapabilityComposition())))


async def verify_command(
    *,
    installation: int | None,
    scope: str | None,
    composition: CapabilityComposition,
    client: str | None = None,
) -> dict[str, object]:
    """Run providerless official-client conformance and retain its safe artifact."""

    settings = composition.settings_factory()
    ordinal, selected_client, item, verification_scope = _selected_target(
        settings, installation, scope, client
    )
    installed = _installation(item)
    actual = await _executable_digest(item)
    if actual is None or not hmac.compare_digest(actual, installed.executable_digest):
        raise ValueError("executable_missing" if actual is None else "executable_digest_mismatch")

    evidence_scope = verification_scope.evidence_scope()
    result: CodexConformanceResult | ClaudeConformanceResult
    try:
        if isinstance(installed, CodexInstallation) and isinstance(
            verification_scope, CodexVerificationScope
        ):
            codex_observed = await composition.conformance_factory().run(
                installed, codex_offline_scope(evidence_scope)
            )
            result = validate_codex_offline_conformance(codex_observed, installed, evidence_scope)
        elif isinstance(installed, ClaudeInstallation) and isinstance(
            verification_scope, ClaudeVerificationScope
        ):
            claude_observed = await composition.claude_conformance_factory().run(
                installed, claude_offline_scope(evidence_scope)
            )
            result = validate_claude_offline_conformance(claude_observed, installed, evidence_scope)
        else:  # pragma: no cover - selected by the closed discriminator above
            raise TypeError("installation_invalid")
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - expose only the closed public reason
        raise ValueError(_public_reason(error)) from None

    if result.publishable or result.live_provider_call:
        raise ValueError("offline_verification_failed")
    wire = json.dumps(
        result.sanitized_payload(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    artifacts = composition.artifact_factory(settings.artifact_root)
    try:
        descriptor = await artifacts.put_bytes(
            wire,
            media_type=_OFFLINE_MEDIA_TYPE,
            max_bytes=64 * 1024,
            bounding_policy="head_tail",
        )
        descriptor_valid = (
            descriptor.digest == hashlib.sha256(wire).hexdigest()
            and descriptor.media_type == _OFFLINE_MEDIA_TYPE
            and descriptor.byte_count == len(wire)
            and not descriptor.truncated
            and await artifacts.verify(descriptor.digest)
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - artifact errors are intentionally sanitized
        raise ValueError("offline_verification_failed") from None
    if not descriptor_valid:
        raise ValueError("offline_verification_failed")

    return {
        "provider_contact": False,
        "publication": "not_attempted",
        "publishable": False,
        "offline_conformance": "passed",
        "manifest_entry": ordinal,
        "client": selected_client,
        "client_version": result.client_version,
        "executable_digest": result.executable_digest,
        "scope": result.scope.name,
        "offline_artifact_digest": descriptor.digest,
        "unmet_proof_kinds": [
            "account_authentication",
            "route_identity",
            CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING.value,
        ],
    }


@capability_app.command("verify")
def verify(
    installation: int | None = typer.Option(None, "--installation"),
    scope: str | None = typer.Option(None, "--scope"),
    client: str | None = typer.Option(None, "--client"),
) -> None:
    """Run bounded offline conformance; never contact a provider or publish authority."""

    try:
        value = asyncio.run(
            verify_command(
                installation=installation,
                scope=scope,
                client=client,
                composition=CapabilityComposition(),
            )
        )
    except Exception as error:  # noqa: BLE001 - never expose process or filesystem details
        _emit(
            {
                "provider_contact": False,
                "publication": "not_attempted",
                "offline_conformance": "failed",
                "reason": _public_reason(error),
            }
        )
        raise typer.Exit(code=1) from None
    _emit(value)


async def publish_command(
    *,
    installation: int | None,
    scope: str | None,
    composition: CapabilityComposition,
    client: str | None = None,
) -> ResolvedCapabilityEvidence:
    settings = composition.settings_factory()
    _, selected_client, item, verification_scope = _selected_target(
        settings, installation, scope, client
    )
    installed = _installation(item)
    identity = capability_identity(
        scope=verification_scope.evidence_scope(),
        client_version=installed.client_version,
        executable_digest=installed.executable_digest,
        client_home=installed.client_home,
        account=installed.account,
    )
    engine = composition.engine_factory(settings.database_url)
    try:
        sessions = composition.session_factory(engine)
        diagnostics = composition.diagnostic_factory(sessions)
        artifacts = composition.artifact_factory(settings.artifact_root)
        source = composition.evidence_source_factory(sessions, artifacts)
        actual = await asyncio.to_thread(
            codex_executable_digest
            if selected_client == "codex_app_server"
            else stable_executable_digest,
            installed.executable,
        )
        if actual is None or not hmac.compare_digest(actual, installed.executable_digest):
            public_reason = "executable_missing" if actual is None else "executable_digest_mismatch"
            recorded = await _record_diagnostic(
                diagnostics, identity, _readiness_reason(public_reason)
            )
            raise CapabilityPublicationAttemptError(
                public_reason,
                provider_contact=False,
                diagnostic_recorded=recorded,
            )
        try:
            if selected_client == "codex_app_server":
                observations = await composition.probe_factory(
                    installed,
                    verification_scope.evidence_scope(),
                    composition.conformance_factory(),
                ).observe(authorize_provider_contact=True)
            else:
                if not is_account_identity_digest(installed.account):
                    raise ValueError("account_identity_unbound")
                observations = await composition.claude_probe_factory(
                    installed,
                    verification_scope.evidence_scope(),
                    composition.claude_conformance_factory(),
                    SupervisedClaudeAuthStatusRunner(),
                    SupervisedClaudeLiveRouteRunner(),
                ).observe(authorize_provider_contact=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - map only to closed public diagnostics
            public_reason = _public_reason(error)
            recorded = await _record_diagnostic(
                diagnostics, identity, _readiness_reason(public_reason)
            )
            raise CapabilityPublicationAttemptError(
                public_reason,
                provider_contact=True,
                diagnostic_recorded=recorded,
            ) from None
        try:
            resolved = await SubscriptionCapabilityEvidenceService(artifacts, source).publish(
                identity=identity,
                observations=observations,
                observed_at=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - provider output is never exposed
            public_reason = _public_reason(error)
            recorded = await _record_diagnostic(
                diagnostics,
                identity,
                ReadinessReason.EVIDENCE_STALE_OR_INVALID,
            )
            raise CapabilityPublicationAttemptError(
                public_reason,
                provider_contact=True,
                diagnostic_recorded=recorded,
            ) from None
        await _record_diagnostic(
            diagnostics,
            identity,
            ReadinessReason.READY,
            expires_at=resolved.manifest.expires_at,
        )
        return resolved
    finally:
        await engine.dispose()


def _public_reason(error: Exception) -> str:
    value = str(error)
    return value if value in _SAFE else "capability_publication_failed"


def _readiness_reason(public_reason: str) -> ReadinessReason:
    if public_reason == "executable_missing":
        return ReadinessReason.MISSING_EXECUTABLE
    if public_reason == "executable_digest_mismatch":
        return ReadinessReason.EXECUTABLE_DIGEST_MISMATCH
    if public_reason == "version_mismatch":
        return ReadinessReason.VERSION_MISMATCH
    if public_reason in {"subscription_signed_out", "subscription_authentication_failed"}:
        return ReadinessReason.SIGNED_OUT
    if public_reason == "account_identity_missing":
        return ReadinessReason.ACCOUNT_AUTHENTICATION_UNPROVED
    if public_reason in {"account_identity_unbound", "subscription_type_unrecognized"}:
        return ReadinessReason.SUBSCRIPTION_ROUTE_UNBOUND
    if public_reason == "model_or_effort_unavailable":
        return ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT
    if public_reason in {
        "offline_conformance_failed",
        "isolation_configuration_failed",
        "unexpected_tool_callback",
    }:
        return ReadinessReason.ISOLATION_UNPROVED
    return ReadinessReason.UNKNOWN


async def _record_diagnostic(
    diagnostics: CapabilityProbeDiagnosticSink,
    identity: CapabilityEvidenceIdentity,
    reason: ReadinessReason,
    *,
    expires_at: datetime | None = None,
) -> bool:
    observed_at = datetime.now(UTC)
    try:
        await diagnostics.report(
            identity,
            reason,
            observed_at=observed_at,
            expires_at=expires_at or observed_at + timedelta(hours=1),
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - diagnostics cannot change publication authority
        return False


@capability_app.command("publish")
def publish(
    authorize_provider_contact: bool = typer.Option(False, "--authorize-provider-contact"),
    installation: int | None = typer.Option(None, "--installation"),
    scope: str | None = typer.Option(None, "--scope"),
    client: str | None = typer.Option(None, "--client"),
) -> None:
    if not authorize_provider_contact:
        _emit(
            {
                "provider_contact": False,
                "publication": "not_attempted",
                "reason": "provider_observation_required",
                "hint": "authorize a subscription/ChatGPT probe; it has no API-credit or paid fallback route",
            }
        )
        raise typer.Exit(code=2)
    try:
        selected_client = client
        if selected_client is not None and selected_client not in {
            "codex_app_server",
            "claude_code",
        }:
            raise ValueError("installation_invalid")
        resolved = asyncio.run(
            publish_command(
                installation=installation,
                scope=scope,
                client=selected_client,
                composition=CapabilityComposition(),
            )
        )
    except CapabilityPublicationAttemptError as error:
        _emit(
            {
                "provider_contact": error.provider_contact,
                "publication": "failed",
                "reason": error.public_reason,
                "diagnostic_recorded": error.diagnostic_recorded,
            }
        )
        raise typer.Exit(code=1) from None
    except Exception as error:  # noqa: BLE001 - never expose raw provider/process errors
        _emit(
            {
                "provider_contact": False,
                "publication": "failed",
                "reason": _public_reason(error),
                "diagnostic_recorded": False,
            }
        )
        raise typer.Exit(code=1) from None
    identity = resolved.manifest.identity
    _emit(
        {
            "provider": identity.provider,
            "client": identity.client,
            "model": identity.model,
            "effort": identity.effort.value,
            "scope": identity.role.value,
            "evidence_id": str(resolved.manifest.evidence_id),
            "revision": resolved.revision,
            "expires_at": resolved.manifest.expires_at.isoformat(),
            "publication": "published",
        }
    )


__all__ = [
    "CapabilityComposition",
    "CapabilityPublicationAttemptError",
    "capability_app",
    "publish_command",
    "status_data",
    "verify_command",
]
