"""The ADK gateway retains each measured request beside its aggregate."""

import pytest
from forge.agents.adk_gateway import GoogleAdkGateway
from forge.agents.errors import AgentOutputInvalid
from forge.domain.actor import AgentRole
from test_adk_gateway import (
    _FakeAdkRuntime,
    _FakeAdkToolProvider,
    _make_invocation_result,
    _make_loader,
    _make_request,
    _pricing_catalog,
)
from test_contracts import build_plan_output


@pytest.mark.parametrize("repair, invalid", [(False, False), (True, False), (True, True)])
async def test_gateway_retains_attempt_ids_and_usage(repair: bool, invalid: bool) -> None:
    valid = build_plan_output().model_dump_json()
    responses = [
        _make_invocation_result("{}" if repair else valid, provider_request_id="first-request")
    ]
    if repair:
        responses.append(
            _make_invocation_result(
                "{}" if invalid else valid, provider_request_id="repair-request"
            )
        )
    runtime = _FakeAdkRuntime(responses)
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())
    request = _make_request(AgentRole.PLANNER)
    if invalid:
        with pytest.raises(AgentOutputInvalid) as raised:
            await gateway.execute(request)
        attempts = raised.value.usage_attempts
        aggregate = raised.value.usage
    else:
        result = await gateway.execute(request)
        attempts = result.usage_attempts
        aggregate = result.usage
    assert [attempt.provider_request_id for attempt in attempts] == (
        ["first-request", "repair-request"] if repair else ["first-request"]
    )
    assert aggregate is not None
    assert sum(attempt.input_tokens for attempt in attempts) == aggregate.input_tokens
    assert sum(attempt.duration_ms for attempt in attempts) == aggregate.duration_ms
    assert len(runtime.invocations) == len(attempts)
