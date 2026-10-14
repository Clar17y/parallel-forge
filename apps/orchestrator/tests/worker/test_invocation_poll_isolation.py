from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError


@pytest.mark.asyncio
async def test_invocation_poll_survives_one_decision_application_error() -> None:
    """A rejected decision stays durably pending for recovery; the worker keeps running.

    A Muse reassignment failed its stopped-source proof and the uncaught error
    exited the worker, which made the dev supervisor stop the API and web too.
    """
    from forge.worker.main import _poll_invocations

    stop = asyncio.Event()

    class Worker:
        calls = 0

        async def run_once(self, *, stop_event: asyncio.Event) -> object:
            assert stop_event is stop
            self.calls += 1
            if self.calls == 1:
                raise SubscriptionDecisionError("reassignment stopped source proof differs")
            stop.set()
            return uuid4()

    worker = Worker()
    await asyncio.wait_for(_poll_invocations(worker, stop, 0.01), 2)  # type: ignore[arg-type]
    assert worker.calls == 2
