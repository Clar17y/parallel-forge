"""Claude's verified configuration directory is explicit and isolated per launch."""

import os
import sys
from dataclasses import replace

import pytest
from forge.agents.claude_gateway import ClaudeGateway, ClaudeInstallation
from forge.agents.client_process import ClientProcessSupervisor
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.domain.tool import ToolName
from test_claude_supervised import _anthropic_request, _Broker, _gateway, _report, _Verifier


async def test_claude_launch_uses_only_the_verified_client_home(tmp_path, monkeypatch):
    home = tmp_path / "verified-home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient-home"))
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
    assert set(environment) == {"CLAUDE_CONFIG_DIR"} | (
        {"SystemRoot"} if os.name == "nt" else set()
    )


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


@pytest.mark.parametrize("value", [None, "", ".", "missing", 1])
def test_client_home_requires_an_existing_absolute_directory(tmp_path, value):
    with pytest.raises(ValueError, match="explicit existing absolute client home"):
        ClaudeInstallation(
            executable=sys.executable,
            cwd=str(tmp_path),
            model="claude-test",
            effort="medium",
            client_home=value,
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
    )
    assert installation.client_home == str(home.resolve())
    report = _report(client_home=str(home.resolve()))
    assert report.admits(installation)
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
    assert not _report(**changes).admits(_gateway("success")._installation)
