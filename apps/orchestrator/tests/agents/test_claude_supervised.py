from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from capability_support import fake_capability_evidence
from forge.agents.claude_gateway import (
    ClaudeCapabilityReport,
    ClaudeGateway,
    ClaudeInstallation,
)
from forge.agents.client_process import ClientProcessSupervisor
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
)
from forge.domain.capability_evidence import CapabilityEvidenceScope
from forge.domain.subscription import AuthMode, BillingMode
from forge.domain.tool import ToolName
from test_subscription_protocol import _request


@dataclass(frozen=True)
class _Verifier:
    report: ClaudeCapabilityReport
    bind_evidence: bool = True

    def verify(
        self, installation: ClaudeInstallation, scope: CapabilityEvidenceScope
    ) -> ClaudeCapabilityReport:
        if not self.bind_evidence:
            return self.report
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


class _Broker:
    def __init__(self) -> None:
        self.calls: list[ProviderToolCall] = []
        self.revoked = False
        self.called = asyncio.Event()

    async def __call__(self, call: ProviderToolCall) -> dict[str, object]:
        assert not self.revoked
        assert isinstance(call.arguments, MappingProxyType)
        self.calls.append(call)
        self.called.set()
        return {
            "status": "succeeded",
            "evidence": {"path": call.arguments["path"], "lines": [1, 2]},
        }

    async def revoke(self) -> None:
        self.revoked = True


class _Lifecycle:
    def __init__(self) -> None:
        self.result = None

    async def launch_intent(self, _launch_id: str) -> None:
        pass

    async def started(self, _receipt) -> None:
        pass

    async def finished(self, _receipt, result) -> None:
        self.result = result


class _UncertainLifecycle(_Lifecycle):
    async def finished(self, receipt, result) -> None:
        await super().finished(receipt, result)
        raise RuntimeError("receipt store unavailable")


def _report(**changes: Any) -> ClaudeCapabilityReport:
    values: dict[str, Any] = {
        "installed_version": "2.1.263",
        "subscription_auth": True,
        "model": "claude-test",
        "effort": "medium",
        "builtins_disabled": True,
        "strict_mcp": True,
        "allowance_only_enforced": True,
        "hooks_disabled": True,
        "client_home": str(Path.cwd().resolve()),
        "account": "test-account",
        "executable_digest": "b" * 64,
    }
    values.update(changes)
    return ClaudeCapabilityReport(**values)


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


def _gateway(
    scenario: str,
    *,
    broker: _Broker | None = None,
    report=None,
    duration: float = 5,
    lifecycle: _Lifecycle | None = None,
    bind_evidence: bool = True,
):
    return ClaudeGateway(
        ClaudeInstallation(
            executable=sys.executable,
            cwd=".",
            model="claude-test",
            effort="medium",
            client_home=str(Path.cwd().resolve()),
            account="test-account",
            executable_digest="b" * 64,
            script=(str(Path(__file__).with_name("claude_stream_peer.py")), scenario),
            duration_seconds=duration,
        ),
        _Verifier(report or _report(), bind_evidence),
        broker=broker,
        supervisor=ClientProcessSupervisor(),
        lifecycle=lifecycle,
    )


@pytest.mark.asyncio
async def test_real_supervisor_completes_official_control_mcp_and_terminal_exchange() -> None:
    broker = _Broker()
    lifecycle = _Lifecycle()
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    result = await _gateway("success", broker=broker, lifecycle=lifecycle).execute(request)
    assert lifecycle.result is not None, "supervised process did not settle"
    assert lifecycle.result.stderr == "", lifecycle.result.stderr
    assert lifecycle.result.stop_confirmed is True
    from forge.agents.client_process import terminal_launch_proof

    assert result.launch_proof == terminal_launch_proof(lifecycle.result)
    assert result.failure is None and result.decision is not None
    assert result.decision.attempt_id == request.attempt.attempt_id
    assert [(call.call_key, call.name, dict(call.arguments)) for call in broker.calls] == [
        ("7", "repository.read_file", {"path": "README.md"})
    ]
    assert broker.revoked is True
    assert (result.telemetry.input_tokens, result.telemetry.output_tokens) == (13, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["eof", "malformed", "wrong_session", "wrong_initialize", "duplicate_request"]
)
async def test_eof_malformed_and_foreign_terminal_fail_closed(scenario: str) -> None:
    broker = _Broker()
    result = await _gateway(scenario, broker=broker).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert result.decision is None
    assert broker.revoked is True


@pytest.mark.asyncio
async def test_tool_call_during_outer_initialize_is_never_authorized() -> None:
    broker = _Broker()
    result = await _gateway("tool_during_initialize", broker=broker).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert broker.calls == []
    assert broker.revoked is True


@pytest.mark.asyncio
async def test_jsonrpc_integer_and_string_ids_are_distinct_tool_authorities() -> None:
    broker = _Broker()
    result = await _gateway("typed_ids", broker=broker).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is None
    assert [
        (type(call.call_key), call.call_key, call.arguments["path"]) for call in broker.calls
    ] == [
        (str, "7", "README.md"),
        (str, '"7"', "OTHER.md"),
    ]


@pytest.mark.asyncio
async def test_cancellation_revokes_before_stop_and_preserves_observed_usage() -> None:
    broker = _Broker()
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    task = asyncio.create_task(_gateway("cancel", broker=broker, duration=20).execute(request))
    await asyncio.wait_for(broker.called.wait(), 3)
    task.cancel()
    with pytest.raises(SubscriptionInterrupted) as raised:
        await task
    assert broker.revoked is True
    assert raised.value.result.failure is SubscriptionFailure.INTERRUPTED
    assert (
        raised.value.result.telemetry.input_tokens,
        raised.value.result.telemetry.output_tokens,
    ) == (13, 5)


@pytest.mark.asyncio
async def test_timeout_revokes_after_inflight_tool_and_closes_process() -> None:
    broker = _Broker()
    lifecycle = _Lifecycle()
    result = await _gateway("cancel", broker=broker, duration=0.4, lifecycle=lifecycle).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.DEADLINE
    assert broker.revoked is True
    assert lifecycle.result is not None
    assert lifecycle.result.stop_confirmed is True


@pytest.mark.asyncio
async def test_uncertain_process_receipt_overrides_successful_provider_terminal() -> None:
    broker = _Broker()
    lifecycle = _UncertainLifecycle()
    result = await _gateway("success", broker=broker, lifecycle=lifecycle).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.UNCERTAIN
    assert result.decision is None
    assert broker.revoked is True
    assert lifecycle.result is not None and lifecycle.result.stop_confirmed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["usage_repeat", "usage_sum"])
async def test_assistant_usage_is_deduplicated_by_message_and_aggregated(scenario: str) -> None:
    result = await _gateway(scenario, broker=_Broker()).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is None
    assert (result.telemetry.input_tokens, result.telemetry.output_tokens) == (13, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"subscription_auth": False},
        {"builtins_disabled": False},
        {"strict_mcp": False},
        {"allowance_only_enforced": False},
        {"hooks_disabled": False},
    ],
)
async def test_unsupported_capability_is_rejected_before_process_launch(
    change: dict[str, Any],
) -> None:
    broker = _Broker()
    result = await _gateway("success", broker=broker, report=_report(**change)).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert broker.calls == []
    assert broker.revoked is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("generation_error", SubscriptionFailure.PROTOCOL),
        ("quota_status", SubscriptionFailure.THROTTLED),
        ("quota_exhausted", SubscriptionFailure.QUOTA),
        ("authentication_status", SubscriptionFailure.AUTHENTICATION),
    ],
)
async def test_terminal_failure_classification_uses_typed_status_without_substring_aliases(
    scenario, expected
):
    result = await _gateway(scenario, broker=_Broker()).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is expected
    assert result.decision is None
