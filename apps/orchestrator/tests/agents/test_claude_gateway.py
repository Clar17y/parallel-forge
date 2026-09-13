import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from forge.agents.claude_gateway import (
    ClaudeCapabilityReport,
    ClaudeGateway,
    ClaudeInstallation,
)
from forge.domain.tool import ToolName
from test_subscription_protocol import _request


@dataclass
class _Verifier:
    report: ClaudeCapabilityReport

    def verify(self, _installation):
        return self.report


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
        ),
        _Verifier(report or _report()),
    )


def test_command_registers_only_sdk_forge_mcp_and_separates_system_prompt():
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    command = _gateway()._command(request)
    assert "--verbose" in command and "--tools=" in command
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{"forge":{"type":"sdk"}}}'
    assert "--disallowed-tools" not in command
    assert command[command.index("--system-prompt") + 1] == request.trusted_system_prompt
    assert "--allowed-tools=mcp__forge__repository.read_file" in command


def test_verifier_failure_does_not_admit_route():
    assert not _report(allowance_only_enforced=False).admits(_gateway()._installation)


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
    assert not _report(**{field: value}).admits(_gateway()._installation)


def test_absent_hook_isolation_proof_does_not_admit_route():
    report = _report()
    values = {
        name: getattr(report, name)
        for name in report.__dataclass_fields__
        if name != "hooks_disabled"
    }
    assert not ClaudeCapabilityReport(**values).admits(_gateway()._installation)


def test_all_verified_capabilities_admit_matching_installation():
    assert _report().admits(_gateway()._installation)
