"""Lease ownership fences around durable planning settlement."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from forge.application.services.planning import PlanningRecoveryRequired
from forge.application.services.worker import Worker
from forge.domain.command import CommandStatus
from forge.persistence.models import AgentExecution, Artifact, ArtifactLineage, Run, RunCommand
from forge.persistence.repositories.commands import CommandLeaseError, PostgresCommandRepository
from sqlalchemy import select, update

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest_asyncio.fixture
async def session_factory(migrated_database_url):
    from forge.persistence.database import create_engine, create_session_factory

    engine = create_engine(migrated_database_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _reclaim(command_repository, session_factory, command_id):
    async with session_factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == command_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    reclaimed = await command_repository.claim_next(worker_id="worker-b", lease_seconds=30)
    assert reclaimed is not None and reclaimed.id == command_id
    return reclaimed


async def test_reclaimed_delivery_rejects_stale_finalization_fence(
    command_repository, persisted_run, session_factory
) -> None:
    queued = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="planning:lease-fence",
        payload={},
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    stale = await command_repository.claim_next(worker_id="worker-a", lease_seconds=30)
    assert stale is not None
    reclaimed = await _reclaim(command_repository, session_factory, queued.id)
    assert reclaimed.attempt == stale.attempt + 1

    from forge.persistence.unit_of_work import PostgresUnitOfWork

    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(CommandLeaseError):
            await work.commands.assert_current_lease(stale)


async def test_renewal_loss_cancels_active_handler_without_terminal_failure(
    command_repository, persisted_run, session_factory, monkeypatch
) -> None:
    queued = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="planning:renewal-loss",
        payload={},
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_handler(_command, _work) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def renewal_failure(*_args, **_kwargs):
        raise CommandLeaseError("forced renewal loss")

    monkeypatch.setattr(command_repository, "renew", renewal_failure)
    worker = Worker(
        command_repository,
        session_factory,
        handlers={"start_planning": blocked_handler},
        worker_id="worker-a",
        lease_seconds=1,
    )
    task = asyncio.create_task(worker.tick())
    await asyncio.wait_for(started.wait(), timeout=1)
    assert await asyncio.wait_for(task, timeout=2) is False
    assert cancelled.is_set()
    stored = await command_repository.get(queued.id)
    assert stored.status is CommandStatus.LEASED
    assert stored.lease_owner == "worker-a"


async def test_recovery_required_reclaim_is_not_terminally_failed(
    command_repository, persisted_run, session_factory
) -> None:
    queued = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="planning:recovery-lease",
        payload={},
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    first = await command_repository.claim_next(worker_id="worker-a", lease_seconds=30)
    assert first is not None
    reclaimed = await _reclaim(command_repository, session_factory, queued.id)

    async def recovery_handler(_command, _work) -> None:
        raise PlanningRecoveryRequired

    worker = Worker(
        command_repository,
        session_factory,
        handlers={"start_planning": recovery_handler},
        worker_id="worker-b",
        lease_seconds=30,
    )
    assert reclaimed.status is CommandStatus.LEASED
    # Simulate the recovery delivery's next retry without introducing a new
    # owner; it must stay leased instead of being terminally failed.
    async with session_factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == queued.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await worker.tick() is False
    stored = await command_repository.get(queued.id)
    assert stored.status is CommandStatus.LEASED
    assert stored.lease_owner == "worker-b"


@pytest.mark.parametrize("replacement_owner", ["worker-b", "worker-a"])
@pytest.mark.parametrize("invalid_output", [False, True])
async def test_two_workers_fence_delayed_provider_result_after_lease_reclaim(
    tmp_path, session_factory, monkeypatch, replacement_owner, invalid_output
):
    from test_planning_failed_usage import _build_case

    case = await _build_case(tmp_path, session_factory, fail_invalid=invalid_output)
    original_gateway = case.gateway
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class DelayedPlanner:
        async def execute(self, request):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Model an external request that has received cancellation but
                # still delivers its result after another worker reclaimed it.
                cancelled.set()
                await release.wait()
            return await original_gateway.execute(request)

    case.service._gateway = DelayedPlanner()
    commands_a = PostgresCommandRepository(session_factory)
    commands_b = PostgresCommandRepository(session_factory)

    async def lose_renewal(*_args, **_kwargs):
        await started.wait()
        raise CommandLeaseError("injected renewal failure")

    monkeypatch.setattr(commands_a, "renew", lose_renewal)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == case.command.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    worker_a = Worker(
        commands_a,
        session_factory,
        handlers={"start_planning": case.service.execute},
        worker_id="worker-a",
        lease_seconds=1,
    )
    worker_b = Worker(
        commands_b,
        session_factory,
        handlers={"start_planning": case.service.execute},
        worker_id=replacement_owner,
        lease_seconds=30,
    )
    first = asyncio.create_task(worker_a.tick())
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(cancelled.wait(), 3)
        async with session_factory() as session, session.begin():
            await session.execute(
                update(RunCommand)
                .where(RunCommand.id == case.command.id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        assert await asyncio.wait_for(worker_b.tick(), 3) is False
        reclaimed = await commands_b.get(case.command.id)
        assert reclaimed.status is CommandStatus.LEASED
        assert reclaimed.lease_owner == replacement_owner
        release.set()
        assert await asyncio.wait_for(first, 3) is False
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.wait_for(worker_a.drain(), 3)
    assert len(original_gateway.requests) == 1
    async with session_factory() as session:
        run = await session.get(Run, case.run_id)
        command = await session.get(RunCommand, case.command.id)
        executions = (
            await session.scalars(
                select(AgentExecution).where(AgentExecution.run_id == case.run_id)
            )
        ).all()
        assert run.state == "PLANNING" and run.pending_evidence_digest is None
        assert command.status == "LEASED" and command.lease_owner == replacement_owner
        assert len(executions) == 1 and executions[0].status == "RUNNING"
        observation = await session.scalar(
            select(Artifact)
            .join(ArtifactLineage, ArtifactLineage.artifact_id == Artifact.id)
            .where(
                ArtifactLineage.run_id == case.run_id,
                ArtifactLineage.producer_kind == "planning_late_usage",
                ArtifactLineage.producer_id == executions[0].id,
            )
        )
        assert observation is not None
        receipt = json.loads(await case.artifact_store.open_bytes(observation.digest))
        assert receipt["command_id"] == str(case.command.id)
        assert receipt["execution_id"] == str(executions[0].id)
        assert receipt["usage"]["input_tokens"] > 0
        assert len(receipt["attempts"]) == 2


async def test_reclaim_during_context_selection_prevents_provider_admission(
    tmp_path, session_factory, monkeypatch
):
    from forge.application.ports.commands import CommandLeaseLost
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from test_planning_failed_usage import _build_case

    case = await _build_case(tmp_path, session_factory, fail_invalid=False)
    read_context = case.service._read_context

    async def reclaim_after_context(*args, **kwargs):
        result = await read_context(*args, **kwargs)
        await _reclaim(PostgresCommandRepository(session_factory), session_factory, case.command.id)
        return result

    monkeypatch.setattr(case.service, "_read_context", reclaim_after_context)
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(CommandLeaseLost):
            await case.service.execute(case.command, work)
    assert case.gateway.requests == []
