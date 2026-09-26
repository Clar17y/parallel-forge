"""Provider execution uses the reserved attempt ceiling, not the aggregate task."""

from dataclasses import replace

import pytest
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.domain.tool import ToolName
from test_claude_supervised import (
    _anthropic_request,
    _supported_isolation_platform,  # noqa: F401 - imported autouse fixture
)
from test_claude_supervised import _Broker as ClaudeBroker
from test_claude_supervised import _gateway as claude_gateway
from test_codex_gateway import _Broker as CodexBroker
from test_codex_gateway import _gateway as codex_gateway
from test_subscription_protocol import _request


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("limit", ["tools", "input_tokens", "output_tokens"])
async def test_reserved_attempt_limits_stop_provider(provider, limit):
    tools = frozenset({ToolName.REPOSITORY_READ_FILE})
    request = _request(tools=tools) if provider == "codex" else _anthropic_request(tools=tools)
    changes = {
        "tools": {"max_tool_calls": 0},
        "input_tokens": {"max_input_tokens": 12},
        "output_tokens": {"max_output_tokens": 4},
    }[limit]
    reserved = replace(request.task.budget, max_provider_attempts=1, max_repairs=0, **changes)
    request = replace(request, attempt_budget=reserved)
    broker = CodexBroker() if provider == "codex" else ClaudeBroker()
    gateway = (
        codex_gateway("tool", broker=broker)
        if provider == "codex"
        else claude_gateway("success", broker=broker)
    )
    result = await gateway.execute(request)
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert broker.revoked
    if limit == "tools":
        assert broker.calls == []
        assert result.telemetry.tool_call_count == 0
    else:
        assert result.telemetry.input_tokens == 13
        assert result.telemetry.output_tokens == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_duration_seconds", 61),
        ("max_tool_calls", 5),
        ("max_named_checks", 3),
        ("max_provider_attempts", 2),
        ("max_repairs", 1),
    ],
)
def test_request_rejects_attempt_ceiling_outside_task(field, value):
    request = _request()
    reserved = replace(request.task.budget, max_provider_attempts=1, max_repairs=0)
    with pytest.raises(ValueError, match="attempt budget"):
        replace(request, attempt_budget=replace(reserved, **{field: value}))


def test_request_cannot_weaken_task_telemetry_policy():
    request = _request()
    strict = replace(request.task.budget.unknown_telemetry_policy, allow_unknown_tokens=False)
    request = replace(
        request,
        task=replace(
            request.task, budget=replace(request.task.budget, unknown_telemetry_policy=strict)
        ),
    )
    reserved = replace(
        request.task.budget,
        max_provider_attempts=1,
        max_repairs=0,
        unknown_telemetry_policy=replace(strict, allow_unknown_tokens=True),
    )
    with pytest.raises(ValueError, match="attempt budget"):
        replace(request, attempt_budget=reserved)


@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_zero_attempt_duration_is_a_deadline_without_launch(provider):
    request = _request() if provider == "codex" else _anthropic_request()
    request = replace(
        request,
        attempt_budget=replace(
            request.task.budget, max_duration_seconds=0, max_provider_attempts=1, max_repairs=0
        ),
    )
    gateway = codex_gateway("tool") if provider == "codex" else claude_gateway("success")
    result = await gateway.execute(request)
    assert result.failure is SubscriptionFailure.DEADLINE
    assert result.launch_proof is None
