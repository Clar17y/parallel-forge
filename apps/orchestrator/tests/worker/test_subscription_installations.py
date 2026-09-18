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
from forge.domain.subscription_quota import QuotaPolicy, QuotaPoolKey, QuotaRoutePool
from forge.settings import Settings
from forge.worker.subscription_installations import (
    SubscriptionVerifierDependencies,
    load_subscription_installations,
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
    settings = Settings(subscription_installations_path=manifest, _env_file=None)
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

    assert (
        load_subscription_installations(settings, SubscriptionVerifierDependencies(codex=verifier))
        == ()
    )


def test_absent_and_malformed_manifests_leave_settings_healthy(tmp_path: Path) -> None:
    missing = Settings(subscription_installations_path=tmp_path / "missing.json", _env_file=None)
    assert load_subscription_installations(missing, SubscriptionVerifierDependencies()) == ()

    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text('{"version":2,"version":2,"installations":[]}', encoding="utf-8")
    malformed = Settings(subscription_installations_path=malformed_path, _env_file=None)
    assert malformed.subscription_quota_policy.route_pools == ()
    assert load_subscription_installations(malformed, SubscriptionVerifierDependencies()) == ()


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
