import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
    required_claude_verification_scopes,
)
from forge.agents.codex_capability_probe import CodexCapabilityProbeError
from forge.agents.codex_conformance import CodexConformanceResult
from forge.agents.codex_verification import (
    CODEX_VERIFIER_ID,
    CODEX_VERIFIER_VERSION,
    required_codex_verification_scopes,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.cli.subscription_capabilities import (
    CapabilityComposition,
    CapabilityPublicationAttemptError,
    _readiness_reason,
    capability_app,
    publish_command,
    status_data,
    verify_command,
)
from forge.domain.capability_evidence import capability_home_digest
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.subscription_readiness import ReadinessReason
from forge.settings import Settings
from typer.testing import CliRunner

from scripts.dev import DefaultCommandRunner


def test_status_never_contacts_provider_and_publish_requires_gate():
    runner = CliRunner()
    status = runner.invoke(capability_app, ["status"])
    assert status.exit_code == 0 and '"provider_contact":false' in status.output
    publish = runner.invoke(capability_app, ["publish"])
    assert publish.exit_code == 2 and "provider_observation_required" in publish.output


def test_root_help_registers_status_and_publish():
    result = CliRunner().invoke(capability_app, ["--help"])
    assert result.exit_code == 0
    assert all(command in result.output for command in ("status", "verify", "publish"))


@pytest.mark.asyncio
async def test_status_sanitizes_missing_home_and_keeps_other_targets(tmp_path: Path) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    missing_claude_home = tmp_path / "missing-claude-home"
    manifest = tmp_path / "installations.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "installations": [
                    {
                        "client": "codex_app_server",
                        "executable": str(executable),
                        "cwd": str(tmp_path),
                        "home": str(codex_home),
                        "model": "gpt-6-astra",
                        "effort": "low",
                        "account": "a" * 64,
                        "executable_digest": digest,
                        "client_version": "0.154.0",
                        "quota": {"account": "personal", "pool": "weekly"},
                    },
                    {
                        "client": "claude_code",
                        "executable": str(executable),
                        "cwd": str(tmp_path),
                        "home": str(missing_claude_home),
                        "model": "claude-opus-5",
                        "effort": "medium",
                        "account": "b" * 64,
                        "executable_digest": digest,
                        "client_version": "2.1.263",
                        "quota": {"account": "review", "pool": "seven-day"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    class Engine:
        disposed = False

        async def dispose(self):
            self.disposed = True

    engine = Engine()
    artifacts = SimpleNamespace(
        put_bytes=lambda *_args, **_kwargs: None,
        open_bytes=lambda *_args, **_kwargs: None,
        verify=lambda *_args, **_kwargs: None,
    )
    value = await status_data(
        CapabilityComposition(
            settings_factory=lambda: Settings(
                _env_file=None,
                process_role="cli",
                subscription_installations_path=manifest,
            ),
            engine_factory=lambda _url: engine,
            session_factory=lambda _engine: lambda: None,
            artifact_factory=lambda _root: artifacts,
            diagnostic_factory=lambda _sessions: None,
        )
    )

    targets = {target["client"]: target for target in value["targets"]}
    assert set(targets) == {"codex_app_server", "claude_code"}
    assert targets["claude_code"]["evidence"] == [
        {
            "scope": "opus-independent-review",
            "status": "status_unavailable",
            "readiness_reason": "configuration_invalid",
        }
    ]
    assert targets["codex_app_server"]["evidence"][0]["status"] == "stale_or_invalid"
    assert str(missing_claude_home) not in repr(value)
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_offline_verify_retains_sanitized_nonpublishable_artifact(
    tmp_path: Path,
) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    account = "a" * 64
    home = tmp_path / "codex-home"
    home.mkdir()
    manifest = tmp_path / "installations.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "installations": [
                    {
                        "client": "codex_app_server",
                        "executable": str(executable),
                        "cwd": str(tmp_path),
                        "home": str(home),
                        "model": "gpt-6-astra",
                        "effort": "low",
                        "account": account,
                        "executable_digest": digest,
                        "client_version": "0.154.0",
                        "quota": {"account": "personal", "pool": "weekly"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scope = required_codex_verification_scopes()[0]
    result = CodexConformanceResult(
        scope,
        "0.154.0",
        digest,
        capability_home_digest(str(home)),
        account,
        True,
        True,
        True,
        ("all",),
        True,
        True,
        True,
        False,
        SubscriptionLaunchTerminalProof(
            launch_id="offline-proof",
            pid=123,
            process_identity="process",
            outcome="completed",
            return_code=0,
            stop_confirmed=True,
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    class Conformance:
        async def run(self, *_args):
            return result

    class Artifacts:
        def __init__(self):
            self.data = b""

        async def put_bytes(self, data, *, media_type, **_kwargs):
            self.data = data
            stored_digest = hashlib.sha256(data).hexdigest()
            return SimpleNamespace(
                digest=stored_digest,
                media_type=media_type,
                byte_count=len(data),
                truncated=False,
            )

        async def verify(self, stored_digest):
            return stored_digest == hashlib.sha256(self.data).hexdigest()

    artifacts = Artifacts()
    composition = CapabilityComposition(
        settings_factory=lambda: Settings(
            _env_file=None,
            process_role="cli",
            subscription_installations_path=manifest,
            artifact_root=tmp_path / "artifacts",
        ),
        conformance_factory=lambda: Conformance(),
        artifact_factory=lambda _root: artifacts,
    )

    value = await verify_command(
        installation=1,
        scope="astra-primary",
        client="codex_app_server",
        composition=composition,
    )

    assert value["provider_contact"] is False
    assert value["publication"] == "not_attempted" and value["publishable"] is False
    assert value["manifest_entry"] == 1
    assert value["client_version"] == "0.154.0" and value["executable_digest"] == digest
    assert value["unmet_proof_kinds"] == [
        "account_authentication",
        "route_identity",
        "billing_enforcement",
    ]
    assert str(home) not in repr(value) and account not in repr(value)
    retained = json.loads(artifacts.data)
    assert retained["publication"]["eligible"] is False
    assert retained["publication"]["live_provider_call"] is False


@pytest.mark.official_client
@pytest.mark.asyncio
async def test_offline_verify_command_runs_installed_codex_without_provider_contact(
    tmp_path: Path,
) -> None:
    shim = shutil.which("codex")
    if shim is None:
        pytest.skip("official Codex is not installed")
    executable = (
        Path(shim).parent
        / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64"
        / "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    )
    if not executable.is_file():
        pytest.skip("installed Codex native executable was not found")
    observed_version = DefaultCommandRunner().run([str(executable), "--version"], timeout=5)
    if observed_version.returncode != 0:
        pytest.skip("installed Codex version could not be read")
    client_version = observed_version.stdout.strip().removeprefix("codex-cli ")
    if client_version.count(".") != 2:
        pytest.skip("installed Codex did not report an exact release")

    cwd, home = tmp_path / "repository", tmp_path / "codex-home"
    data_root = tmp_path / "data"
    cwd.mkdir()
    home.mkdir()
    manifest = tmp_path / "installations.json"
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "installations": [
                    {
                        "client": "codex_app_server",
                        "executable": str(executable),
                        "cwd": str(cwd),
                        "home": str(home),
                        "model": "gpt-6-astra",
                        "effort": "low",
                        "account": "a" * 64,
                        "executable_digest": digest,
                        "client_version": client_version,
                        "quota": {"account": "personal", "pool": "weekly"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    value = await verify_command(
        installation=1,
        scope="astra-primary",
        client="codex_app_server",
        composition=CapabilityComposition(
            settings_factory=lambda: Settings(
                _env_file=None,
                process_role="cli",
                subscription_installations_path=manifest,
                data_root=data_root,
            )
        ),
    )

    assert value["provider_contact"] is False
    assert value["publication"] == "not_attempted" and value["publishable"] is False
    assert value["client_version"] == client_version
    assert value["executable_digest"] == digest
    assert value["manifest_entry"] == 1
    assert value["unmet_proof_kinds"] == [
        "account_authentication",
        "route_identity",
        "billing_enforcement",
    ]
    artifact_digest = value["offline_artifact_digest"]
    assert isinstance(artifact_digest, str)
    retained = json.loads(
        await FilesystemArtifactStore(data_root / "artifacts").open_bytes(artifact_digest)
    )
    assert retained["installation"]["client_version"] == client_version
    assert retained["installation"]["executable_digest"] == digest
    assert retained["publication"]["eligible"] is False
    assert retained["publication"]["live_provider_call"] is False


@pytest.mark.parametrize(
    ("public_reason", "expected"),
    [
        ("executable_digest_mismatch", ReadinessReason.EXECUTABLE_DIGEST_MISMATCH),
        ("version_mismatch", ReadinessReason.VERSION_MISMATCH),
        ("subscription_signed_out", ReadinessReason.SIGNED_OUT),
        ("subscription_authentication_failed", ReadinessReason.SIGNED_OUT),
        ("account_identity_missing", ReadinessReason.ACCOUNT_AUTHENTICATION_UNPROVED),
        ("account_identity_unbound", ReadinessReason.SUBSCRIPTION_ROUTE_UNBOUND),
        ("subscription_type_unrecognized", ReadinessReason.SUBSCRIPTION_ROUTE_UNBOUND),
        ("model_or_effort_unavailable", ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT),
        ("isolation_configuration_failed", ReadinessReason.ISOLATION_UNPROVED),
        ("unexpected_tool_callback", ReadinessReason.ISOLATION_UNPROVED),
        ("offline_conformance_failed", ReadinessReason.ISOLATION_UNPROVED),
        ("probe_timeout", ReadinessReason.UNKNOWN),
        ("unrecognized-secret-provider-error", ReadinessReason.UNKNOWN),
    ],
)
def test_probe_failures_map_to_closed_readiness_reasons(public_reason, expected):
    assert _readiness_reason(public_reason) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "model", "effort", "scope_name"),
    [
        ("codex_app_server", "gpt-6-astra", "low", "astra-primary"),
        ("claude_code", "claude-opus-5", "medium", "opus-independent-review"),
    ],
)
async def test_authorized_probe_failure_records_safe_identity_bound_diagnostic(
    tmp_path: Path,
    client: str,
    model: str,
    effort: str,
    scope_name: str,
) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    home = tmp_path / "codex-home"
    home.mkdir()
    manifest = tmp_path / "installations.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "installations": [
                    {
                        "client": client,
                        "executable": str(executable),
                        "cwd": str(tmp_path),
                        "home": str(home),
                        "model": model,
                        "effort": effort,
                        "account": "a" * 64,
                        "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
                        "client_version": "0.154.0",
                        "quota": {"account": "personal", "pool": "weekly"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reports = []

    class Diagnostics:
        async def report(self, identity, reason, **times):
            reports.append((identity, reason, times))

    class Probe:
        async def observe(self, **_kwargs):
            raise CodexCapabilityProbeError("subscription_authentication_failed")

    class Engine:
        async def dispose(self):
            return None

    probe_args = []
    conformance = object()

    def probe_factory(*args):
        probe_args.append(args)
        return Probe()

    composition = CapabilityComposition(
        settings_factory=lambda: Settings(
            _env_file=None,
            process_role="cli",
            subscription_installations_path=manifest,
            artifact_root=tmp_path / "artifacts",
        ),
        probe_factory=probe_factory,
        claude_probe_factory=probe_factory,
        conformance_factory=lambda: conformance,
        claude_conformance_factory=lambda: conformance,
        engine_factory=lambda _url: Engine(),
        session_factory=lambda _engine: lambda: None,
        diagnostic_factory=lambda _sessions: Diagnostics(),
    )

    with pytest.raises(CapabilityPublicationAttemptError) as caught:
        await publish_command(
            installation=1,
            scope=scope_name,
            client=client,
            composition=composition,
        )

    assert caught.value.public_reason == "subscription_authentication_failed"
    assert caught.value.provider_contact is True
    assert probe_args[0][2] is conformance
    assert len(reports) == 1
    identity, reason, times = reports[0]
    assert reason is ReadinessReason.SIGNED_OUT
    assert identity.account == "a" * 64
    assert times["expires_at"] > times["observed_at"]


async def _run_verifier_status_test(
    tmp_path: Path,
    client: str,
    model: str,
    effort: str,
    scope_name: str,
    verifier_id: str,
    verifier_version: str,
) -> tuple[dict[str, Any], Any]:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from forge.domain.capability_evidence import (
        CapabilityEvidenceManifest,
        CapabilityProof,
        CapabilityProofKind,
        ResolvedCapabilityEvidence,
        capability_identity,
        encode_capability_evidence,
    )

    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    account = "a" * 64
    home = tmp_path / f"{client}-home"
    home.mkdir(exist_ok=True)
    manifest_file = tmp_path / f"{client}-installations.json"
    installation_data = {
        "client": client,
        "executable": str(executable),
        "cwd": str(tmp_path),
        "home": str(home),
        "model": model,
        "effort": effort,
        "account": account,
        "executable_digest": digest,
        "client_version": "0.154.0" if client == "codex_app_server" else "2.1.263",
        "quota": {"account": "personal", "pool": "weekly"},
    }
    if client == "claude_code":
        installation_data["quota_limit_types"] = ["seven_day"]
    manifest_file.write_text(
        json.dumps({"version": 2, "installations": [installation_data]}),
        encoding="utf-8",
    )

    scopes = (
        required_codex_verification_scopes()
        if client == "codex_app_server"
        else required_claude_verification_scopes()
    )
    scope = next(s for s in scopes if s.name == scope_name)
    identity = capability_identity(
        scope=scope.evidence_scope(),
        client_version=installation_data["client_version"],
        executable_digest=digest,
        client_home=str(home),
        account=account,
    )

    evidence_manifest = CapabilityEvidenceManifest(
        evidence_id=uuid4(),
        identity=identity,
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        observed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        proofs=tuple(
            CapabilityProof(kind=kind, artifact_digest="a" * 64) for kind in CapabilityProofKind
        ),
    )
    wire = encode_capability_evidence(evidence_manifest)
    resolved = ResolvedCapabilityEvidence(
        manifest=evidence_manifest,
        artifact_digest=hashlib.sha256(wire).hexdigest(),
        revision=1,
    )

    class FakeSource:
        async def resolve(self, _id):
            return resolved

    class Engine:
        async def dispose(self):
            return None

    composition = CapabilityComposition(
        settings_factory=lambda: Settings(
            _env_file=None,
            process_role="cli",
            subscription_installations_path=manifest_file,
        ),
        engine_factory=lambda _url: Engine(),
        session_factory=lambda _engine: lambda: None,
        evidence_source_factory=lambda _sessions, _artifacts: FakeSource(),
        diagnostic_factory=lambda _sessions: None,
    )

    result = await status_data(composition)
    return result["targets"][0]["evidence"][0], evidence_manifest


@pytest.mark.parametrize(
    (
        "client",
        "model",
        "effort",
        "scope_name",
        "obsolete_verifier_id",
        "obsolete_verifier_version",
    ),
    [
        (
            "codex_app_server",
            "gpt-6-astra",
            "low",
            "astra-primary",
            "forge-codex-official",
            "1-obsolete-policy-catalog",
        ),
        (
            "claude_code",
            "claude-opus-5",
            "medium",
            "opus-independent-review",
            "forge-claude-official",
            "1-obsolete-policy-catalog",
        ),
    ],
)
async def test_status_reports_stale_or_invalid_for_obsolete_verifier_evidence(
    tmp_path: Path,
    client: str,
    model: str,
    effort: str,
    scope_name: str,
    obsolete_verifier_id: str,
    obsolete_verifier_version: str,
) -> None:
    entry, _ = await _run_verifier_status_test(
        tmp_path,
        client,
        model,
        effort,
        scope_name,
        obsolete_verifier_id,
        obsolete_verifier_version,
    )
    assert entry["status"] == "stale_or_invalid"


@pytest.mark.parametrize(
    (
        "client",
        "model",
        "effort",
        "scope_name",
        "verifier_id",
        "verifier_version",
    ),
    [
        (
            "claude_code",
            "claude-opus-5",
            "medium",
            "opus-independent-review",
            "forge-untrusted-verifier",
            CLAUDE_VERIFIER_VERSION,
        ),
        (
            "claude_code",
            "claude-opus-5",
            "medium",
            "opus-independent-review",
            CODEX_VERIFIER_ID,
            CODEX_VERIFIER_VERSION,
        ),
        (
            "codex_app_server",
            "gpt-6-astra",
            "low",
            "astra-primary",
            CLAUDE_VERIFIER_ID,
            CLAUDE_VERIFIER_VERSION,
        ),
    ],
)
async def test_status_reports_stale_or_invalid_for_wrong_or_cross_client_verifier_evidence(
    tmp_path: Path,
    client: str,
    model: str,
    effort: str,
    scope_name: str,
    verifier_id: str,
    verifier_version: str,
) -> None:
    entry, _ = await _run_verifier_status_test(
        tmp_path,
        client,
        model,
        effort,
        scope_name,
        verifier_id,
        verifier_version,
    )
    assert entry["status"] == "stale_or_invalid"


@pytest.mark.parametrize(
    (
        "client",
        "model",
        "effort",
        "scope_name",
        "verifier_id",
        "verifier_version",
    ),
    [
        (
            "codex_app_server",
            "gpt-6-astra",
            "low",
            "astra-primary",
            CODEX_VERIFIER_ID,
            CODEX_VERIFIER_VERSION,
        ),
        (
            "claude_code",
            "claude-opus-5",
            "medium",
            "opus-independent-review",
            CLAUDE_VERIFIER_ID,
            CLAUDE_VERIFIER_VERSION,
        ),
    ],
)
async def test_status_reports_current_for_valid_current_verifier_evidence(
    tmp_path: Path,
    client: str,
    model: str,
    effort: str,
    scope_name: str,
    verifier_id: str,
    verifier_version: str,
) -> None:
    entry, manifest = await _run_verifier_status_test(
        tmp_path,
        client,
        model,
        effort,
        scope_name,
        verifier_id,
        verifier_version,
    )
    assert entry["status"] == "current"
    assert entry["evidence_id"] == str(manifest.evidence_id)
    assert entry["revision"] == 1
    assert entry["expires_at"] == manifest.expires_at.isoformat()
