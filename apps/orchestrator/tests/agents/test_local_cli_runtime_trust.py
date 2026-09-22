"""Operator trust reaches real supervised callbacks, with no evidence reads."""

from dataclasses import replace
from pathlib import Path

import pytest
from forge.agents.claude_gateway import ClaudeGateway
from forge.agents.codex_gateway import CodexGateway
from forge.agents.gemini_gateway import GeminiGateway
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.domain.local_cli import LocalCliTrust
from forge.domain.tool import ToolName
from test_claude_supervised import _anthropic_request
from test_claude_supervised import _Broker as ClaudeBroker
from test_claude_supervised import _gateway as claude_gateway
from test_codex_gateway import _Broker
from test_codex_gateway import _gateway as codex_gateway
from test_gemini_gateway import _gateway as gemini_gateway
from test_gemini_gateway import _google_request
from test_subscription_protocol import _request


class NoEvidence:
    def verify(self, *_args):
        raise AssertionError("Personal runtime must not consult capability evidence")


@pytest.mark.parametrize("client", ["codex", "claude", "gemini"])
async def test_operator_trust_runs_supervised_tools_without_evidence(
    tmp_path: Path, monkeypatch, client: str
) -> None:
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported",
        lambda: (_ for _ in ()).throw(AssertionError("No Linux isolation prerequisite")),
    )
    broker = ClaudeBroker() if client == "claude" else _Broker()
    tools = frozenset({ToolName.REPOSITORY_READ_FILE})
    if client == "codex":
        installation = codex_gateway("tool")._installation
        gateway = CodexGateway(
            installation, NoEvidence(), broker=broker, trust=LocalCliTrust.OPERATOR
        )
        request = _request(tools=tools)
    elif client == "claude":
        installation = claude_gateway("tool")._installation
        gateway = ClaudeGateway(
            installation, NoEvidence(), broker=broker, trust=LocalCliTrust.OPERATOR
        )
        request = _anthropic_request(tools=tools)
    else:
        installation = gemini_gateway(tmp_path, "tool")._installation
        gateway = GeminiGateway(
            installation, NoEvidence(), broker=broker, trust=LocalCliTrust.OPERATOR
        )
        request = _google_request(tools=tools)

    result = await gateway.execute(request)

    assert result.failure is None
    assert result.decision is not None
    assert len(broker.calls) == 1
    assert broker.revoked
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed


async def test_personal_codex_does_not_require_effective_isolation_metadata():
    source = codex_gateway("tool")._installation
    script = source.script[1].replace('config["mcp_servers"]={}', "config={}")
    broker = _Broker()
    result = await CodexGateway(
        replace(source, script=("-c", script)), broker=broker, trust=LocalCliTrust.OPERATOR
    ).execute(_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))
    assert result.failure is None
    assert len(broker.calls) == 1


@pytest.mark.parametrize("scenario", ["settings_drift", "init_drift", "late_init_drift"])
async def test_personal_claude_does_not_gate_on_inherited_customizations(scenario):
    result = await ClaudeGateway(
        claude_gateway(scenario)._installation,
        broker=ClaudeBroker(),
        trust=LocalCliTrust.OPERATOR,
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))
    assert result.failure is None


@pytest.mark.parametrize(
    "scenario", ["account_mismatch", "account_identity_mismatch", "foreign", "stale_tool"]
)
async def test_personal_codex_still_rejects_known_auth_or_callback_mismatch(scenario):
    result = await CodexGateway(
        codex_gateway(scenario)._installation,
        broker=_Broker(),
        trust=LocalCliTrust.OPERATOR,
    ).execute(_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))
    assert result.failure in {SubscriptionFailure.AUTHENTICATION, SubscriptionFailure.PROTOCOL}
