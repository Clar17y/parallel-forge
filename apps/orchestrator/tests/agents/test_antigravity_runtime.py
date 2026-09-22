import asyncio
import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from forge.agents.antigravity_runtime import AntigravityGateway, AntigravityInstallation
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
)
from forge.domain.tool import ToolName
from test_codex_gateway import _Broker
from test_gemini_gateway import _google_request, _Lifecycle


def request():
    value = _google_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    route = replace(
        value.task.route.effective, client="antigravity_cli", model="gemini-3.8-flash-medium"
    )
    binding = replace(value.task.route, requested=route, effective=route)
    return replace(
        value,
        task=replace(value.task, route=binding),
        envelope=replace(
            value.envelope,
            routes=tuple(
                (purpose, binding if purpose is value.task.purpose else existing)
                for purpose, existing in value.envelope.routes
            ),
        ),
    )


def gateway(tmp_path, scenario, *, broker=None, duration=5):
    executable = Path(sys.executable).resolve()
    installation = AntigravityInstallation(
        executable=str(executable),
        cwd=str(tmp_path),
        home=str(tmp_path),
        model="gemini-3.8-flash-medium",
        effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
        script=(str(Path(__file__).with_name("antigravity_runtime_peer.py")), scenario),
        duration_seconds=duration,
    )
    lifecycle = _Lifecycle()
    return AntigravityGateway(
        installation, broker=broker or _Broker(), lifecycle=lifecycle
    ), lifecycle


async def test_antigravity_uses_real_mcp_and_supervised_stream_without_capability_evidence(
    tmp_path,
):
    broker = _Broker()
    runtime, lifecycle = gateway(tmp_path, "success", broker=broker)
    result = await runtime.execute(request())
    assert result.failure is None, [value.stderr for value in lifecycle.results]
    assert result.decision.summary == "done"
    assert len(broker.calls) == 1 and broker.revoked
    assert result.telemetry.input_tokens == 13 and result.telemetry.output_tokens == 5
    assert result.launch_proof.stop_confirmed
    assert not list(tmp_path.glob(".forge-agy-*"))


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("bad_usage", SubscriptionFailure.PROTOCOL),
        ("429", SubscriptionFailure.THROTTLED),
        ("401", SubscriptionFailure.AUTHENTICATION),
        ("provider_cancel", SubscriptionFailure.INTERRUPTED),
        ("bad_tool", SubscriptionFailure.PROTOCOL),
    ],
)
async def test_antigravity_rejects_bad_data_and_never_invents_exhaustion(
    tmp_path, scenario, expected
):
    runtime, _ = gateway(tmp_path, scenario)
    result = await runtime.execute(request())
    assert result.failure is expected
    assert result.quota_exhaustion is None
    assert result.launch_proof.stop_confirmed


async def test_antigravity_cancellation_settles_the_mcp_child(tmp_path):
    broker = _Broker()
    runtime, _ = gateway(tmp_path, "cancel", broker=broker)
    task = asyncio.create_task(runtime.execute(request()))
    await asyncio.wait_for(broker.called.wait(), 5)
    task.cancel()
    with pytest.raises(SubscriptionInterrupted) as interrupted:
        await task
    assert broker.revoked
    assert interrupted.value.result.launch_proof.stop_confirmed


async def test_antigravity_deadline_returns_stopped_tree(tmp_path):
    runtime, _ = gateway(tmp_path, "cancel", duration=0.5)
    result = await runtime.execute(request())
    assert result.failure is SubscriptionFailure.DEADLINE
    assert result.launch_proof.stop_confirmed


async def test_missing_usage_stays_unknown_and_does_not_block_personal_use(tmp_path):
    runtime, _ = gateway(tmp_path, "missing_usage")
    result = await runtime.execute(request())
    assert result.failure is None
    assert result.telemetry.input_tokens is None
    assert result.telemetry.output_tokens is None


async def test_token_budget_failure_retains_actual_usage(tmp_path):
    runtime, _ = gateway(tmp_path, "success")
    value = request()
    value = replace(
        value, task=replace(value.task, budget=replace(value.task.budget, max_input_tokens=10))
    )
    result = await runtime.execute(value)
    assert result.failure is SubscriptionFailure.BUDGET
    assert result.telemetry.input_tokens == 13
    assert result.telemetry.output_tokens == 5
    assert result.launch_proof.stop_confirmed


async def test_antigravity_failed_revoke_still_closes_transport_and_stops_child(
    tmp_path, monkeypatch
):
    from forge.agents.local_cli_mcp import LocalCliMcp

    closed = []
    original_close = LocalCliMcp.close

    async def close(mcp):
        await original_close(mcp)
        closed.append(True)

    monkeypatch.setattr(LocalCliMcp, "close", close)

    class FailedRevoke(_Broker):
        async def revoke(self):
            raise RuntimeError("fixture revoke error")

    runtime, _ = gateway(tmp_path, "success", broker=FailedRevoke())
    result = await runtime.execute(request())
    assert result.failure is SubscriptionFailure.UNCERTAIN
    assert result.launch_proof is not None
    assert result.launch_proof.outcome == "stop_uncertain"
    assert closed == [True]
