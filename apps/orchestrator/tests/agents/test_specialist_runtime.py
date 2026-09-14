"""Forge's specialist registrations bind exact routes and fresh attempt state."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from forge.agents.capability_verification import capability_scope
from forge.agents.claude_runtime import ClaudeRuntimeAdapter
from forge.agents.gemini_runtime import GeminiRuntimeAdapter
from forge.agents.runtime_factory import AgentRuntimeFactory, RouteUnavailable
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.domain.subscription import SpecialistPurpose
from forge.domain.tool import ToolName
from test_claude_supervised import _anthropic_request
from test_claude_supervised import _Broker as ClaudeBroker
from test_claude_supervised import _gateway as claude_gateway
from test_codex_runtime import Lifecycle
from test_gemini_gateway import _Broker as GeminiBroker
from test_gemini_gateway import _gateway as gemini_gateway
from test_gemini_gateway import _google_request
from test_subscription_protocol import _request


@pytest.fixture(params=("claude", "gemini"))
def specialist(request, tmp_path):
    provider = request.param
    fake = claude_gateway("success") if provider == "claude" else gemini_gateway(tmp_path, "tool")
    return SimpleNamespace(
        adapter_type=ClaudeRuntimeAdapter if provider == "claude" else GeminiRuntimeAdapter,
        fake=fake,
        request=_anthropic_request if provider == "claude" else _google_request,
        broker=ClaudeBroker if provider == "claude" else GeminiBroker,
        billing_field="allowance_only_enforced" if provider == "claude" else "billing_never",
    )


async def test_registration_keeps_concurrent_brokers_and_lifecycles_separate(specialist):
    fake = specialist.fake
    adapter = specialist.adapter_type(fake._installation, fake._verifier)
    factory = AgentRuntimeFactory(subscription_adapters=(adapter,))
    requests = [
        specialist.request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})) for _ in range(2)
    ]
    brokers, lifecycles = [specialist.broker(), specialist.broker()], [Lifecycle(), Lifecycle()]
    gateways = [
        factory.subscription_gateway_for(request, broker=broker, lifecycle=lifecycle)
        for request, broker, lifecycle in zip(requests, brokers, lifecycles, strict=True)
    ]
    assert factory.subscription_routes == frozenset({requests[0].task.route.effective})
    assert all(not lifecycle.events for lifecycle in lifecycles)
    results = await asyncio.gather(
        *(gateway.execute(request) for gateway, request in zip(gateways, requests, strict=True))
    )
    for request, broker, lifecycle, result in zip(
        requests, brokers, lifecycles, results, strict=True
    ):
        assert result.attempt == request.attempt and result.failure is None
        assert result.launch_proof.permits_decision and broker.revoked
        assert len(broker.calls) == 1
        assert [kind for kind, _ in lifecycle.events] == ["intent", "started", "finished"]
        assert {identity for _, identity in lifecycle.events} == {result.launch_proof.launch_id}
        assert result.quota_exhaustion is None and not result.telemetry.is_quota_known
    assert results[0].launch_proof.launch_id != results[1].launch_proof.launch_id


async def test_binding_is_lazy_and_rechecks_changed_billing_proof(specialist):
    fake = specialist.fake
    requests = [
        specialist.request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})) for _ in range(2)
    ]
    reports = [fake._verifier.verify(fake._installation, capability_scope(requests[0]))]
    verified = []

    def verify(installation, scope):
        verified.append(installation)
        return reports[0]

    adapter = specialist.adapter_type(fake._installation, SimpleNamespace(verify=verify))
    factory = AgentRuntimeFactory(subscription_adapters=(adapter,))
    brokers, lifecycles = [specialist.broker(), specialist.broker()], [Lifecycle(), Lifecycle()]
    gateways = [
        factory.subscription_gateway_for(request, broker=broker, lifecycle=lifecycle)
        for request, broker, lifecycle in zip(requests, brokers, lifecycles, strict=True)
    ]
    assert verified == [] and all(not item.events for item in lifecycles)
    assert (await gateways[0].execute(requests[0])).failure is None
    reports[0] = replace(reports[0], **{specialist.billing_field: False})
    rejected = await gateways[1].execute(requests[1])
    assert rejected.failure is SubscriptionFailure.UNAVAILABLE and rejected.launch_proof is None
    assert len(verified) == 2 and lifecycles[1].events == []
    assert brokers[1].revoked and brokers[1].calls == []
    assert rejected.quota_exhaustion is None and not rejected.telemetry.is_quota_known


@pytest.mark.parametrize("changes", [{"model": "gemini-unapproved"}, {"effort": "high"}])
def test_registration_does_not_substitute_a_nearby_model_or_effort(specialist, changes):
    fake = specialist.fake
    adapter = specialist.adapter_type(replace(fake._installation, **changes), fake._verifier)
    factory = AgentRuntimeFactory(subscription_adapters=(adapter,))
    for bind in (adapter.gateway_for, factory.subscription_gateway_for):
        with pytest.raises(RouteUnavailable):
            bind(specialist.request(), broker=specialist.broker(), lifecycle=Lifecycle())


def test_specialist_registration_cannot_replace_the_selected_primary(specialist):
    fake = specialist.fake
    factory = AgentRuntimeFactory(
        subscription_adapters=(specialist.adapter_type(fake._installation, fake._verifier),)
    )
    request = _request(purpose=SpecialistPurpose.PRIMARY)
    selected = request.task.route
    with pytest.raises(RouteUnavailable):
        factory.subscription_gateway_for(request, broker=specialist.broker(), lifecycle=Lifecycle())
    assert request.task.route == selected


@pytest.mark.parametrize("field", ["installation", "verifier"])
def test_invalid_dependencies_are_rejected_without_execution(specialist, field):
    fake = specialist.fake
    arguments = {"installation": fake._installation, "verifier": fake._verifier}
    arguments[field] = None
    with pytest.raises(TypeError, match="trusted installation and verification"):
        specialist.adapter_type(**arguments)


def test_claude_registration_rejects_invalid_clock_and_effort():
    fake = claude_gateway("success")
    with pytest.raises(TypeError, match="trusted installation and verification"):
        ClaudeRuntimeAdapter(fake._installation, fake._verifier, now=None)
    with pytest.raises(ValueError, match="supported reasoning effort") as error:
        ClaudeRuntimeAdapter(replace(fake._installation, effort="automatic"), fake._verifier)
    assert "automatic" not in str(error.value)


def test_gemini_registration_rejects_missing_effort(tmp_path):
    fake = gemini_gateway(tmp_path, "tool")
    with pytest.raises(ValueError, match="explicit reasoning effort"):
        GeminiRuntimeAdapter(replace(fake._installation, effort=None), fake._verifier)


async def test_factory_rejects_cross_attempt_reuse_before_launch(specialist):
    fake = specialist.fake
    adapter = specialist.adapter_type(fake._installation, fake._verifier)
    factory = AgentRuntimeFactory(subscription_adapters=(adapter,))
    lifecycle = Lifecycle()
    gateway = factory.subscription_gateway_for(
        specialist.request(), broker=specialist.broker(), lifecycle=lifecycle
    )
    with pytest.raises(RouteUnavailable):
        await gateway.execute(specialist.request())
    assert lifecycle.events == []
