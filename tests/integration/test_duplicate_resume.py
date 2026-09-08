"""Accepted duplicate resume intentions must not obstruct one restoration."""

from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.runs import RunCommandRequest, RunCommandService
from forge.application.services.worker import Worker
from forge.domain.command import CommandStatus
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import update

from tests.integration.test_run_controls import _approval_run, _control_command

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _accepted_resumes(persisted_run, session_factory):
    run = await _approval_run(session_factory, persisted_run)
    pause = await _control_command(session_factory, run, "pause")
    async with PostgresUnitOfWork(session_factory) as work:
        await PauseRunHandler()(pause, work)
    commands = PostgresCommandRepository(session_factory)
    await commands.complete(pause.id, worker_id="controls")
    service = RunCommandService(lambda: PostgresUnitOfWork(session_factory))
    resumes = [
        await service.enqueue(
            actor=AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4()),
            run_id=run.id,
            idempotency_key=f"resume-{index}",
            request=RunCommandRequest(command_type="resume", expected_run_version=run.version + 1),
        )
        for index in range(3)
    ]
    return run, pause, commands, resumes


async def test_distinct_accepted_resumes_restore_once_and_leave_no_queue_blocker(
    persisted_run, session_factory
):
    run, pause, commands, resumes = await _accepted_resumes(persisted_run, session_factory)
    worker = Worker(
        commands, session_factory, handlers={"resume": ResumeRunHandler()}, worker_id="resume"
    )
    assert await worker.tick() is True
    assert await worker.tick() is None
    async with PostgresUnitOfWork(session_factory) as work:
        restored = await work.runs.get(run.id)
        events = await work.events.list_after(run.id, 0)
    assert restored.state is run.state
    assert restored.version == run.version + 2
    assert restored.pending_evidence_digest == run.pending_evidence_digest
    assert sum(event.event_type == "run.resumed" for event in events) == 1
    assert (await commands.get(resumes[0].id)).status is CommandStatus.COMPLETED
    for redundant in resumes[1:]:
        settled = await commands.get(redundant.id)
        assert settled.status is CommandStatus.CANCELLED
        assert settled.attempt == 0
        receipts = [
            event
            for event in events
            if event.event_type == "resume.superseded"
            and event.payload["command_id"] == str(redundant.id)
        ]
        assert len(receipts) == 1
        assert receipts[0].payload["resume_command_id"] == str(resumes[0].id)
        assert receipts[0].payload["actor_id"] == str(redundant.actor_id)
        assert receipts[0].payload["pause_command_id"] == str(pause.id)


@pytest.mark.parametrize(
    "blocker", ["payload", "actor", "version", "attempt", "operation", "stage"]
)
async def test_duplicate_settlement_rolls_back_when_restoration_is_not_safe(
    persisted_run, session_factory, blocker
):
    run, _pause, commands, resumes = await _accepted_resumes(persisted_run, session_factory)
    claimed = await commands.claim_next(worker_id="resume", lease_seconds=60)
    assert claimed is not None and claimed.id == resumes[0].id
    changes = {
        "payload": {"payload": {"unexpected": True}},
        "actor": {"actor_id": None},
        "version": {"expected_run_version": run.version},
        "attempt": {"attempt_count": 1},
    }
    if blocker in changes:
        async with session_factory() as session, session.begin():
            await session.execute(
                update(RunCommand).where(RunCommand.id == resumes[-1].id).values(**changes[blocker])
            )
    else:
        async with PostgresUnitOfWork(session_factory) as work:
            if blocker == "operation":
                await work.operations.begin(
                    run_id=run.id,
                    operation_type="repository_write",
                    idempotency_key="unsettled",
                    request_digest="a" * 64,
                    request_payload={},
                )
            else:
                await work.commands.enqueue(
                    run_id=run.id,
                    command_type="start_planning",
                    idempotency_key="unsettled",
                    payload={},
                    expected_run_version=run.version,
                )
            await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        before = await work.runs.get(run.id)
    with pytest.raises(CommandRecoveryRequired):
        async with PostgresUnitOfWork(session_factory) as work:
            await ResumeRunHandler()(claimed, work)
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.runs.get(run.id) == before
        events = await work.events.list_after(run.id, 0)
        assert not any(event.event_type == "resume.superseded" for event in events)
    assert (await commands.get(resumes[1].id)).status is CommandStatus.PENDING
    assert (await commands.get(resumes[-1].id)).status is CommandStatus.PENDING
