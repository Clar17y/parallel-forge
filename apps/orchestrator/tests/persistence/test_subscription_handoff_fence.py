"""Snapshot observers fence controlled writes without retaining a transaction."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.domain.scheduling import ScheduleTask
from forge.domain.subscription import is_read_only
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
from forge.persistence.repositories.scheduling import SchedulingConflict
from forge.persistence.repositories.subscription_handoff_fence import (
    PostgresSubscriptionHandoffFence,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_handoff_proposal import handoff_case


@pytest.mark.integration
async def test_handoff_observation_is_exclusive_and_exactly_released(session_factory, tmp_path):
    factory, _, child, handoff = await handoff_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    token = uuid4()
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, token)
    assert observation.proposal.handoff == handoff and observation.token == token
    assert (
        await application.begin_handoff_observation(child.attempt.attempt_id, token) == observation
    )
    with pytest.raises(SubscriptionDecisionError):
        await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    assert await application.release_handoff_observation(observation)
    assert not await application.release_handoff_observation(observation)
    fresh = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    assert fresh.token != token
    assert not await application.release_handoff_observation(observation)
    assert await application.release_handoff_observation(fresh)


async def sibling(factory, child, *, read_only=False, worktree=None):
    task = replace(
        child.task,
        task_id=uuid4(),
        owned_paths=("apps/other",),
    )
    async with factory() as work:
        proposal = await work.subscription_decisions.handoff_proposal(child.attempt.attempt_id)
        await work.subscription.create_task(task, idempotency_key=f"sibling:{task.task_id}")
        await work.scheduler.enqueue(
            ScheduleTask(
                run_id=task.run_id,
                task_id=task.task_id,
                parent_task_id=task.parent_task_id,
                worktree_id=worktree or proposal.worktree.identity.worktree_name,
                owned_paths=task.owned_paths,
                read_only=is_read_only(task.purpose),
                max_repairs=task.max_repairs,
            )
        )
        if read_only:
            # Exercise the scheduler's read-only/global-effect branch directly;
            # this fixture's provider profile has no verification route.
            row = await work.session.get(SubscriptionScheduledTask, task.task_id)
            row.read_only = True
        await work.commit()
    return task


async def claim(factory):
    async with factory() as work:
        lease = await work.scheduler.claim_ready("sibling", timedelta(seconds=60))
        await work.commit()
        return lease


@pytest.mark.integration
@pytest.mark.parametrize("read_only", [False, True])
async def test_observation_blocks_existing_sibling_mutation_and_named_check(
    session_factory, tmp_path, read_only
):
    factory, _, child, _ = await handoff_case(session_factory, tmp_path)
    task = await sibling(factory, child, read_only=read_only)
    lease = await claim(factory)
    assert lease.task_id == task.task_id
    application = SubscriptionDecisionApplication(factory)
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    async with factory() as work:
        with pytest.raises(SchedulingConflict):
            await work.scheduler.admit_effect(
                lease,
                uuid4(),
                whole_worktree_exclusive=read_only,
                owned_paths=() if read_only else task.owned_paths,
            )
    assert await application.release_handoff_observation(observation)


@pytest.mark.integration
async def test_observation_waits_for_outstanding_sibling_effect(session_factory, tmp_path):
    factory, _, child, _ = await handoff_case(session_factory, tmp_path)
    task = await sibling(factory, child)
    lease = await claim(factory)
    async with factory() as work:
        effect = await work.scheduler.admit_effect(lease, uuid4(), owned_paths=task.owned_paths)
        await work.commit()
    application = SubscriptionDecisionApplication(factory)
    with pytest.raises(SubscriptionDecisionError, match="outstanding"):
        await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    async with factory() as work:
        await work.scheduler.settle_effect(effect, accepted=True)
        await work.commit()
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    assert await application.release_handoff_observation(observation)


@pytest.mark.integration
async def test_expired_observer_cannot_apply_or_release_replacement(session_factory, tmp_path):
    factory, _, child, _ = await handoff_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    old = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    async with factory() as work:
        assert await PostgresSubscriptionHandoffFence(work.session).current(old)
        row = await work.session.get(
            SubscriptionHandoffFence, old.proposal.worktree.identity.worktree_name
        )
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    async with factory() as work:
        assert not await PostgresSubscriptionHandoffFence(work.session).current(old)
    with pytest.raises(SubscriptionDecisionError, match="fresh token"):
        await application.begin_handoff_observation(child.attempt.attempt_id, old.token)
    fresh = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    assert not await application.release_handoff_observation(old)
    async with factory() as work:
        assert not await PostgresSubscriptionHandoffFence(work.session).current(old)
        assert await PostgresSubscriptionHandoffFence(work.session).current(fresh)
    assert await application.release_handoff_observation(fresh)


@pytest.mark.integration
async def test_observation_only_blocks_its_worktree_admission(session_factory, tmp_path):
    factory, _, child, _ = await handoff_case(session_factory, tmp_path)
    await sibling(factory, child)
    other = await sibling(factory, child, worktree="independent-test-tree")
    application = SubscriptionDecisionApplication(factory)
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    lease = await claim(factory)
    assert lease.task_id == other.task_id
    assert await claim(factory) is None
    assert await application.release_handoff_observation(observation)


@pytest.mark.integration
async def test_migration_refuses_to_drop_active_observation(session_factory, tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    factory, _, child, _ = await handoff_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260911_0016_subscription_handoff_fence.py"
    )
    spec = importlib.util.spec_from_file_location("handoff_fence_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()

    async with factory() as work:
        connection = await work.session.connection()
        with pytest.raises(RuntimeError, match="must not be discarded"):
            await connection.run_sync(downgrade)
    assert await application.release_handoff_observation(observation)
