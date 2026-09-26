"""Closed candidate task admission authenticates the persisted read-only contract."""

import pytest
from forge.domain.subscription import SpecialistPurpose
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_scheduler_acceptance import (
    _admit_run,
    _claim,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401
    _route,
)


@pytest.mark.integration
@pytest.mark.parametrize("tamper", [None, "payload", "paths", "readonly", "parent"])
async def test_closed_candidate_readonly_claim_requires_matching_contract(
    session_factory, persisted_run, tamper
):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        reader = await _enqueue(
            work, persisted_run.id, provider="p", worktree="one", parent_id=primary,
            purpose=SpecialistPurpose.PLANNING,
        )
        epoch = await work.scheduler.begin_candidate(persisted_run.id)
        await work.scheduler.close_candidate(persisted_run.id, epoch)
        scheduled = await work.session.scalar(
            select(SubscriptionScheduledTask).where(SubscriptionScheduledTask.task_id == reader)
        )
        if tamper == "payload":
            (await work.session.get(SubscriptionTask, reader)).payload = {"invalid": True}
        elif tamper == "paths":
            scheduled.owned_paths = ["apps"]
        elif tamper == "readonly":
            scheduled.read_only = False
        elif tamper == "parent":
            scheduled.parent_task_id = None
        await work.commit()
    lease = await _claim(session_factory, "closed-reader")
    if tamper is None:
        assert lease is not None and lease.task_id == reader
    else:
        assert lease is None
