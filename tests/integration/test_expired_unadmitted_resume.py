"""An expired claimed delivery with no admission is resumed as the same attempt."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import update
from test_delivery_development import _service_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _expired_unadmitted_case(tmp_path, factory):
    case, source, service, gateway, _git = await _service_case(tmp_path, factory)
    commands = PostgresCommandRepository(factory)
    await commands.enqueue(
        run_id=source.run_id,
        command_type="pause",
        idempotency_key=str(uuid4()),
        payload={},
        expected_run_version=source.expected_run_version,
        actor_id=source.actor_id,
    )
    pause = await commands.claim_next(worker_id="pause", lease_seconds=60, lane=CommandLane.CONTROL)
    assert pause is not None
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
        paused = await work.runs.get(source.run_id)
    await commands.complete(pause.id, worker_id="pause")
    async with factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await commands.enqueue(
        run_id=source.run_id,
        command_type="resume",
        idempotency_key=str(uuid4()),
        payload={},
        expected_run_version=paused.version,
        actor_id=source.actor_id,
    )
    resume = await commands.claim_next(worker_id="resume", lease_seconds=60)
    assert resume is not None
    return case, source, service, gateway, commands, resume, paused


async def test_expired_unadmitted_lease_resumes_same_semantic_attempt(
    tmp_path, workflow_session_factory
):
    case, source, service, gateway, commands, resume, _paused = await _expired_unadmitted_case(
        tmp_path, workflow_session_factory
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
        assert (await work.runs.get(source.run_id)).state is RunState.IMPLEMENTING
    assert (await commands.get(source.id)).status is CommandStatus.CANCELLED
    assert gateway.requests == []
    await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="developer", lease_seconds=60)
    assert (
        fresh is not None
        and fresh.payload["semantic_attempt"] == source.payload["semantic_attempt"]
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(fresh, work)
    assert len(gateway.requests) == 1


async def test_live_or_admitted_expired_source_remains_fail_closed(
    tmp_path, workflow_session_factory
):
    case, source, _service, gateway, _commands, resume, paused = await _expired_unadmitted_case(
        tmp_path, workflow_session_factory
    )
    async with workflow_session_factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(lease_expires_at=datetime.now(UTC) + timedelta(minutes=1))
        )
    with pytest.raises(CommandRecoveryRequired):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert await work.runs.get(source.run_id) == paused
    assert gateway.requests == []
