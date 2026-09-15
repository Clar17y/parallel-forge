"""Claude's verified configuration directory is explicit and isolated per launch."""

import os
import sys
from dataclasses import replace

import pytest
from forge.agents.capability_verification import capability_scope
from forge.agents.claude_gateway import ClaudeGateway, ClaudeInstallation
from forge.agents.client_process import ClientProcessSupervisor
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.domain.tool import ToolName
from test_claude_supervised import (
    _EXECUTABLE_DIGEST,
    _anthropic_request,
    _Broker,
    _gateway,
    _report,
    _Verifier,
)


async def test_claude_launch_uses_only_the_verified_client_home(tmp_path, monkeypatch):
    home = tmp_path / "verified-home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient-home"))
    monkeypatch.setenv(
        "CLAUDE_CODE_MANAGED_SETTINGS_PATH", str(tmp_path / "ambient-managed-policy")
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-ambient-value")
    base = _gateway("success")
    installation = replace(base._installation, client_home=str(home))
    report = _report(client_home=str(home.resolve()))
    broker = _Broker()
    launches = []

    class Capture:
        async def start(self, spec, **kwargs):
            launches.append(spec)
            return await ClientProcessSupervisor().start(spec, **kwargs)

    result = await ClaudeGateway(
        installation, _Verifier(report), broker=broker, supervisor=Capture()
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))
    assert result.failure is None and result.launch_proof.stop_confirmed
    assert broker.revoked and len(broker.calls) == 1
    assert len(launches) == 1
    environment = launches[0].environment
    assert environment["CLAUDE_CONFIG_DIR"] == str(home.resolve())
    assert environment["CLAUDE_CODE_MANAGED_SETTINGS_PATH"] == str(home.resolve())
    assert launches[0].executable_digest == installation.executable_digest
    assert set(environment) == {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
    } | ({"SystemRoot"} if os.name == "nt" else set())


async def test_claude_home_proof_mismatch_rejects_before_launch(tmp_path):
    home = tmp_path / "verified-home"
    home.mkdir()
    broker = _Broker()
    installation = ClaudeInstallation(
        executable=sys.executable,
        cwd=str(tmp_path),
        model="claude-test",
        effort="medium",
        client_home=str(home),
        account="test-account",
        executable_digest=_EXECUTABLE_DIGEST,
    )

    class NoLaunch:
        async def start(self, *_args, **_kwargs):
            raise AssertionError("mismatched home must not launch")

    result = await ClaudeGateway(
        installation,
        _Verifier(_report(client_home=str(tmp_path))),
        broker=broker,
        supervisor=NoLaunch(),
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))
    assert result.failure is SubscriptionFailure.UNAVAILABLE and result.launch_proof is None
    assert result.quota_exhaustion is None and broker.revoked and broker.calls == []


async def test_managed_policy_created_by_verifier_rejects_before_launch(tmp_path):
    home = tmp_path / "verified-home"
    home.mkdir()
    base = _gateway("success")
    installation = replace(base._installation, client_home=str(home))
    report = _report(client_home=str(home.resolve()))
    broker = _Broker()

    class CreatesPolicy:
        async def verify(self, *_args):
            (home / "managed-settings.json").write_text("{}", encoding="utf-8")
            return report

    class NoLaunch:
        async def start(self, *_args, **_kwargs):
            raise AssertionError("managed policy drift must not launch")

    result = await ClaudeGateway(
        installation, CreatesPolicy(), broker=broker, supervisor=NoLaunch()
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))

    assert result.failure is SubscriptionFailure.UNAVAILABLE and result.launch_proof is None
    assert result.quota_exhaustion is None and broker.revoked and broker.calls == []


async def test_system_managed_mcp_policy_rejects_before_launch(tmp_path, monkeypatch):
    home, system = tmp_path / "home", tmp_path / "system-managed-mcp.json"
    home.mkdir()
    system.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "forge.agents.claude_gateway._claude_system_managed_mcp_paths", lambda: (system,)
    )
    base, broker = _gateway("success"), _Broker()
    installation = replace(base._installation, client_home=str(home))

    class NoLaunch:
        async def start(self, *_args, **_kwargs):
            raise AssertionError("system managed MCP must reject before launch")

    result = await ClaudeGateway(
        installation,
        _Verifier(_report(client_home=str(home))),
        broker=broker,
        supervisor=NoLaunch(),
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))
    assert (
        result.failure is SubscriptionFailure.UNAVAILABLE and broker.calls == [] and broker.revoked
    )


@pytest.mark.parametrize("scenario", ["settings_drift", "init_drift", "foreign_init"])
async def test_effective_configuration_drift_rejects_before_user_or_callback(scenario):
    broker = _Broker()
    result = await _gateway(scenario, broker=broker).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )

    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert result.quota_exhaustion is None and broker.calls == [] and broker.revoked


@pytest.mark.parametrize("scenario", ["duplicate_early_init", "late_init_drift"])
async def test_init_metadata_must_be_unique_and_match_the_effective_policy(scenario):
    broker = _Broker()
    result = await _gateway(scenario, broker=broker).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )

    assert result.failure in {SubscriptionFailure.PROTOCOL, SubscriptionFailure.UNAVAILABLE}
    assert result.quota_exhaustion is None and broker.calls == [] and broker.revoked


@pytest.mark.parametrize("value", [None, "", ".", "missing", 1])
def test_client_home_requires_an_existing_absolute_directory(tmp_path, value):
    with pytest.raises(ValueError, match="explicit existing absolute client home"):
        ClaudeInstallation(
            executable=sys.executable,
            cwd=str(tmp_path),
            model="claude-test",
            effort="medium",
            client_home=value,
            account="test-account",
            executable_digest="b" * 64,
        )


def test_file_is_not_an_authentication_directory(tmp_path):
    file = tmp_path / "file"
    file.write_text("fixture")
    with pytest.raises(ValueError, match="explicit existing absolute client home"):
        ClaudeInstallation(
            executable=sys.executable,
            cwd=str(tmp_path),
            model="claude-test",
            effort="medium",
            client_home=str(file),
            account="test-account",
            executable_digest="b" * 64,
        )


def test_home_is_canonical_and_omitted_from_representations(tmp_path):
    home = tmp_path / "dedicated-client-home"
    home.mkdir()
    installation = ClaudeInstallation(
        executable=sys.executable,
        cwd=str(tmp_path),
        model="claude-test",
        effort="medium",
        client_home=str(home / ".." / home.name),
        account="test-account",
        executable_digest=_EXECUTABLE_DIGEST,
    )
    assert installation.client_home == str(home.resolve())
    scope = capability_scope(_anthropic_request())
    report = _Verifier(_report(client_home=str(home.resolve()))).verify(installation, scope)
    assert report.admits(installation, scope)
    assert "dedicated-client-home" not in repr(installation) + repr(report)


@pytest.mark.parametrize(
    "changes",
    [
        {"client_home": None},
        {"client_home": "relative"},
        {"installed_version": "2.1.268"},
        {"allowance_only_enforced": False},
        {"hooks_disabled": False},
    ],
)
def test_home_binding_does_not_supply_other_missing_capabilities(changes):
    gateway = _gateway("success")
    scope = capability_scope(_anthropic_request())
    report = _Verifier(_report(**changes)).verify(gateway._installation, scope)
    assert not report.admits(gateway._installation, scope)
