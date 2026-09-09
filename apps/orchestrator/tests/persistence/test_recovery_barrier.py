"""Database-wide recovery excludes command admission until verified completion."""

import asyncio
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandLane
from forge.persistence.models.recovery import RecoveryBarrier
from forge.persistence.repositories.recovery import PostgresRecoveryBarrier, RecoveryBarrierLost
from sqlalchemy import select, text


@pytest.mark.integration
@pytest.mark.parametrize("lane", [CommandLane.NORMAL, CommandLane.CONTROL])
async def test_expired_recovery_stays_closed_until_successful_takeover(
    session_factory, command_repository, persisted_run, lane
):
    barrier = PostgresRecoveryBarrier(session_factory)
    source = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="pause" if lane is CommandLane.CONTROL else "start_planning",
        idempotency_key="recovery-blocked",
        payload={},
        actor_id=uuid4(),
    )
    first = await barrier.acquire(owner_id=uuid4(), lease_seconds=30)
    assert first is not None
    assert await barrier.acquire(owner_id=uuid4(), lease_seconds=30) is None
    assert (
        await command_repository.claim_next(worker_id="existing", lease_seconds=30, lane=lane)
        is None
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE recovery_barrier SET expires_at = clock_timestamp() - interval '1 second'")
        )
    assert (
        await command_repository.claim_next(worker_id="existing", lease_seconds=30, lane=lane)
        is None
    )
    second = await barrier.acquire(owner_id=uuid4(), lease_seconds=30)
    assert second is not None and second.generation == first.generation + 1
    with pytest.raises(RecoveryBarrierLost):
        await barrier.finish(first)
    with pytest.raises(RecoveryBarrierLost):
        await barrier.renew(first, lease_seconds=30)
    await barrier.abandon(first)
    assert (
        await command_repository.claim_next(worker_id="existing", lease_seconds=30, lane=lane)
        is None
    )
    await barrier.renew(second, lease_seconds=30)
    await barrier.finish(second)
    assert (
        await command_repository.claim_next(worker_id="existing", lease_seconds=30, lane=lane)
    ).id == source.id


@pytest.mark.integration
async def test_recovery_acquisition_is_singleton_and_abandonment_stays_closed(session_factory):
    barrier = PostgresRecoveryBarrier(session_factory)
    acquired = await asyncio.gather(
        *(barrier.acquire(owner_id=uuid4(), lease_seconds=30) for _ in range(2))
    )
    owners = [lease for lease in acquired if lease is not None]
    assert len(owners) == 1
    await barrier.abandon(owners[0])
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT required, owner_id, expires_at FROM recovery_barrier")
            )
        ).one()
        assert row.required and row.owner_id is None and row.expires_at is None
    replacement = await barrier.acquire(owner_id=uuid4(), lease_seconds=30)
    assert replacement is not None
    await barrier.finish(replacement)


@pytest.mark.integration
async def test_claim_waits_for_recovery_admission_transaction(
    session_factory, command_repository, persisted_run
):
    await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="barrier-race",
        payload={},
    )
    async with session_factory() as session, session.begin():
        row = await session.scalar(select(RecoveryBarrier).with_for_update())
        row.required = True
        await session.flush()
        claim = asyncio.create_task(
            command_repository.claim_next(worker_id="racing", lease_seconds=30)
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(claim), 0.1)
    assert await asyncio.wait_for(claim, 2) is None


@pytest.mark.integration
async def test_recovery_waits_for_existing_claim_transaction(session_factory):
    barrier = PostgresRecoveryBarrier(session_factory)
    async with session_factory() as session, session.begin():
        await session.scalar(select(RecoveryBarrier).with_for_update(read=True))
        acquiring = asyncio.create_task(barrier.acquire(owner_id=uuid4(), lease_seconds=30))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(acquiring), 0.1)
    lease = await asyncio.wait_for(acquiring, 2)
    assert lease is not None
    await barrier.finish(lease)
