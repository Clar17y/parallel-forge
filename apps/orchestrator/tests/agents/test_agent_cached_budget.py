"""Cached input is charged once and still counts toward the input budget."""

import pytest
from forge.agents.adk_gateway import GoogleAdkGateway
from forge.agents.errors import AgentBudgetExceeded
from forge.agents.fake_gateway import FakeAgentGateway, FakeAgentStep
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.observability.usage import PricingCatalog
from test_adk_gateway import (
    _FakeAdkRuntime,
    _FakeAdkToolProvider,
    _make_invocation_result,
    _make_loader,
    _make_request,
)
from test_contracts import build_agent_budget, build_agent_request, build_plan_output


@pytest.mark.parametrize(
    "uncached, expected",
    [(20, AgentFinishStatus.SUCCEEDED), (21, AgentFinishStatus.BUDGET_EXCEEDED)],
)
async def test_fake_counts_cached_input_toward_total_budget(
    uncached: int, expected: AgentFinishStatus
) -> None:
    gateway = FakeAgentGateway(
        {
            AgentRole.PLANNER: [
                FakeAgentStep.success(
                    build_plan_output(),
                    input_tokens=uncached,
                    cached_input_tokens=80,
                )
            ]
        }
    )
    result = await gateway.execute(
        build_agent_request(budget=build_agent_budget(max_input_tokens=100))
    )
    assert result.finish_status is expected


async def test_gateway_splits_provider_total_input_for_pricing() -> None:
    catalog = PricingCatalog.from_mapping(
        version="cached-budget-test",
        entries={
            "google:gemini-2.5-flash": {
                "input_per_million": "10000",
                "output_per_million": "0",
                "cached_input_per_million": "1000",
            }
        },
    )
    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result(
                build_plan_output().model_dump_json(),
                input_tokens=100,
                output_tokens=0,
                cached_tokens=80,
                tool_calls=0,
            )
        ]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), catalog)
    result = await gateway.execute(
        _make_request(
            AgentRole.PLANNER,
            budget=build_agent_budget(max_input_tokens=100, max_cost_minor=28),
        )
    )
    assert result.finish_status is AgentFinishStatus.SUCCEEDED
    assert result.usage.input_tokens == 20
    assert result.usage.cached_input_tokens == 80
    assert result.usage.estimated_cost_minor == 28


async def test_gateway_rejects_cached_count_above_provider_total() -> None:
    from test_adk_gateway import _pricing_catalog

    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result(
                build_plan_output().model_dump_json(), input_tokens=100, cached_tokens=101
            )
        ]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())
    with pytest.raises(AgentBudgetExceeded):
        await gateway.execute(_make_request(AgentRole.PLANNER))
    assert len(runtime.invocations) == 1


@pytest.mark.parametrize(
    "input_cap, expected",
    [(None, AgentFinishStatus.SUCCEEDED), (99, AgentFinishStatus.BUDGET_EXCEEDED)],
)
async def test_optional_input_cap_does_not_replace_provider_capacity(
    input_cap: int | None, expected: AgentFinishStatus
) -> None:
    from test_adk_gateway import _pricing_catalog

    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result(
                build_plan_output().model_dump_json(), input_tokens=100, cached_tokens=80
            )
        ]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())
    result = await gateway.execute(
        _make_request(
            AgentRole.PLANNER,
            budget=build_agent_budget(max_input_tokens=input_cap, max_output_tokens=None),
        )
    )
    assert result.finish_status is expected
    assert runtime.invocations[0].max_input_tokens == input_cap
    assert runtime.invocations[0].max_output_tokens is None


@pytest.mark.parametrize(
    "total_cap, expected",
    [
        (None, AgentFinishStatus.SUCCEEDED),
        (110, AgentFinishStatus.SUCCEEDED),
        (109, AgentFinishStatus.BUDGET_EXCEEDED),
    ],
)
async def test_aggregate_hard_cap_counts_cached_input_once(
    total_cap: int | None, expected: AgentFinishStatus
) -> None:
    from test_adk_gateway import _pricing_catalog

    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result(
                build_plan_output().model_dump_json(),
                input_tokens=100,
                output_tokens=10,
                cached_tokens=80,
            )
        ]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())
    result = await gateway.execute(
        _make_request(
            AgentRole.PLANNER,
            budget=build_agent_budget(max_input_tokens=None, max_output_tokens=None).model_copy(
                update={"max_total_tokens": total_cap}
            ),
        )
    )
    assert result.finish_status is expected
    assert runtime.invocations[0].max_total_tokens == total_cap


async def test_fake_aggregate_hard_cap_counts_cached_input_once() -> None:
    gateway = FakeAgentGateway(
        {
            AgentRole.PLANNER: [
                FakeAgentStep.success(
                    build_plan_output(), input_tokens=20, cached_input_tokens=80, output_tokens=10
                )
            ]
        }
    )
    request = build_agent_request(
        budget=build_agent_budget(max_input_tokens=None, max_output_tokens=None).model_copy(
            update={"max_total_tokens": 109}
        )
    )
    result = await gateway.execute(request)
    assert result.finish_status is AgentFinishStatus.BUDGET_EXCEEDED


async def test_aggregate_hard_cap_applies_across_repair_attempts() -> None:
    from test_adk_gateway import _pricing_catalog

    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result('{"invalid": true}', input_tokens=50, output_tokens=1),
            _make_invocation_result(
                build_plan_output().model_dump_json(), input_tokens=60, output_tokens=1
            ),
        ]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())
    request = _make_request(
        AgentRole.PLANNER,
        budget=build_agent_budget(max_input_tokens=None, max_output_tokens=None).model_copy(
            update={"max_total_tokens": 100}
        ),
    )
    result = await gateway.execute(request)
    assert result.finish_status is AgentFinishStatus.BUDGET_EXCEEDED
    assert result.usage.input_tokens == 110
    assert runtime.invocations[1].max_total_tokens == 49
