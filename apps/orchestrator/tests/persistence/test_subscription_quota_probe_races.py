"""Probe recovery must share the durable lease and client-launch fence."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription_quota import QuotaPolicy, QuotaPoolKey
from forge.persistence.models.subscription_quota import SubscriptionQuotaAdmission
from forge.persistence.repositories import subscription
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_quota import _factory, _report, _seed
from test_subscription_usage import _reservation


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["renew", "launch_intent"])
async def test_recovery_preserves_probe_during_uncommitted_launch_authority(
    session_factory, persisted_run, monkeypatch, mutation
):
    clock = [datetime.now(UTC)]

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(subscription, "datetime", ControlledDatetime)
    factory = _factory(
        session_factory, clock, policy=QuotaPolicy(unknown_reset_cooldown_seconds=60)
    )
    await _seed(factory, persisted_run, ("p", "p"))
    key = QuotaPoolKey("p", "local", "subscription-allowance_only")
    await _report(factory, key, clock[0], reset=clock[0] + timedelta(seconds=1))
    clock[0] += timedelta(seconds=1)
    executor = SubscriptionDecisionExecutor(factory)
    probe = await executor.admit_next("probe-owner", _reservation())
    assert probe is not None

    async def assert_original_probe():
        async with factory() as work:
            status = await work.quota.status(key)
            admission = await work.session.get(SubscriptionQuotaAdmission, probe.attempt.attempt_id)
            assert status.status == "blocked"
            assert status.probe_attempt_id == probe.attempt.attempt_id
            assert admission.finished_at is None

    # Each real mutation has validated the lease and flushed its change, but
    # another PostgreSQL session cannot see it until this transaction commits.
    clock[0] = probe.lease.expires_at - timedelta(seconds=1)
    async with factory() as mutating:
        if mutation == "renew":
            await mutating.scheduler.renew(probe.lease, timedelta(seconds=120))
        else:
            await mutating.subscription.launch_intent(
                probe.attempt.attempt_id, "probe-launch", worker_identity=probe.lease.owner
            )
        clock[0] = probe.lease.expires_at + timedelta(seconds=1)
        # A pool-to-run wait would deadlock with ordinary run-to-pool settlement.
        # The competing claim must skip the busy run and preserve the probe.
        assert await asyncio.wait_for(executor.admit_next("other-run", _reservation()), 10) is None
        await assert_original_probe()
        await mutating.commit()

    # Even after a full cooldown, a fresh worker cannot replace a renewed
    # attempt or a client whose committed launch intent has no stopped proof.
    clock[0] += timedelta(seconds=61)
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("restarted", _reservation()) is None
    )
    await assert_original_probe()
