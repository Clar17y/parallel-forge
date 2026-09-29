"""Compatibility registrations expire without removing active worker evidence."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from forge.persistence.models.subscription_recovery import SubscriptionRecoveryWorker
from forge.worker import main
from sqlalchemy import select


@pytest.mark.integration
async def test_publication_prunes_expired_workers_and_preserves_live_registrations(
    session_factory, monkeypatch
):
    now = datetime.now(UTC)
    monkeypatch.setattr(main, "datetime", SimpleNamespace(now=lambda _timezone: now))
    async with session_factory() as session:
        session.add_all([
            SubscriptionRecoveryWorker(
                worker_id="expired", contract_version=1, observed_at=now - timedelta(seconds=31)
            ),
            SubscriptionRecoveryWorker(
                worker_id="boundary", contract_version=1, observed_at=now - timedelta(seconds=30)
            ),
            SubscriptionRecoveryWorker(
                worker_id="live", contract_version=1, observed_at=now - timedelta(seconds=5)
            ),
            SubscriptionRecoveryWorker(
                worker_id="live-incompatible", contract_version=0, observed_at=now
            ),
        ])
        await session.commit()

    await asyncio.gather(
        main._publish_recovery_worker(session_factory, "process-a"),
        main._publish_recovery_worker(session_factory, "process-b"),
    )
    await main._publish_recovery_worker(session_factory, "process-a")
    async with session_factory() as session:
        rows = {row.worker_id: row for row in await session.scalars(select(SubscriptionRecoveryWorker))}
        assert set(rows) == {"boundary", "live", "live-incompatible", "process-a", "process-b"}
        assert rows["live-incompatible"].contract_version == 0
        assert rows["process-a"].observed_at == now

    # Repeated process restarts retain only the current freshness window.
    now += timedelta(seconds=31)
    await main._publish_recovery_worker(session_factory, "restarted-process")
    async with session_factory() as session:
        assert list(await session.scalars(select(SubscriptionRecoveryWorker.worker_id))) == [
            "restarted-process"
        ]


@pytest.mark.integration
async def test_expiry_cleanup_rolls_back_with_failed_publication_and_retries(
    session_factory, monkeypatch
):
    async with session_factory() as session:
        session.add(SubscriptionRecoveryWorker(
            worker_id="expired", contract_version=1,
            observed_at=datetime.now(UTC) - timedelta(minutes=1),
        ))
        await session.commit()

    def fail_insert(_model):
        raise ValueError("simulated publication failure after pruning")

    with monkeypatch.context() as patch:
        patch.setattr(main, "insert", fail_insert)
        await main._publish_recovery_worker(session_factory, "current")

    async with session_factory() as session:
        assert await session.get(SubscriptionRecoveryWorker, "expired") is not None
        assert await session.get(SubscriptionRecoveryWorker, "current") is None

    await main._publish_recovery_worker(session_factory, "current")
    async with session_factory() as session:
        assert await session.get(SubscriptionRecoveryWorker, "expired") is None
        assert await session.get(SubscriptionRecoveryWorker, "current") is not None
