"""Candidate transitions serialize with run authority before locking scheduler state."""

import asyncio

import pytest
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _remove_disposable_subscription_rows,
    _route,
)


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["begin", "close"])
async def test_candidate_transition_waits_for_run_authority(
    session_factory, persisted_run, operation
):
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        epoch = (
            await work.scheduler.begin_candidate(persisted_run.id) if operation == "close" else None
        )
        await work.commit()

    async def transition():
        async with factory() as work:
            if operation == "begin":
                await work.scheduler.begin_candidate(persisted_run.id)
            else:
                await work.scheduler.close_candidate(persisted_run.id, epoch)
            await work.commit()

    async with factory() as work:
        await work.runs.get_for_update(persisted_run.id)
        pending = asyncio.create_task(transition())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), 1)
        await work.rollback()
    await asyncio.wait_for(pending, 5)
