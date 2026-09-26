"""Subscription work resumes its durable primary and exact pending controller delivery."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane
from forge.application.services.subscription_remote_remediation import (
    SubscriptionRemoteRemediationController,
)
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.repositories.commands import PostgresCommandRepository
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_preparation import preparation_case
from test_subscription_remote_remediation import remote_failure_case


@pytest.mark.integration
async def test_subscription_prepared_primary_resumes_existing_schedule(session_factory, tmp_path):
    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    async with factory() as work:
        await service.execute(command, work)
    commands = PostgresCommandRepository(session_factory)
    await commands.complete(command.id, worker_id=command.lease_owner)
    async with factory() as work:
        before = await work.subscription.get_task(command.run_id, evidence.producer.task_id)
    continued = await pause_and_resume(
        factory, session_factory, command.run_id, service._approved_plans._artifacts
    )
    assert continued is None
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.IMPLEMENTING
        assert await work.subscription.get_task(run.id, evidence.producer.task_id) == before
        scheduled = await work.session.get(SubscriptionScheduledTask, evidence.producer.task_id)
        assert scheduled.state == "queued" and scheduled.repairs == 0
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from test_subscription_usage import _reservation

    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "resumed-primary", _reservation()
    )
    assert admission is not None and admission.task == before


async def pause_for_resume(factory, session_factory, run_id, *, source=None):
    commands = PostgresCommandRepository(session_factory)
    actor = uuid4()
    async with factory() as work:
        if source is not None:
            row = await work.session.get(RunCommand, source.id)
            row.available_at = datetime.now(UTC) + timedelta(hours=1)
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        run = await work.runs.get(run_id)
        await work.commands.enqueue(
            run_id=run_id,
            command_type="pause",
            idempotency_key=f"{run_id}:pause:{run.version}",
            payload={},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    pause = await commands.claim_next(
        worker_id="pause", lease_seconds=120, lane=CommandLane.CONTROL
    )
    assert pause is not None and pause.command_type == "pause"
    async with factory() as work:
        await PauseRunHandler()(pause, work)
    await commands.complete(pause.id, worker_id=pause.lease_owner)
    async with factory() as work:
        run = await work.runs.get(run_id)
        await work.commands.enqueue(
            run_id=run_id,
            command_type="resume",
            idempotency_key=f"{run_id}:resume:{run.version}",
            payload={},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    resume = await commands.claim_next(worker_id="resume", lease_seconds=120)
    assert resume is not None and resume.command_type == "resume"
    return commands, resume


async def pause_and_resume(factory, session_factory, run_id, store, *, source=None, repairs=None):
    commands, resume = await pause_for_resume(factory, session_factory, run_id, source=source)
    for _ in range(2):
        async with factory() as work:
            await ResumeRunHandler(artifact_store=store, subscription_remote_repairs=repairs)(
                resume, work
            )
    await commands.complete(resume.id, worker_id=resume.lease_owner)
    return await commands.claim_next(worker_id="continued", lease_seconds=120)


@pytest.mark.integration
@pytest.mark.parametrize(
    "stage", ["before_reopen", "failed_before_reopen", "after_reopen", "after_ack"]
)
async def test_subscription_remote_resume_preserves_one_primary_repair(
    session_factory, tmp_path, stage
):
    factory, proposal, dispatch, validator, commands, _, command, _, _ = await remote_failure_case(
        session_factory, tmp_path
    )
    controller = SubscriptionRemoteRemediationController(
        dispatch._store, validator, validator._git_factory
    )
    if stage in {"after_reopen", "after_ack"}:
        async with factory() as work:
            await controller.execute(command, work)
    if stage == "after_ack":
        await commands.complete(command.id, worker_id=command.lease_owner)
    elif stage == "failed_before_reopen":
        await commands.fail(
            command.id, worker_id=command.lease_owner, error="unadmitted test failure"
        )
    continued = await pause_and_resume(
        factory,
        session_factory,
        command.run_id,
        dispatch._store,
        source=command if stage in {"before_reopen", "after_reopen"} else None,
        repairs=controller,
    )
    if stage in {"before_reopen", "failed_before_reopen"}:
        assert continued is not None and continued.command_type == "remediate_remote"
        assert continued.id != command.id
        async with factory() as work:
            await controller.execute(continued, work)
        await commands.complete(continued.id, worker_id=continued.lease_owner)
    else:
        assert continued is None
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, run.id)
        assert run.state is RunState.REMEDIATING and run.remote_remediation_count == 1
        assert scheduled.state == "queued" and scheduled.repairs == 1
        assert scheduler.candidate_epoch == proposal.review.candidate_epoch + 1
        assert not await work.operations.list_unresolved()
        status = CommandStatus.COMPLETED
        if stage == "before_reopen":
            status = CommandStatus.CANCELLED
        elif stage == "failed_before_reopen":
            status = CommandStatus.FAILED
        assert (await work.commands.get(command.id)).status is status
