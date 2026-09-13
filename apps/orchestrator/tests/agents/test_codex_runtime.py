"""The production Codex registration is lazy and binds a fresh attempt gateway."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from forge.agents.codex_gateway import CodexCapabilityReport
from forge.agents.codex_runtime import CodexRuntimeAdapter
from forge.agents.runtime_factory import AgentRuntimeFactory, RouteUnavailable
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.domain.subscription import SpecialistPurpose
from forge.domain.tool import ToolName
from test_codex_gateway import _Broker, _gateway, _report
from test_subscription_protocol import _request


class Lifecycle:
    def __init__(self):
        self.events = []

    async def launch_intent(self, launch_id):
        self.events.append(("intent", launch_id))

    async def started(self, receipt):
        self.events.append(("started", receipt.launch_id))

    async def finished(self, receipt, result):
        assert result.stop_confirmed
        self.events.append(("finished", receipt.launch_id))


async def test_production_registration_binds_concurrent_brokers_and_lifecycles():
    fake = _gateway("tool")
    adapter = CodexRuntimeAdapter(fake._installation, fake._verifier)
    requests = [_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})) for _ in range(2)]
    factory = AgentRuntimeFactory(subscription_adapters=(adapter,))
    assert factory.subscription_routes == frozenset({requests[0].task.route.effective})
    brokers, lifecycles = [_Broker(), _Broker()], [Lifecycle(), Lifecycle()]
    gateways = [
        factory.subscription_gateway_for(request, broker=broker, lifecycle=lifecycle)
        for request, broker, lifecycle in zip(requests, brokers, lifecycles, strict=True)
    ]
    assert all(not value.events for value in lifecycles)
    assert all(not value.calls for value in brokers)
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
        assert {ident for _, ident in lifecycle.events} == {result.launch_proof.launch_id}
    assert results[0].launch_proof.launch_id != results[1].launch_proof.launch_id


async def test_construction_is_lazy_and_capability_changes_are_checked_for_every_attempt():
    fake = _gateway("success")
    reports = [_report()]
    verified = []

    def verify(installation):
        verified.append(installation)
        return reports[0]

    adapter = CodexRuntimeAdapter(fake._installation, SimpleNamespace(verify=verify))
    factory = AgentRuntimeFactory(subscription_adapters=(adapter,))
    requests = [_request(), _request()]
    lifecycles = [Lifecycle(), Lifecycle()]
    gateways = [
        factory.subscription_gateway_for(request, broker=_Broker(), lifecycle=lifecycle)
        for request, lifecycle in zip(requests, lifecycles, strict=True)
    ]
    assert verified == [] and all(not value.events for value in lifecycles)
    assert (await gateways[0].execute(requests[0])).failure is None
    reports[0] = CodexCapabilityReport.unavailable("fixture billing proof no longer valid")
    rejected = await gateways[1].execute(requests[1])
    assert rejected.failure is SubscriptionFailure.UNAVAILABLE and rejected.launch_proof is None
    assert len(verified) == 2 and lifecycles[1].events == []


@pytest.mark.parametrize("changes", [{"model": "unapproved-model"}, {"effort": "high"}])
def test_registration_never_substitutes_a_nearby_model_or_effort(changes):
    fake = _gateway("success")
    adapter = CodexRuntimeAdapter(replace(fake._installation, **changes), fake._verifier)
    factory = AgentRuntimeFactory(subscription_adapters=(adapter,))
    request = _request()
    for bind in (adapter.gateway_for, factory.subscription_gateway_for):
        with pytest.raises(RouteUnavailable):
            bind(request, broker=_Broker(), lifecycle=Lifecycle())


def test_specialist_registration_does_not_replace_the_selected_primary():
    fake = _gateway("success")
    factory = AgentRuntimeFactory(
        subscription_adapters=(CodexRuntimeAdapter(fake._installation, fake._verifier),)
    )
    request = _request(purpose=SpecialistPurpose.PRIMARY)
    selected = request.task.route
    with pytest.raises(RouteUnavailable):
        factory.subscription_gateway_for(request, broker=_Broker(), lifecycle=Lifecycle())
    assert request.task.route == selected


@pytest.mark.parametrize("field", ["installation", "verifier", "now"])
def test_invalid_runtime_dependencies_are_rejected_without_execution(field):
    fake = _gateway("success")
    arguments = {"installation": fake._installation, "verifier": fake._verifier}
    arguments[field] = None
    with pytest.raises(TypeError, match="trusted installation and verification"):
        CodexRuntimeAdapter(**arguments)


def test_unrecognized_effort_cannot_produce_a_registered_route():
    fake = _gateway("success")
    with pytest.raises(ValueError, match="supported reasoning effort") as error:
        CodexRuntimeAdapter(replace(fake._installation, effort="automatic"), fake._verifier)
    assert "automatic" not in str(error.value)


async def test_factory_binding_rejects_cross_attempt_reuse_before_client_launch():
    fake = _gateway("success")
    factory = AgentRuntimeFactory(
        subscription_adapters=(CodexRuntimeAdapter(fake._installation, fake._verifier),)
    )
    lifecycle = Lifecycle()
    gateway = factory.subscription_gateway_for(_request(), broker=_Broker(), lifecycle=lifecycle)
    with pytest.raises(RouteUnavailable):
        await gateway.execute(_request())
    assert lifecycle.events == []
