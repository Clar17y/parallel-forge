"""Whole-worktree effects retain durable exclusion through settlement."""

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge.domain.subscription import SpecialistPurpose
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.repositories.scheduling import SchedulingConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select, text, update
from test_scheduler_acceptance import (
    _admit_run,
    _claim,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401 - autouse disposable cleanup
    _route,
)


async def _owner(session_factory, run):
    async with PostgresUnitOfWork(session_factory) as work:
        parent = await _admit_run(work, run, (_route("p"), _route("p")))
        task = await _enqueue(
            work, run.id, provider="p", worktree="one", parent_id=parent, paths=("apps/a",)
        )
        await work.commit()
    lease = await _claim(session_factory, "exclusive-owner")
    assert lease is not None and lease.task_id == task
    return parent, lease


async def _exclusive(session_factory, lease):
    async with PostgresUnitOfWork(session_factory) as work:
        effect = await work.scheduler.admit_effect(lease, uuid4(), whole_worktree_exclusive=True)
        await work.commit()
        return effect


@pytest.mark.integration
@pytest.mark.parametrize(
    "state,expired", [("admitted", False), ("reconciling", False), ("admitted", True)]
)
async def test_exclusive_effect_blocks_later_disjoint_writer_but_other_worktree_progresses(
    session_factory, persisted_run, state, expired
):
    parent, lease = await _owner(session_factory, persisted_run)
    effect = await _exclusive(session_factory, lease)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.session.execute(
            update(SubscriptionScheduledEffect)
            .where(SubscriptionScheduledEffect.id == effect.effect_id)
            .values(state=state)
        )
        if expired:
            await work.session.execute(
                update(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.task_id == lease.task_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        blocked = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=parent,
            paths=("apps/b",),
        )
        other = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="two",
            parent_id=parent,
            paths=("apps/c",),
        )
        await work.commit()
    claim = await _claim(session_factory, "other")
    assert claim is not None and claim.task_id == other
    assert await _claim(session_factory, "blocked") is None
    async with session_factory() as session:
        row = await session.scalar(
            select(SubscriptionScheduledTask).where(SubscriptionScheduledTask.task_id == blocked)
        )
        assert row.state == "queued"


@pytest.mark.integration
async def test_same_task_mutation_denied_and_exact_exclusive_replay_preserved(
    session_factory, persisted_run
):
    _, lease = await _owner(session_factory, persisted_run)
    effect = await _exclusive(session_factory, lease)
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.scheduler.admit_effect(
                lease, effect.effect_id, whole_worktree_exclusive=True
            )
            == effect
        )
        await work.commit()
    for effect_id in (uuid4(), effect.effect_id):
        async with PostgresUnitOfWork(session_factory) as work:
            with pytest.raises(SchedulingConflict):
                await work.scheduler.admit_effect(lease, effect_id, owned_paths=("apps/a/file",))
            await work.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("accepted", [False, True])
async def test_terminal_exclusive_effect_releases_disjoint_writer(
    session_factory, persisted_run, accepted
):
    parent, lease = await _owner(session_factory, persisted_run)
    effect = await _exclusive(session_factory, lease)
    async with PostgresUnitOfWork(session_factory) as work:
        waiting = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=parent,
            paths=("apps/b",),
        )
        await work.commit()
    assert await _claim(session_factory, "blocked") is None
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.scheduler.settle_effect(effect, accepted=accepted) is accepted
        await work.commit()
    claim = await _claim(session_factory, "released")
    assert claim is not None and claim.task_id == waiting


@pytest.mark.integration
async def test_exclusive_admission_and_disjoint_claim_linearize(session_factory, persisted_run):
    parent, lease = await _owner(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        waiting = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=parent,
            paths=("apps/b",),
        )
        await work.commit()

    async def admit():
        try:
            return await _exclusive(session_factory, lease)
        except SchedulingConflict:
            return None

    effect, claim = await asyncio.gather(admit(), _claim(session_factory, "racer"))
    assert (effect is not None) != (claim is not None)
    if claim is not None:
        assert claim.task_id == waiting
    else:
        assert await _claim(session_factory, "still-blocked") is None


@pytest.mark.integration
async def test_migration_backfills_active_effects_and_refuses_to_discard_live_barriers(
    session_factory, persisted_run
):
    _, lease = await _owner(session_factory, persisted_run)
    ids = {state: uuid4() for state in ("admitted", "reconciling", "settled", "rejected")}
    async with PostgresUnitOfWork(session_factory) as work:
        for state, effect_id in ids.items():
            await work.scheduler.admit_effect(lease, effect_id)
            await work.session.execute(
                update(SubscriptionScheduledEffect)
                .where(SubscriptionScheduledEffect.id == effect_id)
                .values(state=state)
            )
        await work.commit()
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260910_0011_exclusive_effect_barrier.py"
    )
    spec = importlib.util.spec_from_file_location("exclusive_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    async with session_factory() as session, session.begin():
        await session.execute(
            text("ALTER TABLE subscription_scheduled_effects DROP COLUMN whole_worktree_exclusive")
        )
        connection = await session.connection()
        await connection.run_sync(upgrade)
        rows = dict(
            (
                await session.execute(
                    text(
                        "SELECT state, whole_worktree_exclusive FROM subscription_scheduled_effects"
                    )
                )
            ).all()
        )
        assert rows == {"admitted": True, "reconciling": True, "settled": False, "rejected": False}
        with pytest.raises(RuntimeError, match="unsettled"):
            await connection.run_sync(downgrade)


@pytest.mark.integration
async def test_exclusive_admission_waits_for_same_task_inflight_effect(
    session_factory, persisted_run
):
    _, lease = await _owner(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.admit_effect(lease, uuid4(), owned_paths=("apps/a/file",))
        await work.commit()
    with pytest.raises(SchedulingConflict):
        await _exclusive(session_factory, lease)


@pytest.mark.integration
async def test_read_only_task_can_progress_during_exclusive_effect(session_factory, persisted_run):
    parent, lease = await _owner(session_factory, persisted_run)
    await _exclusive(session_factory, lease)
    async with PostgresUnitOfWork(session_factory) as work:
        reader = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=parent,
            purpose=SpecialistPurpose.PLANNING,
        )
        await work.commit()
    claim = await _claim(session_factory, "reader")
    assert claim is not None and claim.task_id == reader
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.admit_effect(claim, uuid4())
        await work.commit()
