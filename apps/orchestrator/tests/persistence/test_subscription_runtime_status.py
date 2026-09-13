"""Worker registration diagnostics survive separate processes without authority."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.subscription import ReasoningEffort, RouteSpec
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)

PRIMARY = RouteSpec(
    provider="openai", client="codex", model="gpt-6-astra", effort=ReasoningEffort.LOW
)


@pytest.mark.integration
async def test_recreated_status_reader_distinguishes_empty_current_and_stale_workers(
    session_factory,
):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    clock = lambda: now
    writer = SubscriptionRuntimeStatusStore(session_factory, clock=clock)
    reader = SubscriptionRuntimeStatusStore(session_factory, clock=clock)
    assert (await reader.status())["workers"] == []
    first, second = uuid4(), uuid4()
    assert await writer.report(first, ())
    assert await writer.report(second, (PRIMARY,))
    value = await reader.status()
    assert value["observed_at"] == now and not value["has_more"]
    rows = {row["worker_instance_id"]: row for row in value["workers"]}
    assert rows[first]["state"] == rows[second]["state"] == "current"
    assert rows[first]["routes"] == []
    assert rows[second]["routes"][0]["model"] == "gpt-6-astra"
    now += timedelta(seconds=value["fresh_for_seconds"])
    assert all(row["state"] == "stale" for row in (await reader.status())["workers"])
    assert await writer.report(second, (PRIMARY,))
    rows = {row["worker_instance_id"]: row for row in (await reader.status())["workers"]}
    assert rows[first]["state"] == "stale" and rows[second]["state"] == "current"


@pytest.mark.integration
async def test_stop_racing_renewal_is_sticky_and_new_process_keeps_its_own_inventory(
    session_factory,
):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    store = SubscriptionRuntimeStatusStore(session_factory, clock=lambda: now)
    old, new = uuid4(), uuid4()
    assert all(await asyncio.gather(*(store.report(old, (PRIMARY,)) for _ in range(2))))
    assert await store.report(new, ())
    assert not await store.report(old, ())  # same process cannot change its frozen registry
    now += timedelta(seconds=10)
    await asyncio.gather(store.report(old, (PRIMARY,)), store.stop(old))
    assert not await store.report(old, (PRIMARY,))
    assert await store.report(new, ())
    reader = SubscriptionRuntimeStatusStore(session_factory, clock=lambda: now)
    rows = {row["worker_instance_id"]: row for row in (await reader.status())["workers"]}
    assert rows[old]["state"] == "stopped" and rows[new]["state"] == "current"
    assert rows[old]["stopped_at"] >= rows[old]["last_seen_at"]


@pytest.mark.integration
async def test_old_renewal_cannot_move_freshness_backwards_and_future_time_is_unknown(
    session_factory,
):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    store = SubscriptionRuntimeStatusStore(session_factory, clock=lambda: now)
    instance = uuid4()
    assert await store.report(instance, (PRIMARY,))
    later = now + timedelta(seconds=10)
    assert await SubscriptionRuntimeStatusStore(session_factory, clock=lambda: later).report(
        instance, (PRIMARY,)
    )
    assert await store.report(instance, (PRIMARY,))
    row = (await store.status())["workers"][0]
    assert row["last_seen_at"] == later and row["state"] == "stale"


@pytest.mark.integration
async def test_status_pagination_is_explicit_and_invalid_secrets_never_persist(session_factory):
    store = SubscriptionRuntimeStatusStore(session_factory)
    for _ in range(3):
        assert await store.report(uuid4(), ())
    first = await store.status(limit=2)
    second = await store.status(offset=2, limit=2)
    assert first["has_more"] and not second["has_more"]
    assert len({row["worker_instance_id"] for row in first["workers"] + second["workers"]}) == 3
    with pytest.raises(ValueError, match="credential"):
        await store.report(uuid4(), (replace(PRIMARY, model="password=fixture-secret"),))
    for kwargs in ({"limit": 0}, {"offset": -1}, {"limit": True}, {"offset": 1_000_001}):
        with pytest.raises(ValueError, match="bounds"):
            await store.status(**kwargs)
    assert len((await store.status())["workers"]) == 3


@pytest.mark.integration
async def test_stop_before_initial_report_retains_tombstone(session_factory):
    store = SubscriptionRuntimeStatusStore(session_factory)
    instance = uuid4()
    await store.stop(instance)
    assert not await store.report(instance, (PRIMARY,))
    assert (await store.status())["workers"][0]["state"] == "stopped"
