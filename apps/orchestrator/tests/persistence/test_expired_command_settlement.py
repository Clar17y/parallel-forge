"""Expired delivery acknowledgment is conditional on its full observed identity."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.command import CommandStatus
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "drift", [None, "active", "owner", "attempt", "payload", "actor", "version", "type"]
)
async def test_complete_expired_delivery_requires_exact_identity(
    persisted_run, session_factory, drift
):
    commands = PostgresCommandRepository(session_factory)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.commands.enqueue(
            run_id=persisted_run.id,
            command_type="monitor_pr",
            idempotency_key="observed-poll",
            payload={"poll": 1},
            expected_run_version=persisted_run.version,
            actor_id=uuid4(),
        )
        await work.commit()
    leased = await commands.claim_next(worker_id="observer", lease_seconds=120)
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(RunCommand, leased.id)
        row.lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=120 if drift == "active" else -1
        )
        await work.commit()
    observed = await commands.get(leased.id)
    changes = {
        "owner": {"lease_owner": "other"},
        "attempt": {"attempt": observed.attempt + 1},
        "payload": {"payload": {"poll": 2}},
        "actor": {"actor_id": uuid4()},
        "version": {"expected_run_version": observed.expected_run_version + 1},
        "type": {"command_type": "merge_pr"},
    }
    candidate = replace(observed, **changes.get(drift, {}))
    async with PostgresUnitOfWork(session_factory) as work:
        settled = await work.commands.complete_expired_observed_lease(candidate)
        assert (settled is not None) == (drift is None)
        await work.commit()
    current = await commands.get(leased.id)
    assert current.status is (CommandStatus.COMPLETED if drift is None else CommandStatus.LEASED)
    if drift is None:
        assert current.payload == observed.payload
        assert current.lease_owner is None and current.lease_expires_at is None
        async with PostgresUnitOfWork(session_factory) as work:
            assert await work.commands.complete_expired_observed_lease(observed) is None
