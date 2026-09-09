"""PostgreSQL evidence for the first safe paused-run resume slice."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.worker import Worker
from forge.domain.actor import AgentRole
from forge.domain.approval import ApprovalGate
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork

from tests.integration.test_run_controls import _approval_run, _control_command

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("target", ["plan", "pr", "merge", "intervention"])
async def test_resume_restores_approval_gate_after_pause_and_ack(
    persisted_run, session_factory, target
) -> None:
    run = await _approval_run(session_factory, persisted_run)
    if target != "plan":
        async with PostgresUnitOfWork(session_factory) as work:
            for state in (
                RunState.PREPARING_WORKTREE,
                RunState.IMPLEMENTING,
                RunState.VALIDATING,
                RunState.REVIEWING,
            ):
                run = await work.runs.transition(run.id, run.version, state, "test.advance", {})
            if target == "intervention":
                run = await work.runs.intervene(run.id, run.version, "test.intervention", {})
            else:
                run = await work.runs.await_approval(
                    run.id, run.version, ApprovalGate.PR, "b" * 64, "test.pr", {}
                )
                if target == "merge":
                    for state in (RunState.PUBLISHING_PR, RunState.MONITORING_PR):
                        run = await work.runs.transition(
                            run.id, run.version, state, "test.advance", {}
                        )
                    run = await work.runs.await_approval(
                        run.id, run.version, ApprovalGate.MERGE, "c" * 64, "test.merge", {}
                    )
            await work.commit()
    pause = await _control_command(session_factory, run, "pause")
    async with PostgresUnitOfWork(session_factory) as work:
        await PauseRunHandler()(pause, work)
    await PostgresCommandRepository(session_factory).complete(pause.id, worker_id="controls")

    async with PostgresUnitOfWork(session_factory) as work:
        paused = await work.runs.get(run.id)
        resume = await work.commands.enqueue(
            run_id=run.id,
            command_type="resume",
            idempotency_key=f"resume:{uuid4().hex}",
            payload={},
            expected_run_version=paused.version,
            actor_id=pause.actor_id,
        )
        await work.commit()
    worker = Worker(
        PostgresCommandRepository(session_factory),
        session_factory,
        handlers={"resume": ResumeRunHandler()},
        worker_id="resume-worker",
    )
    assert await worker.tick() is True
    async with PostgresUnitOfWork(session_factory) as work:
        current = await work.runs.get(run.id)
        assert current == replace(run, version=run.version + 2)
        await work.commit()
    completed = await PostgresCommandRepository(session_factory).get(resume.id)
    assert completed.status is CommandStatus.COMPLETED


@pytest.mark.parametrize("blocker", ["command", "execution", "operation"])
async def test_resume_refuses_unsettled_command_and_leaves_run_paused(
    persisted_run, session_factory, blocker
) -> None:
    run = await _approval_run(session_factory, persisted_run)
    pause = await _control_command(session_factory, run, "pause")
    async with PostgresUnitOfWork(session_factory) as work:
        await PauseRunHandler()(pause, work)
    await PostgresCommandRepository(session_factory).complete(pause.id, worker_id="controls")
    async with PostgresUnitOfWork(session_factory) as work:
        paused = await work.runs.get(run.id)
        resume = await work.commands.enqueue(
            run_id=run.id,
            command_type="resume",
            idempotency_key=f"resume:{uuid4().hex}",
            payload={},
            expected_run_version=paused.version,
            actor_id=pause.actor_id,
        )
        if blocker == "command":
            await work.commands.enqueue(
                run_id=run.id,
                command_type="start_planning",
                idempotency_key=f"blocker:{uuid4().hex}",
                payload={},
                expected_run_version=paused.version,
            )
        elif blocker == "execution":
            await work.executions.admit(
                run.id, uuid4(), uuid4(), "plan", 1, AgentRole.PLANNER, "1", "test", "fixture"
            )
        else:
            await work.operations.begin(
                run_id=run.id,
                operation_type="repository_write",
                idempotency_key="unresolved",
                request_digest="a" * 64,
                request_payload={},
            )
        await work.commit()
    claimed = await PostgresCommandRepository(session_factory).claim_next(
        worker_id="controls", lease_seconds=60, lane=CommandLane.NORMAL
    )
    assert claimed is not None and claimed.id == resume.id
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await ResumeRunHandler()(claimed, work)
        assert (await work.runs.get(run.id)).state is RunState.PAUSED


async def test_resume_replay_preserves_exact_gate_evidence(persisted_run, session_factory):
    run = await _approval_run(session_factory, persisted_run)
    commands = PostgresCommandRepository(session_factory)
    pause = await _control_command(session_factory, run, "pause")
    async with PostgresUnitOfWork(session_factory) as work:
        await PauseRunHandler()(pause, work)
    await commands.complete(pause.id, worker_id="controls")
    await commands.enqueue(
        run_id=run.id,
        command_type="resume",
        idempotency_key="resume-replay",
        payload={},
        expected_run_version=run.version + 1,
        actor_id=pause.actor_id,
    )
    resume = await commands.claim_next(worker_id="resume", lease_seconds=60)
    assert resume is not None
    async with PostgresUnitOfWork(session_factory) as work:
        await ResumeRunHandler()(resume, work)
    async with PostgresUnitOfWork(session_factory) as work:
        await ResumeRunHandler()(resume, work)
        restored = await work.runs.get(run.id)
        assert restored.pending_evidence_digest == run.pending_evidence_digest
        assert restored.version == run.version + 2
        assert (
            len(
                [
                    event
                    for event in await work.events.list_after(run.id, 0)
                    if event.event_type == "run.resumed"
                ]
            )
            == 1
        )
