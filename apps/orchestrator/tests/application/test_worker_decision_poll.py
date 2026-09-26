"""Periodic decision retries are sequential, stoppable, and fail visibly."""

import asyncio
from types import SimpleNamespace

import pytest
from forge.application.services.subscription_decision_recovery import (
    SubscriptionDecisionRecoveryReport,
)
from forge.worker import main


async def test_decision_poll_retries_deferred_source_without_overlap():
    stop = asyncio.Event()
    calls, active = 0, 0

    async def reconcile():
        nonlocal calls, active
        active += 1
        assert active == 1
        calls += 1
        await asyncio.sleep(0)
        active -= 1
        if calls == 2:
            stop.set()
            return SubscriptionDecisionRecoveryReport(applied=1)
        return SubscriptionDecisionRecoveryReport(deferred=1)

    await asyncio.wait_for(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), stop, 0.01), 1
    )
    assert calls == 2


async def test_decision_poll_stops_without_another_scan():
    stop = asyncio.Event()

    async def forbidden():
        pytest.fail("stopped polling must not scan")

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=forbidden), stop, 60)
    )
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(polling, 1)


async def test_decision_scan_failure_propagates():
    async def fail():
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await main._poll_decisions(SimpleNamespace(reconcile_all=fail), asyncio.Event(), 0.001)


async def test_decision_poll_cancellation_awaits_scan_cleanup():
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def reconcile():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), asyncio.Event(), 0.001)
    )
    await asyncio.wait_for(entered.wait(), 1)
    polling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await polling
    assert cleaned.is_set()


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True, 61])
async def test_decision_poll_interval_is_finite_and_bounded(interval):
    with pytest.raises(ValueError, match="decision retry"):
        await main._poll_decisions(object(), asyncio.Event(), interval)


async def test_stop_during_decision_scan_cancels_and_awaits_cleanup():
    stop, entered, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def reconcile():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), stop, 0.001)
    )
    await asyncio.wait_for(entered.wait(), 1)
    stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(polling), 0.1)
    finally:
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)
    assert cleaned.is_set()


async def test_repeated_cancellation_preserves_scan_cleanup_owner():
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def reconcile():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), asyncio.Event(), 0.001)
    )
    await asyncio.wait_for(entered.wait(), 1)
    polling.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    polling.cancel()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(polling), 0.02)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await polling
