"""Worker registration diagnostics survive separate processes without authority."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.subscription import ReasoningEffort, RouteSpec
from forge.domain.subscription_readiness import (
    ReadinessQuota,
    ReadinessReason,
    ReadinessWarning,
    SubscriptionRouteReadiness,
)
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)

PRIMARY = RouteSpec(
    provider="openai", client="codex", model="gpt-6-astra", effort=ReasoningEffort.LOW
)
READY = SubscriptionRouteReadiness(
    PRIMARY,
    configured=True,
    admitted=True,
    reason=ReadinessReason.READY,
    quota=ReadinessQuota.ELIGIBLE,
)


@pytest.mark.integration
async def test_personal_readiness_survives_reload_with_warnings_and_quota_block(session_factory):
    route = replace(
        PRIMARY, provider="google", client="antigravity_cli", model="gemini-3.8-flash-medium"
    )
    value = SubscriptionRouteReadiness(
        route, True, True, ReadinessReason.OPERATOR_TRUSTED, quota=ReadinessQuota.BLOCKED
    )
    store = SubscriptionRuntimeStatusStore(session_factory)
    assert await store.report(uuid4(), (value,))
    reloaded = (await SubscriptionRuntimeStatusStore(session_factory).status())["workers"][0][
        "routes"
    ][0]
    assert reloaded["reason"] == "operator_trusted"
    assert reloaded["effective_reason"] == "quota_exhausted"
    assert reloaded["evidence"] == []
    assert set(reloaded["warnings"]) == {
        ReadinessWarning.OPERATOR_TRUSTED.value,
        ReadinessWarning.APPROVED_TOOLS_UNPROVED.value,
    }


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
    assert await store.report(old, ())  # diagnostic snapshots refresh without changing authority
    now += timedelta(seconds=10)
    await asyncio.gather(store.report(old, (PRIMARY,)), store.stop(old))
    assert not await store.report(old, (PRIMARY,))
    assert await store.report(new, ())
    reader = SubscriptionRuntimeStatusStore(session_factory, clock=lambda: now)
    rows = {row["worker_instance_id"]: row for row in (await reader.status())["workers"]}
    assert rows[old]["state"] == "stopped" and rows[new]["state"] == "current"
    assert rows[old]["stopped_at"] >= rows[old]["last_seen_at"]
    assert rows[old]["routes"][0]["effective_reason"] == "stale_worker"


@pytest.mark.integration
async def test_v2_readiness_refreshes_and_inactive_workers_never_appear_ready(
    session_factory,
) -> None:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    store = SubscriptionRuntimeStatusStore(session_factory, clock=lambda: now)
    instance = uuid4()

    assert await store.report(instance, (READY,))
    route = (await store.status())["workers"][0]["routes"][0]
    assert (route["schema_version"], route["reason"], route["effective_reason"]) == (
        2,
        "ready",
        "ready",
    )
    assert route["quota"] == "eligible"

    now += timedelta(seconds=46)
    route = (await store.status())["workers"][0]["routes"][0]
    assert route["effective_reason"] == "stale_worker"


@pytest.mark.integration
async def test_conflicting_duplicate_route_snapshots_are_rejected(session_factory) -> None:
    store = SubscriptionRuntimeStatusStore(session_factory)
    conflicting = replace(READY, reason=ReadinessReason.SIGNED_OUT)

    with pytest.raises(ValueError, match="duplicate"):
        await store.report(uuid4(), (READY, conflicting))


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


@pytest.mark.integration
async def test_model_snapshot_is_durable_bounded_and_never_renews_heartbeat(session_factory):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    store = SubscriptionRuntimeStatusStore(session_factory, clock=lambda: now)
    instance = uuid4()
    assert await store.report(instance, ())
    catalog = {
        "provider": "openai",
        "client": "codex_app_server",
        "source": "provider",
        "status": "available",
        "observed_at": now.isoformat(),
        "stale": False,
        "models": [{"id": "gpt-6-sol", "label": "GPT-6 Sol", "efforts": ["medium"]}],
        "message": "Available choices from the installed client; worker configuration is required to run a selected model.",
    }
    assert await store.report_catalogs(instance, [catalog])
    assert (await store.model_catalogs())["catalogs"][0]["status"] == "available"
    now += timedelta(seconds=46)
    assert await store.report_catalogs(instance, [catalog])
    stale = (await store.model_catalogs())["catalogs"][0]
    assert stale["status"] == "unavailable" and stale["stale"]
    assert stale["observed_at"] == catalog["observed_at"]
    with pytest.raises(ValueError):
        await store.report_catalogs(instance, [catalog | {"message": "C:/secret/account"}])
    await store.stop(instance)
    assert not await store.report_catalogs(instance, [catalog])


@pytest.mark.integration
async def test_catalog_selects_live_provider_across_mixed_worker_snapshots(session_factory):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    store = SubscriptionRuntimeStatusStore(session_factory, clock=lambda: now)
    live, stopped, failed = uuid4(), uuid4(), uuid4()
    provider = {
        "provider": "openai",
        "client": "codex_app_server",
        "source": "provider",
        "status": "available",
        "observed_at": now.isoformat(),
        "stale": False,
        "models": [{"id": "gpt-6-sol", "label": "GPT-6 Sol", "efforts": ["medium"]}],
        "message": "Available choices from the installed client; worker configuration is required to run a selected model.",
    }
    for worker in (live, stopped, failed):
        assert await store.report(worker, ())
    assert await store.report_catalogs(live, [provider])
    now += timedelta(seconds=1)
    assert await store.report(stopped, ())
    assert await store.report_catalogs(
        stopped,
        [
            provider
            | {
                "models": [{"id": "old", "label": "Old", "efforts": ["low"]}],
            }
        ],
    )
    await store.stop(stopped)
    now += timedelta(seconds=1)
    assert await store.report(failed, ())
    assert await store.report_catalogs(
        failed,
        [
            provider
            | {
                "status": "unavailable",
                "stale": True,
                "message": "Installed client model metadata is unavailable; showing last known choices.",
                "models": [{"id": "failed", "label": "Failed", "efforts": ["low"]}],
            }
        ],
    )
    chosen = (await store.model_catalogs())["catalogs"][0]
    assert chosen["status"] == "available"
    assert chosen["models"][0]["id"] == "gpt-6-sol"


@pytest.mark.integration
@pytest.mark.parametrize("source", ["configured", "provider"])
@pytest.mark.parametrize("inactive", ["stale", "stopped"])
async def test_metadata_failure_distinguishes_current_from_inactive_worker(
    session_factory, source, inactive
):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    store = SubscriptionRuntimeStatusStore(session_factory, clock=lambda: now)
    worker = uuid4()
    catalog = {
        "provider": "openai",
        "client": "codex_app_server",
        "source": source,
        "status": "unavailable",
        "observed_at": now.isoformat() if source == "provider" else None,
        "stale": True,
        "models": [{"id": "gpt-6-sol", "label": "gpt-6-sol", "efforts": ["medium"]}],
        "message": (
            "Installed client model metadata is unavailable; showing last known choices."
            if source == "provider"
            else "Installed client model metadata is unavailable; showing configured choices."
        ),
    }
    assert await store.report(worker, ())
    assert await store.report_catalogs(worker, [catalog])
    current = (await store.model_catalogs())["catalogs"][0]
    assert current["message"] == catalog["message"]
    if inactive == "stopped":
        await store.stop(worker)
    else:
        now += timedelta(seconds=46)
    stopped = (await store.model_catalogs())["catalogs"][0]
    assert stopped["stale"] and stopped["status"] == "unavailable"
    assert stopped["message"] == (
        "Installed client model choices are unavailable because no current worker is reporting them."
        if source == "provider"
        else "Configured model choices are unavailable because no current worker is reporting them."
    )
