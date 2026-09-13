from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery


@pytest.mark.asyncio
async def test_recovery_commits_the_atomic_repository_reconciliation() -> None:
    work = type("Work", (), {})()
    first, second = uuid4(), uuid4()
    work.subscription = type(
        "Subscription",
        (),
        {
            "interrupted_effect_ids": AsyncMock(side_effect=((first, second), ())),
            "reconcile_interrupted_effect": AsyncMock(side_effect=(True, True)),
        },
    )()
    work.commit = AsyncMock()
    work.rollback = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield work

    assert await SubscriptionEffectRecovery(factory).reconcile_all() == 2
    assert work.subscription.reconcile_interrupted_effect.await_count == 2
    assert work.commit.await_count == 2


@pytest.mark.asyncio
async def test_malformed_candidate_does_not_starve_later_valid_effect() -> None:
    malformed, valid = sorted((uuid4(), uuid4()))
    calls = []
    step = 0

    @asynccontextmanager
    async def factory():
        nonlocal step
        work = type("Work", (), {})()
        if step in (0, 3):
            candidates = (malformed, valid) if step == 0 else ()
            work.subscription = type(
                "Subscription", (), {"interrupted_effect_ids": AsyncMock(return_value=candidates)}
            )()
        else:
            effect_id = (malformed, valid)[step - 1]
            work.subscription = type(
                "Subscription",
                (),
                {"reconcile_interrupted_effect": AsyncMock(return_value=effect_id == valid)},
            )()
        work.commit, work.rollback = AsyncMock(), AsyncMock()
        calls.append(work)
        step += 1
        yield work

    assert await SubscriptionEffectRecovery(factory).reconcile_all() == 1
    assert calls[0].rollback.await_count == 1
    assert calls[1].subscription.reconcile_interrupted_effect.await_args.args == (malformed,)
    assert calls[2].subscription.reconcile_interrupted_effect.await_args.args == (valid,)
    assert calls[1].commit.await_count == calls[2].commit.await_count == 1


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_and_same_effect_is_retried_once() -> None:
    effect_id = uuid4()
    settled = False
    fail_commit = True
    rollbacks = 0

    @asynccontextmanager
    async def factory():
        nonlocal settled, fail_commit, rollbacks
        work = type("Work", (), {})()
        is_scan = not hasattr(factory, "last_was_scan") or not factory.last_was_scan
        factory.last_was_scan = is_scan
        if is_scan:
            work.subscription = type(
                "Subscription",
                (),
                {"interrupted_effect_ids": AsyncMock(return_value=() if settled else (effect_id,))},
            )()
        else:
            work.subscription = type(
                "Subscription", (), {"reconcile_interrupted_effect": AsyncMock(return_value=True)}
            )()
        work.rollback = AsyncMock()

        async def commit():
            nonlocal settled, fail_commit
            if fail_commit:
                fail_commit = False
                raise RuntimeError("commit failed")
            settled = True

        work.commit = AsyncMock(side_effect=commit)
        try:
            yield work
        except BaseException:
            rollbacks += 1
            raise

    recovery = SubscriptionEffectRecovery(factory)
    with pytest.raises(RuntimeError, match="commit failed"):
        await recovery.reconcile_all()
    assert settled is False and rollbacks == 1
    assert await recovery.reconcile_all() == 1
    assert settled is True
