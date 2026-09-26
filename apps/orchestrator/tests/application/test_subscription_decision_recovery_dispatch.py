"""Recovery dispatch pages do not hold transactions across application IO."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from forge.application.ports.subscription_decisions import (
    PendingDecisionKind,
    PendingSubscriptionDecision,
)
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery


async def test_recovery_pages_past_deferred_and_unsupported_sources():
    candidates = tuple(
        PendingSubscriptionDecision(UUID(int=index), kind)
        for index, kind in enumerate(
            (
                PendingDecisionKind.PLAN,
                PendingDecisionKind.PLAN,
                PendingDecisionKind.WAIT,
                PendingDecisionKind.UNSUPPORTED,
            ),
            1,
        )
    )
    active, cursors, applied = [], [], []

    async def pending(cursor, limit):
        assert active and limit == 2
        cursors.append(cursor)
        return tuple(item for item in candidates if cursor is None or item.attempt_id > cursor)[
            :limit
        ]

    async def rollback():
        pass

    @asynccontextmanager
    async def factory():
        active.append(True)
        try:
            yield SimpleNamespace(
                subscription_decisions=SimpleNamespace(pending_applications=pending),
                rollback=rollback,
            )
        finally:
            active.pop()

    async def apply(attempt_id):
        assert not active
        if attempt_id.int == 2:
            raise ValueError("untrusted private source text")
        applied.append(attempt_id)

    recovery = SubscriptionDecisionRecovery(factory, object(), page_size=2)
    recovery._plans = SimpleNamespace(request_settled=apply)
    recovery._decisions = SimpleNamespace(apply_wait=apply)
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (2, 1, 1)
    assert cursors == [None, UUID(int=2), UUID(int=4)]
    assert applied == [UUID(int=1), UUID(int=3)]
    assert "private" not in repr(report)


async def test_recovery_does_not_swallow_cancellation():
    async def pending(*args):
        return (PendingSubscriptionDecision(UUID(int=1), PendingDecisionKind.PLAN),)

    async def rollback():
        pass

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(
            subscription_decisions=SimpleNamespace(pending_applications=pending), rollback=rollback
        )

    async def cancel(*args):
        raise asyncio.CancelledError

    recovery = SubscriptionDecisionRecovery(factory, object())
    recovery._plans = SimpleNamespace(request_settled=cancel)
    with pytest.raises(asyncio.CancelledError):
        await recovery.reconcile_all()


async def test_recovery_dispatches_handoff_only_when_observer_is_configured():
    identity, applied = UUID(int=9), []

    async def pending(cursor, limit):
        return (
            () if cursor else (PendingSubscriptionDecision(identity, PendingDecisionKind.HANDOFF),)
        )

    async def rollback():
        pass

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(
            subscription_decisions=SimpleNamespace(pending_applications=pending), rollback=rollback
        )

    async def apply(attempt):
        applied.append(attempt)

    unavailable = await SubscriptionDecisionRecovery(factory, object()).reconcile_all()
    assert unavailable.unsupported == 1 and not applied
    available = await SubscriptionDecisionRecovery(
        factory, object(), handoffs=SimpleNamespace(apply=apply)
    ).reconcile_all()
    assert available.applied == 1 and applied == [identity]


@pytest.mark.parametrize("size", [0, 101, True, 1.5])
def test_recovery_page_bound_is_explicit(size):
    with pytest.raises(ValueError, match="page size"):
        SubscriptionDecisionRecovery(lambda: None, object(), page_size=size)
