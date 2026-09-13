"""Terminal proof IO completes before the effect settlement transaction opens."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery


@pytest.mark.asyncio
async def test_terminal_verification_finishes_before_atomic_effect_settlement():
    effect_id = uuid4()
    proof = object()
    active = False
    scanned = False
    reconciler = AsyncMock(return_value=True)

    @asynccontextmanager
    async def factory():
        nonlocal active, scanned
        assert not active
        active = True
        candidates = () if scanned else (effect_id,)
        scanned = True
        work = SimpleNamespace(
            subscription=SimpleNamespace(
                interrupted_effect_ids=AsyncMock(return_value=candidates),
                reconcile_interrupted_effect=reconciler,
            ),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )
        try:
            yield work
        finally:
            active = False

    async def verify(value):
        assert value == effect_id and not active
        return proof

    verifier = SimpleNamespace(verify_terminal_effect=AsyncMock(side_effect=verify))
    assert (
        await SubscriptionEffectRecovery(factory, terminal_verifier=verifier).reconcile_all() == 1
    )
    reconciler.assert_awaited_once_with(effect_id, verified_terminal=proof)
