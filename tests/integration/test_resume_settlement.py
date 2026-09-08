"""PostgreSQL proof for settling a stopped normal delivery before resume."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.resume_reconciliation import ResumeReconciler
from forge.application.services.suspended_delivery import record_suspended_delivery
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.domain.approval import ApprovalGate
from forge.domain.command import CommandStatus, thaw_payload
from forge.domain.run import RunState
from forge.observability.usage import UsageRecord
from forge.persistence.models import RunCommand
from forge.persistence.models import RunEvent as RunEventRecord
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import update

from tests.integration.test_run_controls import _control_command

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _stopped_delivery(factory, run, phase="initial_plan"):
    kind = "implement" if phase == "implement" else "plan"
    semantic_attempt = 2 if phase == "restarted_plan" else 1
    if phase != "initial_plan":
        async with PostgresUnitOfWork(factory) as work:
            run = await work.runs.transition(
                run.id, run.version, RunState.PLANNING, "test.planning", {}
            )
            if kind == "implement":
                run = await work.runs.await_approval(
                    run.id, run.version, ApprovalGate.PLAN, "a" * 64, "test.approval", {}
                )
                run = await work.runs.transition(
                    run.id, run.version, RunState.PREPARING_WORKTREE, "test.preparing", {}
                )
                run = await work.runs.transition(
                    run.id, run.version, RunState.IMPLEMENTING, "test.implementing", {}
                )
            await work.commit()
    commands = PostgresCommandRepository(factory)
    await commands.enqueue(
        run_id=run.id,
        command_type="implement" if kind == "implement" else "start_planning",
        idempotency_key=f"plan:{uuid4().hex}",
        payload={} if phase == "initial_plan" else {"semantic_attempt": semantic_attempt},
        expected_run_version=run.version,
    )
    source = await commands.claim_next(worker_id="normal", lease_seconds=60)
    assert source is not None
    step_id, execution_id = uuid4(), uuid4()
    async with PostgresUnitOfWork(factory) as work:
        admitted = (
            await work.runs.transition(run.id, run.version, RunState.PLANNING, "test.planning", {})
            if phase == "initial_plan"
            else run
        )
        await work.executions.admit(
            admitted.id,
            step_id,
            execution_id,
            kind,
            semantic_attempt,
            AgentRole.DEVELOPER if kind == "implement" else AgentRole.PLANNER,
            "1",
            "test",
            "fixture",
        )
        await work.commit()
    pause = await _control_command(factory, admitted, "pause")
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
    await commands.complete(pause.id, worker_id="controls")
    async with PostgresUnitOfWork(factory) as work:
        await work.executions.finalize(
            admitted.id,
            step_id,
            execution_id,
            AgentFinishStatus.CANCELLED,
            UsageRecord(
                provider="test",
                model="fixture",
                prompt_version="1",
                input_tokens=7,
                pricing_version="fixture",
                currency="USD",
                unknown_price_reason="fixture",
            ),
        )
        await record_suspended_delivery(work, source, admitted, step_id, kind, semantic_attempt)
        await work.commit()
    async with PostgresUnitOfWork(factory) as work:
        await work.session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await work.commit()
    async with PostgresUnitOfWork(factory) as work:
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
    claimed = await commands.claim_next(
        worker_id="resume", lease_seconds=30, lane=CommandLane.NORMAL
    )
    assert claimed is not None and claimed.id == resume.id
    return commands, source, claimed


@pytest.mark.parametrize("phase", ["initial_plan", "implement", "restarted_plan"])
async def test_resume_reconciliation_cancels_exact_expired_stopped_delivery(
    persisted_run, session_factory, phase
):
    commands, source, resume = await _stopped_delivery(session_factory, persisted_run, phase)
    async with PostgresUnitOfWork(session_factory) as work:
        settled = await ResumeReconciler().reconcile(work, resume)
        assert tuple(command.id for command in settled) == (source.id,)
        await work.commit()
    stored = await commands.get(source.id)
    assert stored.status is CommandStatus.CANCELLED
    assert stored.lease_owner is None
    async with PostgresUnitOfWork(session_factory) as work:
        replayed = await ResumeReconciler().reconcile(work, resume)
        assert tuple(command.id for command in replayed) == (source.id,)
        await work.commit()


async def test_resume_reconciliation_refuses_tampered_receipt_without_settlement(
    persisted_run, session_factory
):
    commands, source, resume = await _stopped_delivery(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        events = await work.events.list_after(source.run_id, 0)
        receipt = next(event for event in events if event.event_type == "delivery.suspended")
        await work.session.execute(
            update(RunEventRecord)
            .where(RunEventRecord.id == receipt.id)
            .values(
                payload={
                    **thaw_payload(receipt.payload),
                    "delivery_attempt": source.attempt + 1,
                }
            )
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await ResumeReconciler().reconcile(work, resume)
    assert (await commands.get(source.id)).status is CommandStatus.LEASED


async def test_resume_reconciliation_refuses_a_renewal_race(persisted_run, session_factory):
    commands, source, resume = await _stopped_delivery(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=60))
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(CommandRecoveryRequired, match="lease changed"):
            await ResumeReconciler().reconcile(work, resume)
    retained = await commands.get(source.id)
    assert retained.status is CommandStatus.LEASED
    assert retained.lease_owner == "normal"
