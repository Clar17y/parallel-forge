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
