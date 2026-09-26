"""Contract tests for request-time provider runtime construction."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from forge.agents.runtime_factory import AgentRuntimeFactory, RouteUnavailable
from forge.application.ports.agents import AgentGateway
from forge.domain.agent import AgentRequest
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
)


def _binding(*, model: str = "gemini-3.8-flash-medium") -> RouteBinding:
    route = RouteSpec(
        provider="google",
        client="gemini",
        model=model,
        effort=ReasoningEffort.MEDIUM,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    return RouteBinding(requested=route, effective=route)


@dataclass
class _Adapter:
    route: RouteSpec
    gateway: AgentGateway
    calls: int = 0

    def gateway_for(self, binding: RouteBinding, tool_provider: object) -> AgentGateway:
        assert binding.effective == self.route
        self.calls += 1
        return self.gateway


class _Tools:
    def tools_for(self, request: object) -> object:
        return request


class _Gateway:
    async def execute(self, request: object) -> object:
        return request


@pytest.mark.asyncio
async def test_subscription_factory_binds_exact_invocation_and_result_attempt() -> None:
    from dataclasses import replace

    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInterrupted,
        SubscriptionInvocationResult,
    )
    from test_subscription_protocol import _request

    request = _request()
    other = _request()
    broker, lifecycle = object(), object()

    class Gateway:
        interrupted = False
        result = SubscriptionInvocationResult(
            attempt=request.attempt, failure=SubscriptionFailure.UNAVAILABLE
        )

        async def execute(self, invocation):
            if self.interrupted:
                raise SubscriptionInterrupted(self.result)
            return self.result

    class Adapter:
        route = request.task.route.effective

        def gateway_for(self, invocation, *, broker, lifecycle):
            assert invocation == request
            assert broker is expected_broker and lifecycle is expected_lifecycle
            return gateway

    expected_broker, expected_lifecycle = broker, lifecycle
    gateway = Gateway()
    factory = AgentRuntimeFactory(subscription_adapters=(Adapter(),))
    assert factory.subscription_routes == frozenset({request.task.route.effective})
    bound = factory.subscription_gateway_for(request, broker=broker, lifecycle=lifecycle)
    assert await bound.execute(request) == gateway.result
    with pytest.raises(RouteUnavailable):
        await bound.execute(other)
    with pytest.raises(RouteUnavailable):
        await bound.execute(replace(request, trusted_system_prompt="changed"))
    gateway.result = replace(gateway.result, attempt=other.attempt)
    with pytest.raises(RouteUnavailable):
        await bound.execute(request)
    gateway.interrupted = True
    with pytest.raises(SubscriptionInterrupted) as interruption:
        await bound.execute(request)
    assert interruption.value.result.attempt == request.attempt
    assert interruption.value.result.failure is SubscriptionFailure.PROTOCOL


def test_subscription_factory_never_resolves_through_legacy_adapter() -> None:
    from test_subscription_protocol import _request

    request = _request()
    adapter = _Adapter(request.task.route.effective, _Gateway())
    factory = AgentRuntimeFactory((adapter,))
    with pytest.raises(RouteUnavailable):
        factory.subscription_gateway_for(request, broker=object(), lifecycle=object())
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_factory_resolves_only_the_exact_frozen_route() -> None:
    gateway = _Gateway()
    binding = _binding()
    adapter = _Adapter(binding.effective, gateway)  # type: ignore[arg-type]
    factory = AgentRuntimeFactory((adapter,))

    request = AgentRequest.model_construct(provider="google", model=binding.effective.model)
    assert await factory.gateway_for(binding, _Tools()).execute(request) is request
    assert adapter.calls == 1


def test_factory_never_uses_primary_or_nearby_route_as_a_fallback() -> None:
    gateway = _Gateway()
    bound = _binding()
    other = _binding(model="gemini-2.5-pro")
    factory = AgentRuntimeFactory((_Adapter(other.effective, gateway),))  # type: ignore[arg-type]

    with pytest.raises(RouteUnavailable):
        factory.gateway_for(bound, _Tools())


def test_factory_rejects_adapter_pin_mismatch() -> None:
    gateway = _Gateway()
    bound = _binding()
    mismatched = RouteSpec(
        provider="google",
        client="gemini",
        model=bound.effective.model,
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    factory = AgentRuntimeFactory((_Adapter(mismatched, gateway),))  # type: ignore[arg-type]

    with pytest.raises(RouteUnavailable):
        factory.gateway_for(bound, _Tools())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,model", [("other", "gemini-3.8-flash-medium"), ("google", "other")]
)
async def test_resolved_gateway_rejects_request_route_drift(provider: str, model: str) -> None:
    binding = _binding()
    adapter = _Adapter(binding.effective, _Gateway())  # type: ignore[arg-type]
    gateway = AgentRuntimeFactory((adapter,)).gateway_for(binding, _Tools())
    # Only the route fields are exercised; the adapter must never receive this request.
    request = AgentRequest.model_construct(provider=provider, model=model)
    with pytest.raises(RouteUnavailable):
        await gateway.execute(request)
