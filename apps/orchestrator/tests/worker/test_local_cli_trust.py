"""Personal installations run without manufacturing capability evidence."""

import json
from pathlib import Path

import pytest
from forge.domain.local_cli import LocalCliTrust
from forge.settings import Settings
from forge.worker.subscription_installations import (
    SubscriptionVerifierDependencies,
    load_subscription_installations_diagnostic,
)
from forge.worker.subscription_readiness import SubscriptionReadinessEnricher
from test_subscription_installations import _codex_manifest
from test_subscription_readiness import quota


@pytest.mark.parametrize(
    ("client", "model", "effort"),
    [
        ("codex_app_server", "gpt-6-astra", "low"),
        ("claude_code", "claude-opus-5", "medium"),
        ("gemini_cli", "gemini-3.1-pro-preview", "medium"),
        ("antigravity_cli", "gemini-3.8-flash-medium", "medium"),
    ],
)
def test_personal_clients_are_available_without_a_capability_verifier(
    tmp_path: Path, client: str, model: str, effort: str
) -> None:
    path, payload = _codex_manifest(tmp_path)
    payload["installations"][0].update(client=client, model=model, effort=effort)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_subscription_installations_diagnostic(
        Settings(subscription_installations_path=path, _env_file=None),
        SubscriptionVerifierDependencies(),
    )

    assert len(loaded.adapters) == 1
    assert loaded.adapters[0].installation.duration_seconds == 300
    status = loaded.readiness[0]
    assert status.admitted
    assert status.reason.value == "operator_trusted"
    assert status.evidence == ()
    assert "operator_trusted" in [warning.value for warning in status.warnings]


@pytest.mark.parametrize("quota_state", ["unknown", "blocked", "eligible"])
async def test_personal_readiness_uses_quota_without_reading_evidence(tmp_path, quota_state):
    path, _ = _codex_manifest(tmp_path)
    loaded = load_subscription_installations_diagnostic(
        Settings(subscription_installations_path=path, _env_file=None),
        SubscriptionVerifierDependencies(),
    )
    quota_status = quota(quota_state)
    status = (
        await SubscriptionReadinessEnricher(None, quota_status, loaded.specs).enrich(
            loaded.readiness
        )
    )[0]
    assert status.reason.value == "operator_trusted"
    assert status.quota.value == quota_state
    assert len(quota_status.calls) == 1
    assert status.evidence == ()


@pytest.mark.parametrize("trust", [LocalCliTrust.OPERATOR, LocalCliTrust.VERIFIED])
def test_upgraded_binary_is_observed_without_demanding_new_evidence(tmp_path, trust):
    path, payload = _codex_manifest(tmp_path)
    actual = payload["installations"][0]["executable_digest"]
    payload["installations"][0]["executable_digest"] = "a" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_subscription_installations_diagnostic(
        Settings(
            subscription_installations_path=path, subscription_client_trust=trust, _env_file=None
        ),
        SubscriptionVerifierDependencies(),
    )
    if trust is LocalCliTrust.OPERATOR:
        assert loaded.adapters[0].installation.executable_digest == actual
        assert "client_build_changed" in loaded.readiness[0].wire()["warnings"]
    else:
        assert not loaded.adapters
        assert loaded.readiness[0].reason.value == "executable_digest_mismatch"
