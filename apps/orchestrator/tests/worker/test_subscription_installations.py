import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge.agents.claude_runtime import ClaudeRuntimeAdapter
from forge.agents.claude_verification import ClaudeEvidenceVerifier
from forge.agents.codex_runtime import CodexRuntimeAdapter
from forge.agents.codex_verification import CodexEvidenceVerifier
from forge.agents.gemini_runtime import GeminiRuntimeAdapter
from forge.domain.local_cli import LocalCliTrust
from forge.domain.subscription_quota import QuotaPolicy, QuotaPoolKey, QuotaRoutePool
from forge.domain.subscription_readiness import ReadinessReason, ReadinessWarning
from forge.settings import Settings
from forge.worker import subscription_installations as installations
from forge.worker.subscription_installations import (
    SubscriptionVerifierDependencies,
    load_subscription_installations,
    load_subscription_installations_diagnostic,
    production_subscription_verifiers,
)


def test_closed_manifest_loads_pinned_adapters_and_opaque_quota_mappings(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "client-cwd"
    cwd.mkdir()
    homes = {name: tmp_path / f"{name}-home" for name in ("codex", "claude", "gemini")}
    for home in homes.values():
        home.mkdir()
    executable = str(Path(sys.executable).resolve(strict=True))
    executable_digest = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
    account = hashlib.sha256(b"opaque-provider-account").hexdigest()
    manifest = tmp_path / "subscription-installations.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "installations": [
                    {
                        "client": "codex_app_server",
                        "executable": executable,
                        "cwd": str(cwd),
                        "home": str(homes["codex"]),
                        "model": "gpt-6-astra",
                        "effort": "low",
                        "account": account,
                        "executable_digest": executable_digest,
                        "client_version": "0.153.4",
                        "quota": {"account": "personal", "pool": "weekly"},
                        "quota_limit_id": "codex",
                    },
                    {
                        "client": "claude_code",
                        "executable": executable,
                        "cwd": str(cwd),
                        "home": str(homes["claude"]),
                        "model": "claude-opus-5",
                        "effort": "medium",
                        "account": account,
                        "executable_digest": executable_digest,
                        "client_version": "2.1.263",
                        "quota": {"account": "review", "pool": "seven-day"},
                        "quota_limit_types": ["seven_day"],
                    },
                    {
                        "client": "gemini_cli",
                        "executable": executable,
                        "cwd": str(cwd),
                        "home": str(homes["gemini"]),
                        "model": "gemini-3.1-pro-preview",
                        "effort": "medium",
                        "account": account,
                        "executable_digest": executable_digest,
                        "client_version": "0.60.0",
                        "quota": {"account": "google", "pool": "allowance"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        subscription_installations_path=manifest,
        subscription_client_trust=LocalCliTrust.VERIFIED,
        _env_file=None,
    )
    verifier = SimpleNamespace(verify=lambda *_args: None)

    adapters = load_subscription_installations(
        settings,
        SubscriptionVerifierDependencies(
            codex=verifier,
            claude=verifier,
            gemini=verifier,
        ),
    )

    assert [type(adapter) for adapter in adapters] == [
        CodexRuntimeAdapter,
        ClaudeRuntimeAdapter,
        GeminiRuntimeAdapter,
    ]
    assert adapters[0].installation.script == ("app-server", "--stdio")
    assert adapters[1].installation.script == (
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    )
    assert adapters[2].installation.script == ("--acp",)
    assert all(not adapter.installation.environment for adapter in adapters)
    assert all(list(home.iterdir()) == [] for home in homes.values())
    assert settings.subscription_quota_policy.key_for(adapters[0].route) == QuotaPoolKey(
        "openai", "personal", "weekly"
    )
    assert settings.subscription_quota_policy.key_for(adapters[1].route) == QuotaPoolKey(
        "anthropic", "review", "seven-day"
    )
    assert settings.subscription_quota_policy.key_for(adapters[2].route) == QuotaPoolKey(
        "google", "google", "allowance"
    )
    diagnostic = load_subscription_installations_diagnostic(
        settings,
        SubscriptionVerifierDependencies(codex=verifier, claude=verifier, gemini=verifier),
    )
    assert diagnostic.readiness[2].warnings == (ReadinessWarning.APPROVED_TOOLS_UNPROVED,)
    assert diagnostic.readiness[2].wire()["warnings"] == ["approved_tools_unproved"]


def test_diagnostic_loader_reads_once_and_reuses_missing_digest(
    tmp_path: Path, monkeypatch
) -> None:
    path, _payload = _codex_manifest(tmp_path)
    settings = Settings(subscription_installations_path=path, _env_file=None)
    original_load = installations.load_subscription_installation_manifest

    loads = digests = 0

    def load_once(value):
        nonlocal loads
        loads += 1
        return original_load(value)

    def missing_once(value):
        nonlocal digests
        digests += 1

    monkeypatch.setattr(installations, "load_subscription_installation_manifest", load_once)
    monkeypatch.setattr(installations, "stable_executable_digest", missing_once)
    result = load_subscription_installations_diagnostic(
        settings,
        SubscriptionVerifierDependencies(codex=SimpleNamespace(verify=lambda *_args: None)),
    )
    assert loads == 1 and digests == len(result.specs) == 1
    assert result.readiness[0].reason is ReadinessReason.MISSING_EXECUTABLE


@pytest.mark.parametrize(
    "untrusted_field,value",
    [
        ("capability_verified", True),
        ("script", ["arbitrary", "command"]),
        ("environment", {"TOKEN": "secret"}),
        ("python_import", "package.factory"),
    ],
)
def test_manifest_cannot_supply_authority_or_execution_mechanisms(
    tmp_path: Path, untrusted_field: str, value: object
) -> None:
    path, payload = _codex_manifest(tmp_path)
    payload["installations"][0][untrusted_field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    settings = Settings(
        data_root=tmp_path / "data",
        subscription_installations_path=path,
        _env_file=None,
    )

    adapters = load_subscription_installations(
        settings,
        SubscriptionVerifierDependencies(codex=SimpleNamespace(verify=lambda *_args: None)),
    )

    assert adapters == ()
    assert settings.subscription_quota_policy.route_pools == ()
    assert not settings.data_root.exists()


@pytest.mark.parametrize("failure", ["missing", "digest", "verifier"])
def test_missing_or_unverified_installation_is_healthy_and_unavailable(
    tmp_path: Path, failure: str
) -> None:
    path, payload = _codex_manifest(tmp_path)
    if failure == "missing":
        payload["installations"][0]["executable"] = str(tmp_path / "missing.exe")
    elif failure == "digest":
        payload["installations"][0]["executable_digest"] = "f" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    settings = Settings(subscription_installations_path=path, _env_file=None)
    verifier = None if failure == "verifier" else SimpleNamespace(verify=lambda *_args: None)
    settings.subscription_client_trust = LocalCliTrust.VERIFIED

    assert (
        load_subscription_installations(settings, SubscriptionVerifierDependencies(codex=verifier))
        == ()
    )

    diagnostic = load_subscription_installations_diagnostic(
        settings, SubscriptionVerifierDependencies(codex=verifier)
    )
    assert len(diagnostic.readiness) == 1
    assert (
        diagnostic.readiness[0].reason
        is {
            "missing": ReadinessReason.MISSING_EXECUTABLE,
            "digest": ReadinessReason.EXECUTABLE_DIGEST_MISMATCH,
            "verifier": ReadinessReason.PROVIDER_UNSUPPORTED,
        }[failure]
    )
    assert diagnostic.readiness[0].admitted is False


def test_absent_and_malformed_manifests_leave_settings_healthy(tmp_path: Path) -> None:
    missing = Settings(subscription_installations_path=tmp_path / "missing.json", _env_file=None)
    assert load_subscription_installations(missing, SubscriptionVerifierDependencies()) == ()

    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text('{"version":2,"version":2,"installations":[]}', encoding="utf-8")
    malformed = Settings(subscription_installations_path=malformed_path, _env_file=None)
    assert malformed.subscription_quota_policy.route_pools == ()
    assert load_subscription_installations(malformed, SubscriptionVerifierDependencies()) == ()


def test_manifest_rejects_unknown_reasoning_effort_before_readiness(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In-process Alembic checks disable existing loggers; a standalone worker
    # starts with this logger enabled.
    monkeypatch.setattr(installations.logger, "disabled", False)
    path, payload = _codex_manifest(tmp_path)
    payload["installations"][0]["effort"] = "turbo"
    path.write_text(json.dumps(payload), encoding="utf-8")
    settings = Settings(subscription_installations_path=path, _env_file=None)

    assert settings.subscription_quota_policy.route_pools == ()
    diagnostic = load_subscription_installations_diagnostic(
        settings,
        SubscriptionVerifierDependencies(codex=SimpleNamespace(verify=lambda *_args: None)),
    )
    assert diagnostic.adapters == ()
    assert diagnostic.readiness == ()
    assert "Subscription installation manifest is unavailable" in caplog.text
    assert str(path) not in caplog.text
    assert "turbo" not in caplog.text


@pytest.mark.parametrize("field", ["executable", "cwd", "home"])
def test_manifest_rejects_relative_installation_paths_before_quota_merge(
    tmp_path: Path, field: str
) -> None:
    path, payload = _codex_manifest(tmp_path)
    payload["installations"][0][field] = "relative/path"
    path.write_text(json.dumps(payload), encoding="utf-8")

    settings = Settings(subscription_installations_path=path, _env_file=None)

    assert settings.subscription_quota_policy.route_pools == ()


def test_oversized_manifest_and_conflicting_quota_mapping_fail_closed(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (64 * 1024 + 1))
    oversized_settings = Settings(subscription_installations_path=oversized, _env_file=None)
    assert (
        load_subscription_installations(oversized_settings, SubscriptionVerifierDependencies())
        == ()
    )

    path, payload = _codex_manifest(tmp_path / "conflict")
    policy = QuotaPolicy(
        route_pools=(
            QuotaRoutePool(
                "openai",
                "codex_app_server",
                "different",
                "monthly",
                model="gpt-6-astra",
            ),
        )
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    settings = Settings(
        subscription_installations_path=path,
        subscription_quota_policy=policy,
        _env_file=None,
    )
    verifier = SimpleNamespace(verify=lambda *_args: None)
    assert settings.subscription_quota_policy == policy
    assert (
        load_subscription_installations(settings, SubscriptionVerifierDependencies(codex=verifier))
        == ()
    )


def test_production_dependencies_exclude_gemini_until_a_trusted_verifier_exists(
    tmp_path: Path,
) -> None:
    dependencies = production_subscription_verifiers(lambda: None, tmp_path)  # type: ignore[arg-type]

    assert isinstance(dependencies.codex, CodexEvidenceVerifier)
    assert isinstance(dependencies.claude, ClaudeEvidenceVerifier)
    assert dependencies.gemini is None


@pytest.mark.parametrize("whitespace_model", ["   ", "\t", "  \t\n "])
def test_manifest_rejects_whitespace_only_model_and_loads_no_authority(
    tmp_path: Path, whitespace_model: str
) -> None:
    path, payload = _codex_manifest(tmp_path)
    payload["installations"][0]["model"] = whitespace_model
    path.write_text(json.dumps(payload), encoding="utf-8")
    settings = Settings(subscription_installations_path=path, _env_file=None)

    assert settings.subscription_quota_policy.route_pools == ()
    assert (
        load_subscription_installations(
            settings,
            SubscriptionVerifierDependencies(codex=SimpleNamespace(verify=lambda *_args: None)),
        )
        == ()
    )
    diagnostic = load_subscription_installations_diagnostic(
        settings,
        SubscriptionVerifierDependencies(codex=SimpleNamespace(verify=lambda *_args: None)),
    )
    assert diagnostic.adapters == ()
    assert diagnostic.readiness == ()


def _codex_manifest(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    tmp_path.mkdir(exist_ok=True)
    cwd, home = tmp_path / "cwd", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    executable = Path(sys.executable).resolve(strict=True)
    payload: dict[str, Any] = {
        "version": 2,
        "installations": [
            {
                "client": "codex_app_server",
                "executable": str(executable),
                "cwd": str(cwd),
                "home": str(home),
                "model": "gpt-6-astra",
                "effort": "low",
                "account": hashlib.sha256(b"account").hexdigest(),
                "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "client_version": "0.153.4",
                "quota": {"account": "personal", "pool": "weekly"},
            }
        ],
    }
    path = tmp_path / "installations.json"
    path.write_text(json.dumps(deepcopy(payload)), encoding="utf-8")
    return path, payload


# ---------------------------------------------------------------------------
# PR118 F3: Muse readiness/admission bind the resolved native binary,
# never the configured Windows shim.
# ---------------------------------------------------------------------------


def _muse_shim_layout(tmp_path: Path, *, marker: str | None = "1.4.7-R6101.1",
                      native: bool = True) -> tuple[Path, Path]:
    programs = tmp_path / "programs"
    programs.mkdir()
    shim = programs / "muse.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    if marker is not None:
        (programs / ".muse-version").write_text(marker + "\n", encoding="ascii")
    native_exe = programs / "muse-bin-1.4.7-R6101.1.exe"
    if native:
        native_exe.write_bytes(b"native-muse-build")
    return shim, native_exe


def _muse_manifest(tmp_path: Path, executable: Path, digest: str) -> Path:
    cwd, home = tmp_path / "cwd", tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    manifest = tmp_path / "installations.json"
    manifest.write_text(json.dumps({
        "version": 2,
        "installations": [{
            "client": "muse_code",
            "executable": str(executable.resolve()),
            "cwd": str(cwd.resolve()),
            "home": str(home.resolve()),
            "model": "muse-wonder-1.0",
            "effort": "medium",
            "account": hashlib.sha256(b"account").hexdigest(),
            "executable_digest": digest,
            "client_version": "1.4.7",
            "quota": {"account": "meta-sub", "pool": "allowance"},
        }],
    }), encoding="utf-8")
    return manifest


def _muse_settings(manifest: Path) -> Settings:
    return Settings(
        subscription_installations_path=manifest,
        subscription_client_trust=LocalCliTrust.OPERATOR,
        _env_file=None,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows muse.cmd shim layout")
@pytest.mark.parametrize("recorded", ["native", "shim"])
def test_muse_loader_binds_native_binary_not_shim(tmp_path: Path, recorded: str) -> None:
    from forge.agents.muse_runtime import MuseRuntimeAdapter

    shim, native_exe = _muse_shim_layout(tmp_path)
    native_digest = hashlib.sha256(native_exe.read_bytes()).hexdigest()
    shim_digest = hashlib.sha256(shim.read_bytes()).hexdigest()
    manifest = _muse_manifest(
        tmp_path, shim, native_digest if recorded == "native" else shim_digest,
    )
    load = load_subscription_installations_diagnostic(
        _muse_settings(manifest), SubscriptionVerifierDependencies(),
    )
    assert len(load.adapters) == 1
    adapter = load.adapters[0]
    assert isinstance(adapter, MuseRuntimeAdapter)
    assert Path(adapter.installation.executable) == native_exe.resolve()
    assert adapter.installation.executable_digest == native_digest
    changed = ReadinessWarning.CLIENT_BUILD_CHANGED in load.readiness[0].warnings
    assert changed is (recorded == "shim")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows muse.cmd shim layout")
@pytest.mark.parametrize("marker,native_present,expected", [
    (None, True, ReadinessReason.CONFIGURATION_INVALID),
    ("not-a-version", True, ReadinessReason.CONFIGURATION_INVALID),
    ("1.4.7-R6101.1", False, ReadinessReason.CONFIGURATION_INVALID),
])
def test_muse_loader_rejects_broken_shim_layout(
    tmp_path: Path, marker: str | None, native_present: bool, expected: ReadinessReason,
) -> None:
    shim, _native = _muse_shim_layout(tmp_path, marker=marker, native=native_present)
    manifest = _muse_manifest(tmp_path, shim, "a" * 64)
    load = load_subscription_installations_diagnostic(
        _muse_settings(manifest), SubscriptionVerifierDependencies(),
    )
    assert load.adapters == ()
    assert load.readiness[0].admitted is False
    assert load.readiness[0].reason is expected


def test_muse_loader_missing_shim_is_missing_executable(tmp_path: Path) -> None:
    manifest = _muse_manifest(tmp_path, tmp_path / "no-muse.cmd", "a" * 64)
    load = load_subscription_installations_diagnostic(
        _muse_settings(manifest), SubscriptionVerifierDependencies(),
    )
    assert load.adapters == ()
    assert load.readiness[0].reason is ReadinessReason.MISSING_EXECUTABLE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows muse.cmd shim layout")
def test_muse_adapter_for_direct_caller_resolves_native(tmp_path: Path) -> None:
    from forge.domain.subscription_installations import MuseInstallationSpec

    shim, native_exe = _muse_shim_layout(tmp_path)
    cwd, home = tmp_path / "cwd", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    spec = MuseInstallationSpec.model_validate({
        "client": "muse_code",
        "executable": str(shim.resolve()),
        "cwd": str(cwd.resolve()),
        "home": str(home.resolve()),
        "model": "muse-wonder-1.0",
        "effort": "medium",
        "account": hashlib.sha256(b"account").hexdigest(),
        "executable_digest": hashlib.sha256(shim.read_bytes()).hexdigest(),
        "client_version": "1.4.7",
        "quota": {"account": "meta-sub", "pool": "allowance"},
    })
    adapter = installations._adapter_for(
        spec, SubscriptionVerifierDependencies(), trust=LocalCliTrust.OPERATOR,
    )
    assert adapter is not None
    assert Path(adapter.installation.executable) == native_exe.resolve()
    assert adapter.installation.executable_digest == hashlib.sha256(
        native_exe.read_bytes()
    ).hexdigest()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows muse.cmd shim layout")
def test_muse_adapter_tracks_native_update_after_loading(tmp_path: Path) -> None:
    from uuid import uuid4

    from forge.agents.muse_runtime import MuseAuthoringHome, muse_launch_spec

    shim, native_exe = _muse_shim_layout(tmp_path)
    manifest = _muse_manifest(tmp_path, shim, hashlib.sha256(native_exe.read_bytes()).hexdigest())
    load = load_subscription_installations_diagnostic(
        _muse_settings(manifest), SubscriptionVerifierDependencies(),
    )
    installation = load.adapters[0].installation
    assert installation.configured_executable == str(shim.resolve())
    (shim.parent / ".muse-version").write_text("1.4.8-R6102.1", encoding="ascii")
    updated = shim.parent / "muse-bin-1.4.8-R6102.1.exe"
    updated.write_bytes(b"updated native Muse")
    home = MuseAuthoringHome(installation, uuid4())
    home.prepare("prompt", {"type": "object"})
    try:
        launch = muse_launch_spec(
            installation, home, mcp_name="forge",
            mcp_server={"transport": "stdio", "command": "unused", "args": [], "env": {}},
            tools=(), duration=1,
        )
        assert Path(launch.argv[0]) == updated.resolve()
        assert launch.executable_digest == hashlib.sha256(updated.read_bytes()).hexdigest()
    finally:
        home.cleanup()
