"""Frozen subscription selection determines which planning service is invoked."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.handlers.planning import PlanningHandler


@pytest.mark.parametrize("selected", [False, True])
async def test_planning_handler_dispatches_from_frozen_envelope(selected):
    calls = []

    async def legacy_execute(command, work):
        calls.append("legacy")
        return "legacy-result"

    async def subscription_execute(command, work):
        calls.append("subscription")
        return "subscription-result"

    async def envelope_for_run(run_id):
        return object() if selected else None

    work = SimpleNamespace(subscription=SimpleNamespace(envelope_for_run=envelope_for_run))
    handler = PlanningHandler(
        SimpleNamespace(execute=legacy_execute),
        subscription_service=SimpleNamespace(execute=subscription_execute),
    )
    result = await handler(SimpleNamespace(run_id=uuid4()), work)
    assert calls == ["subscription" if selected else "legacy"]
    assert result == ("subscription-result" if selected else "legacy-result")


async def test_route_lookup_failure_never_falls_back_to_legacy():
    async def lookup(run_id):
        raise RuntimeError("route storage unavailable")

    async def forbidden(*args):
        raise AssertionError("no planner may run without route proof")

    handler = PlanningHandler(
        SimpleNamespace(execute=forbidden), subscription_service=SimpleNamespace(execute=forbidden)
    )
    with pytest.raises(RuntimeError, match="route storage"):
        await handler(
            SimpleNamespace(run_id=uuid4()),
            SimpleNamespace(subscription=SimpleNamespace(envelope_for_run=lookup)),
        )
