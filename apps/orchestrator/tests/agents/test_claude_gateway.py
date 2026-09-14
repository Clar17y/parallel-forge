import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from capability_support import fake_capability_evidence
from forge.agents.claude_gateway import (
    ClaudeCapabilityReport,
    ClaudeGateway,
    ClaudeInstallation,
)
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
        "allowance_only_enforced": True,
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
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{"forge":{"type":"sdk"}}}'
    assert "--disallowed-tools" not in command
    assert command[command.index("--system-prompt") + 1] == request.trusted_system_prompt
    assert "--allowed-tools=mcp__forge__repository.read_file" in command


def test_verifier_failure_does_not_admit_route():
    report, installation = _verified(_report(allowance_only_enforced=False))
    assert not report.admits(installation, _scope())


@pytest.mark.parametrize(
    "field",
    [
        "subscription_auth",
        "builtins_disabled",
        "hooks_disabled",
        "strict_mcp",
        "allowance_only_enforced",
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
