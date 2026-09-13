from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from forge.agents.codex_gateway import CodexCapabilityReport, CodexGateway, CodexInstallation
from forge.agents.subscription_protocol import ProviderToolCall, tool_input_schema
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
)
from forge.domain.tool import ToolName
from test_subscription_protocol import _request


@dataclass
class _Verifier:
    report: CodexCapabilityReport

    def verify(self, installation: CodexInstallation) -> CodexCapabilityReport:
        return self.report


class _Broker:
    def __init__(self) -> None:
        self.calls: list[ProviderToolCall] = []
        self.revoked = False
        self.called = asyncio.Event()

    async def __call__(self, call: ProviderToolCall) -> dict[str, object]:
        assert not self.revoked
        self.calls.append(call)
        self.called.set()
        return {"status": "succeeded", "path": call.arguments.get("path")}

    async def revoke(self) -> None:
        self.revoked = True


def _report(**changes: object) -> CodexCapabilityReport:
    values: dict[str, object] = {
        "supported": True,
        "installed_version": "0.153.4",
        "account_kind": "chatgpt",
        "model": "gpt-5.6-luna",
        "effort": "medium",
        "billing_allowance_enforced": True,
        "native_tools_isolated": True,
        "client_home": str(Path.cwd()),
    }
    values.update(changes)
    return CodexCapabilityReport(**values)  # type: ignore[arg-type]


def _script(scenario: str) -> str:
    # A supervised fake peer uses the installed 0.153.4 field shapes and checks
    # critical outgoing fields. It does not establish live client conformance.
    return f"""import json,sys,time,tomllib
scenario={scenario!r}
def recv(method):
 m=json.loads(sys.stdin.readline())
 if m.get("method") != method: raise SystemExit("expected "+method+" got "+repr(m))
 return m
def send(value): print(json.dumps(value),flush=True)
m=recv("initialize"); send({{"jsonrpc":"2.0","id":m["id"],"result":{{"userAgent":"fake/0.153.4"}}}})
recv("initialized")
m=recv("account/read"); send({{"jsonrpc":"2.0","id":m["id"],"result":{{"account":{{"type":"apiKey" if scenario=="account_mismatch" else "chatgpt"}}}}}})
if scenario=="account_mismatch": raise SystemExit(0)
m=recv("model/list"); models=[] if scenario=="model_missing" else [{{"id":"gpt-5.6-luna","supportedReasoningEfforts":[{{"reasoningEffort":"medium"}}]}}]
send({{"jsonrpc":"2.0","id":m["id"],"result":{{"data":models}}}})
if scenario=="model_missing": raise SystemExit(0)
m=recv("config/read")
config=tomllib.loads(chr(10).join(sys.argv[2::2]))
config["mcp_servers"]={{}}
send({{"id":m["id"],"result":{{"config":config}}}})
m=recv("thread/start")
p=m["params"]
assert p["model"]=="gpt-5.6-luna" and p["allowProviderModelFallback"] is False
assert p["environments"]==[] and p["ephemeral"] is True
if scenario=="schema":
 s=p["dynamicTools"][0]["inputSchema"]
 assert s["type"]=="object" and s["additionalProperties"] is False
 assert "path" in s["properties"] and "path" in s["required"]
send({{"jsonrpc":"2.0","id":m["id"],"result":{{"thread":{{"id":"thread-actual"}},"model":"gpt-5.6-luna"}}}})
m=recv("turn/start"); p=m["params"]
assert p["threadId"]=="thread-actual" and p["model"]=="gpt-5.6-luna"
assert p["effort"]=="medium" and p["environments"]==[]
send({{"jsonrpc":"2.0","id":m["id"],"result":{{"turn":{{"id":"turn-actual"}}}}}})
if scenario=="late_started":
 send({{"method":"turn/started","params":{{"threadId":"thread-actual","turn":{{"id":"turn-actual","items":[],"status":"inProgress"}}}}}})
final=json.dumps({{"decision":{{"kind":"handoff","status":"blocked","summary":"done","candidate_commit":None,"candidate_tree_digest":"","changed_paths":[],"check_results":[],"evidence_receipt_ids":[],"residual_concerns":[],"scope_request_paths":[]}}}})
if scenario=="quota_notification":
 error={{"message":"Usage limit reached. Resets in 2 minutes.","codexErrorInfo":"usageLimitExceeded"}}
 send({{"method":"error","params":{{"threadId":"thread-actual","turnId":"turn-actual","error":error,"willRetry":False}}}})
 send({{"method":"turn/completed","params":{{"threadId":"thread-actual","turn":{{"id":"turn-actual","items":[],"status":"failed","error":error}}}}}})
elif scenario in ("usageLimitExceeded", "rateLimitExceeded", "sessionBudgetExceeded"):
 send({{"method":"turn/completed","params":{{"threadId":"thread-actual","turn":{{"id":"turn-actual","items":[],"status":"failed","error":{{"codexErrorInfo":scenario,"resetAfterSeconds":120}}}}}}}})
elif scenario=="foreign":
 send({{"method":"item/completed","params":{{"threadId":"other","turnId":"turn-actual","item":{{"id":"i","type":"agentMessage","text":final}}}}}})
elif scenario=="failed_after_item":
 send({{"method":"thread/tokenUsage/updated","params":{{"threadId":"thread-actual","turnId":"turn-actual","tokenUsage":{{"total":{{"inputTokens":13,"outputTokens":5,"cachedInputTokens":2}}}}}}}})
 send({{"method":"item/completed","params":{{"threadId":"thread-actual","turnId":"turn-actual","item":{{"id":"i","type":"agentMessage","text":final}}}}}})
 send({{"method":"turn/completed","params":{{"threadId":"thread-actual","turn":{{"id":"turn-actual","items":[],"status":"failed","error":{{"message":"provider failed"}}}}}}}})
elif scenario=="malformed_final":
 send({{"method":"item/completed","params":{{"threadId":"thread-actual","turnId":"turn-actual","item":{{"id":"i","type":"agentMessage","text":"{{"}}}}}})
elif scenario=="native_request":
 send({{"jsonrpc":"2.0","id":90,"method":"item/fileChange/request","params":{{"threadId":"thread-actual","turnId":"turn-actual"}}}})
elif scenario=="stale_tool":
 send({{"jsonrpc":"2.0","id":80,"method":"item/tool/call","params":{{"callId":"stale","threadId":"thread-actual","turnId":"old-turn","tool":"repository.read_file","arguments":{{"path":"README.md"}}}}}})
elif scenario in ("tool", "schema", "cancel"):
 send({{"jsonrpc":"2.0","id":80,"method":"item/tool/call","params":{{"callId":"call-actual","threadId":"thread-actual","turnId":"turn-actual","tool":"repository.read_file","arguments":{{"path":"README.md"}}}}}})
 r=recv(None) if False else json.loads(sys.stdin.readline())
 assert r["id"]==80 and r["result"]["success"] is True
 if scenario=="cancel":
  time.sleep(30)
 else:
  send({{"jsonrpc":"2.0","id":81,"method":"item/tool/call","params":{{"callId":"call-actual","threadId":"thread-actual","turnId":"turn-actual","tool":"repository.read_file","arguments":{{"path":"README.md"}}}}}})
  r=json.loads(sys.stdin.readline()); assert r["id"]==81
  send({{"method":"thread/tokenUsage/updated","params":{{"threadId":"thread-actual","turnId":"turn-actual","tokenUsage":{{"total":{{"inputTokens":13,"outputTokens":5,"cachedInputTokens":2}}}}}}}})
  send({{"method":"thread/status/changed","params":{{"threadId":"thread-actual","status":{{"type":"active"}}}}}})
  send({{"method":"item/started","params":{{"threadId":"thread-actual","turnId":"turn-actual","item":{{"id":"i","type":"agentMessage","text":""}}}}}})
  send({{"method":"item/completed","params":{{"threadId":"thread-actual","turnId":"turn-actual","item":{{"id":"i","type":"agentMessage","text":final}}}}}})
  send({{"method":"turn/completed","params":{{"threadId":"thread-actual","turn":{{"id":"turn-actual","items":[],"status":"completed"}}}}}})
elif scenario=="duplicate_conflict":
 for rid,path in ((80,"README.md"),(81,"other.txt")):
  send({{"jsonrpc":"2.0","id":rid,"method":"item/tool/call","params":{{"callId":"same","threadId":"thread-actual","turnId":"turn-actual","tool":"repository.read_file","arguments":{{"path":path}}}}}})
  if rid==80: json.loads(sys.stdin.readline())
else:
 send({{"method":"item/completed","params":{{"threadId":"thread-actual","turnId":"turn-actual","item":{{"id":"i","type":"agentMessage","text":final}}}}}})
 completed={{"method":"turn/completed","params":{{"threadId":"thread-actual","turn":{{"id":"turn-actual","items":[],"status":"completed"}}}}}}
 if scenario=="completion_request": completed["id"]=99
 send(completed)
"""


def _gateway(
    scenario: str,
    *,
    broker: _Broker | None = None,
    report: CodexCapabilityReport | None = None,
    duration: float = 5,
    quota_limit_id: str | None = None,
) -> CodexGateway:
    return CodexGateway(
        CodexInstallation(
            executable=sys.executable,
            cwd=".",
            model="gpt-5.6-luna",
            effort="medium",
            client_home=str(Path.cwd()),
            quota_limit_id=quota_limit_id,
            script=("-c", _script(scenario)),
            duration_seconds=duration,
        ),
        _Verifier(report or _report(quota_limit_id=quota_limit_id)),
        broker=broker,
    )


@pytest.mark.asyncio
async def test_success_waits_for_matching_completed_turn_and_ignores_notifications() -> None:
    broker = _Broker()
    result = await _gateway("tool", broker=broker).execute(
        _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is None and result.decision is not None
    assert result.launch_proof is not None and result.launch_proof.permits_decision
    assert result.attempt.attempt_id == result.decision.attempt_id
    assert [(c.call_key, c.thread_id, c.turn_id) for c in broker.calls] == [
        ("call-actual", "thread-actual", "turn-actual")
    ]
    assert broker.revoked is True
    assert (
        result.telemetry.input_tokens,
        result.telemetry.output_tokens,
        result.telemetry.cached_input_tokens,
    ) == (13, 5, 2)


@pytest.mark.asyncio
async def test_item_output_does_not_override_failed_turn() -> None:
    result = await _gateway("failed_after_item").execute(_request())
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert result.decision is None
    assert (
        result.telemetry.input_tokens,
        result.telemetry.output_tokens,
        result.telemetry.cached_input_tokens,
    ) == (13, 5, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "failure"),
    [
        ("account_mismatch", SubscriptionFailure.AUTHENTICATION),
        ("model_missing", SubscriptionFailure.UNAVAILABLE),
    ],
)
async def test_runtime_account_and_model_catalog_must_match(
    scenario: str, failure: SubscriptionFailure
) -> None:
    result = await _gateway(scenario).execute(_request())
    assert result.failure is failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["foreign", "stale_tool", "duplicate_conflict", "malformed_final", "native_request"],
)
async def test_unadmitted_or_malformed_messages_fail_closed(scenario: str) -> None:
    broker = _Broker()
    result = await _gateway(scenario, broker=broker).execute(
        _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert broker.revoked is True
    assert len(broker.calls) <= 1


@pytest.mark.asyncio
async def test_dynamic_tool_uses_strict_canonical_argument_schema() -> None:
    schema = tool_input_schema(ToolName.REPOSITORY_READ_FILE)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["path"]
    result = await _gateway("schema", broker=_Broker()).execute(
        _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"installed_version": "0.153.3"},
        {"installed_version": "0.154.0"},
        {"model": "other"},
        {"effort": "low"},
        {"billing_allowance_enforced": False},
        {"native_tools_isolated": False},
    ],
)
async def test_exact_capability_mismatch_never_launches(change: dict[str, Any]) -> None:
    result = await _gateway("success", report=_report(**change)).execute(_request())
    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert result.decision is None


@pytest.mark.asyncio
async def test_pending_cancellation_revokes_broker() -> None:
    broker = _Broker()
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    task = asyncio.create_task(_gateway("cancel", broker=broker, duration=20).execute(request))
    await asyncio.wait_for(broker.called.wait(), timeout=3)
    task.cancel()
    with pytest.raises(SubscriptionInterrupted) as raised:
        await task
    assert broker.revoked is True
    assert raised.value.result.attempt == request.attempt
    assert raised.value.result.failure is SubscriptionFailure.INTERRUPTED
    assert raised.value.result.telemetry.input_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,success", [("late_started", True), ("completion_request", False)]
)
async def test_turn_notifications_are_order_tolerant_but_never_requests(scenario, success) -> None:
    result = await _gateway(scenario).execute(_request())
    assert (result.failure is None) is success


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("usageLimitExceeded", SubscriptionFailure.QUOTA),
        ("rateLimitExceeded", SubscriptionFailure.THROTTLED),
        ("sessionBudgetExceeded", SubscriptionFailure.BUDGET),
    ],
)
async def test_supervised_terminal_limit_classification(scenario, expected):
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    gateway = _gateway(scenario)
    gateway._now = lambda: now
    result = await gateway.execute(_request())
    assert result.failure is expected and result.decision is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    if expected is SubscriptionFailure.QUOTA:
        assert result.quota_exhaustion is not None
        assert result.quota_exhaustion.reset_at == now + timedelta(seconds=120)
    else:
        assert result.quota_exhaustion is None


@pytest.mark.asyncio
async def test_official_terminal_error_notification_reaches_quota_settlement():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    gateway = _gateway("quota_notification")
    gateway._now = lambda: now
    result = await gateway.execute(_request())
    assert result.failure is SubscriptionFailure.QUOTA
    assert result.decision is None
    assert result.quota_exhaustion is not None
    assert result.quota_exhaustion.observed_at == now
    assert result.quota_exhaustion.reset_at == now + timedelta(minutes=2)
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
