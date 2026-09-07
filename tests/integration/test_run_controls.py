"""PostgreSQL evidence for durable pause and cancel handlers."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import CancelRunHandler, PauseRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.worker import Worker
from forge.domain.approval import ApprovalGate
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.resource import ResourceState
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def test_stale_control_is_terminally_rejected_instead_of_retaining_lease(
    persisted_run, session_factory
) -> None:
    commands = PostgresCommandRepository(session_factory)
    control = await commands.enqueue(
        run_id=persisted_run.id,
        command_type="pause",
        idempotency_key="stale-control",
        payload={},
        expected_run_version=persisted_run.version,
        actor_id=uuid4(),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        current = await work.runs.transition(
            persisted_run.id, persisted_run.version, RunState.PLANNING, "test.planning", {}
        )
        await work.commit()
    stage = await commands.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="new-stage",
        payload={},
        expected_run_version=current.version,
    )
    worker = Worker(
        commands,
        session_factory,
        handlers={"pause": PauseRunHandler()},
        worker_id="control-worker",
        lane=CommandLane.CONTROL,
    )
    assert await worker.tick() is False
    rejected = await commands.get(control.id)
    assert rejected.status is CommandStatus.FAILED
    assert rejected.lease_owner is None
    claimed = await commands.claim_next(worker_id="normal", lease_seconds=30)
    assert claimed is not None and claimed.id == stage.id


async def _approval_run(factory, run: RunSnapshot) -> RunSnapshot:
    async with PostgresUnitOfWork(factory) as work:
        planning = await work.runs.transition(
            run.id, run.version, RunState.PLANNING, "test.planning", {}
        )
        approved = await work.runs.await_approval(
            run.id, planning.version, ApprovalGate.PLAN, "a" * 64, "test.awaiting_plan_approval", {}
        )
        await work.commit()
        return approved


async def _control_command(factory, run: RunSnapshot, command_type: str) -> CommandEnvelope:
    commands = PostgresCommandRepository(factory)
    await commands.enqueue(
        run_id=run.id,
        command_type=command_type,
        idempotency_key=f"{run.id}:{command_type}:{uuid4().hex}",
        payload={},
        expected_run_version=run.version,
        actor_id=uuid4(),
    )
    claimed = await commands.claim_next(
        worker_id="controls", lease_seconds=60, lane=CommandLane.CONTROL
    )
    assert claimed is not None
    return claimed


async def test_pause_retains_exact_approval_suspension_context_and_replays_once(
    persisted_run, session_factory
) -> None:
    run = await _approval_run(session_factory, persisted_run)
    command = await _control_command(session_factory, run, "pause")
    async with PostgresUnitOfWork(session_factory) as work:
        await PauseRunHandler()(command, work)
    async with PostgresUnitOfWork(session_factory) as work:
        paused = await work.runs.get(command.run_id)
        assert paused.state is RunState.PAUSED
        assert paused.suspended_state is RunState.AWAITING_PLAN_APPROVAL
        assert paused.suspension_context is not None
        assert paused.suspension_context.pending_gate is ApprovalGate.PLAN
        assert paused.suspension_context.pending_evidence_digest == "a" * 64
        await PauseRunHandler()(command, work)
        events = await work.events.list_for_version(command.run_id, paused.version)
        assert [event.event_type for event in events if event.event_type == "run.paused"] == [
            "run.paused"
        ]


async def test_cancel_retains_managed_resource_identity_without_successor_dispatch(
    persisted_run, session_factory
) -> None:
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.update_resource(
            persisted_run.id,
            persisted_run.version,
            worktree_path="/managed/worktree",
            database_state=ResourceState.DISABLED,
            event_type="test.resource_bound",
            event_payload={},
        )
        await work.commit()
    command = await _control_command(session_factory, run, "cancel")
    async with PostgresUnitOfWork(session_factory) as work:
        await CancelRunHandler()(command, work)
    async with PostgresUnitOfWork(session_factory) as work:
        cancelled = await work.runs.get(command.run_id)
        assert cancelled.state is RunState.CANCELLED
        assert cancelled.worktree_path == "/managed/worktree"
        assert cancelled.database_state is ResourceState.DISABLED
        assert (
            await PostgresCommandRepository(session_factory).claim_next(
                worker_id="controls-next", lease_seconds=60, lane=CommandLane.CONTROL
            )
            is None
        )


async def test_renewed_lease_succeeds_but_substitution_and_stale_version_do_not_mutate(
    persisted_run, session_factory
) -> None:
    command = await _control_command(session_factory, persisted_run, "pause")
    renewed = await PostgresCommandRepository(session_factory).renew(
        command.id, worker_id="controls", lease_seconds=60
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await PauseRunHandler()(command, work)
    forged = replace(renewed, id=uuid4())
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await PauseRunHandler()(forged, work)
        assert (await work.runs.get(command.run_id)).state is RunState.PAUSED
    wrong_actor = replace(renewed, actor_id=uuid4())
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await PauseRunHandler()(wrong_actor, work)
        assert (await work.runs.get(command.run_id)).state is RunState.PAUSED
    stale = replace(renewed, expected_run_version=renewed.expected_run_version + 9)
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await PauseRunHandler()(stale, work)
        assert (await work.runs.get(command.run_id)).state is RunState.PAUSED
