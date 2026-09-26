import json
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from capability_support import fake_capability_evidence
from forge.agents.claude_gateway import (
    ClaudeCapabilityReport,
    ClaudeGateway,
    ClaudeInstallation,
)
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.domain.capability_evidence import CapabilityEvidenceScope
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteSpec,
    SpecialistPurpose,
)
from forge.domain.tool import ToolName
from test_subscription_protocol import _request


@pytest.fixture(autouse=True)
def _supported_isolation_platform(monkeypatch):
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported", lambda: True
    )


@dataclass
class _Verifier:
    report: ClaudeCapabilityReport

    def verify(self, installation, scope):
        return replace(
            self.report,
            evidence=fake_capability_evidence(
                scope=scope,
                client_version="2.1.263",
                executable_digest=installation.executable_digest,
                client_home=installation.client_home,
                account=installation.account,
                verifier_id="fake-claude-conformance",
            ),
        )


def _report(**changes):
    values = {
        "installed_version": "2.1.263",
        "subscription_auth": True,
        "model": "claude-test",
        "effort": "medium",
        "builtins_disabled": True,
        "hooks_disabled": True,
        "strict_mcp": True,
        "client_home": str(Path.cwd().resolve()),
        "account": "test-account",
        "executable_digest": "b" * 64,
    }
    values.update(changes)
    return ClaudeCapabilityReport(**values)


def _gateway(report=None):
    return ClaudeGateway(
        ClaudeInstallation(
            executable=sys.executable,
            cwd=".",
            model="claude-test",
            effort="medium",
            client_home=str(Path.cwd().resolve()),
            account="test-account",
            executable_digest="b" * 64,
        ),
        _Verifier(report or _report()),
    )


def _scope() -> CapabilityEvidenceScope:
    return CapabilityEvidenceScope(
        route=RouteSpec(
            provider="anthropic",
            client="claude_code",
            model="claude-test",
            effort=ReasoningEffort.MEDIUM,
            auth_mode=AuthMode.SUBSCRIPTION,
            billing_mode=BillingMode.ALLOWANCE_ONLY,
        ),
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
    )


def _verified(report: ClaudeCapabilityReport) -> tuple[ClaudeCapabilityReport, ClaudeInstallation]:
    gateway = _gateway(report)
    scope = _scope()
    return gateway._verifier.verify(gateway._installation, scope), gateway._installation


def test_command_registers_only_sdk_forge_mcp_and_separates_system_prompt():
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    command = _gateway()._command(request)
    assert "--verbose" in command and "--tools=" in command
    assert {
        "--restricted",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-chrome",
    } <= set(command)
    assert command[command.index("--permission-prompts") + 1] == "none"
    managed = json.loads(command[command.index("--managed-settings") + 1])
    assert managed == {
        "allowManagedHooksOnly": True,
        "disableClaudeAiConnectors": True,
        "disableCommandPluginSources": True,
        "syncClaudeAiPlugins": False,
        "syncClaudeAiSkills": False,
    }
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{"forge":{"type":"sdk"}}}'
    assert "--disallowed-tools" not in command
    assert command[command.index("--system-prompt") + 1] == request.trusted_system_prompt
    assert "--allowed-tools=mcp__forge__repository_read_file" in command


def test_verifier_failure_does_not_admit_route():
    report, installation = _verified(_report(subscription_auth=False))
    assert not report.admits(installation, _scope())


def test_claude_admission_requires_subscription_auth_but_not_removed_billing_field():
    report, installation = _verified(_report())
    assert report.admits(installation, _scope())
    assert not replace(report, subscription_auth=False).admits(installation, _scope())


@pytest.mark.parametrize(
    "field",
    [
        "subscription_auth",
        "builtins_disabled",
        "hooks_disabled",
        "strict_mcp",
    ],
)
@pytest.mark.parametrize("value", [1, "false", None])
def test_capability_admission_requires_exact_verified_boolean(field, value):
    report, installation = _verified(_report(**{field: value}))
    assert not report.admits(installation, _scope())


def test_absent_hook_isolation_proof_does_not_admit_route():
    report = _report()
    values = {
        name: getattr(report, name)
        for name in report.__dataclass_fields__
        if name != "hooks_disabled"
    }
    missing, installation = _verified(ClaudeCapabilityReport(**values))
    assert not missing.admits(installation, _scope())


def test_all_verified_capabilities_admit_matching_installation():
    report, installation = _verified(_report())
    assert report.admits(installation, _scope())


def test_capability_booleans_without_source_evidence_do_not_admit():
    installation = _gateway()._installation
    assert not _report(evidence=None).admits(installation, _scope())


def _anthropic_request(*, tools: frozenset[ToolName] = frozenset()):
    request = _request(tools=tools)
    effective = replace(
        request.task.route.effective,
        provider="anthropic",
        client="claude_code",
        model="claude-test",
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    binding = replace(request.task.route, requested=effective, effective=effective)
    routes = tuple(
        (purpose, binding if purpose is request.task.purpose else route)
        for purpose, route in request.envelope.routes
    )
    return replace(
        request,
        task=replace(request.task, route=binding),
        envelope=replace(request.envelope, routes=routes),
    )


async def test_execute_offloads_platform_probe_from_event_loop(monkeypatch) -> None:
    probe_threads: list[threading.Thread] = []

    def _probe() -> bool:
        probe_threads.append(threading.current_thread())
        return False

    monkeypatch.setattr("forge.agents.claude_gateway.claude_isolation_platform_supported", _probe)
    gateway = _gateway()
    request = _anthropic_request()
    result = await gateway.execute(request)
    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert len(probe_threads) == 1
    assert probe_threads[0] is not threading.main_thread()
