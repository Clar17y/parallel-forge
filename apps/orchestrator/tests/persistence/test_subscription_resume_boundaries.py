"""Subscription resume retains causal controls and never repeats settled work."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_remote_remediation import (
    EVENT,
    SubscriptionRemoteRemediationController,
)
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import RunCommand, RunEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_preparation import preparation_case
from test_subscription_remote_remediation import remote_failure_case
from test_subscription_resume_controls import pause_for_resume
from test_subscription_usage import _reservation


async def prepared_case(session_factory, tmp_path, **options):
    factory, evidence, command, service, _ = await preparation_case(
        session_factory, tmp_path, **options
    )
    async with factory() as work:
        await service.execute(command, work)
    await PostgresCommandRepository(session_factory).complete(
        command.id, worker_id=command.lease_owner
    )
    return factory, evidence, command.run_id, service._approved_plans._artifacts


@pytest.mark.integration
async def test_subscription_resume_replay_allows_subsequent_scheduler_progress(
    session_factory, tmp_path
):
    factory, evidence, run_id, store = await prepared_case(session_factory, tmp_path)
    _, resume = await pause_for_resume(factory, session_factory, run_id)
    handler = ResumeRunHandler(artifact_store=store)
    async with factory() as work:
        await handler(resume, work)
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "next-primary", _reservation()
    )
    assert admission is not None and admission.task.task_id == evidence.producer.task_id
    async with factory() as work:
        await handler(resume, work)
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert scheduled.state == "leased" and scheduled.lease_owner == admission.lease.owner
        assert scheduled.repairs == 0


@pytest.mark.integration
@pytest.mark.parametrize("change", ["primary", "envelope", "extra"])
async def test_subscription_resume_replay_rejects_altered_continuation(
    session_factory, tmp_path, change
):
    factory, _, run_id, store = await prepared_case(session_factory, tmp_path)
    _, resume = await pause_for_resume(factory, session_factory, run_id)
    handler = ResumeRunHandler(artifact_store=store)
    async with factory() as work:
        await handler(resume, work)
    async with factory() as work:
        row = await work.session.scalar(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run.resumed")
        )
        binding = dict(row.payload["continuation"])
        binding.update(
            {
                "primary": {"primary_task_id": str(uuid4())},
                "envelope": {"envelope_digest": "f" * 64},
                "extra": {"unexpected": True},
            }[change]
        )
        row.payload = {**row.payload, "continuation": binding}
        await work.commit()
    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired):
            await handler(resume, work)


@pytest.mark.integration
async def test_subscription_resume_waits_for_admitted_attempt_to_settle(session_factory, tmp_path):
    factory, _, run_id, store = await prepared_case(session_factory, tmp_path)
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "active-primary", _reservation()
    )
    assert admission is not None
    _, resume = await pause_for_resume(factory, session_factory, run_id)
    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired, match="unresolved durable effects"):
            await ResumeRunHandler(artifact_store=store)(resume, work)
    async with factory() as work:
        assert (await work.runs.get(run_id)).state is RunState.PAUSED


async def repaired_pause_case(session_factory, tmp_path):
    factory, proposal, dispatch, validator, _, _, source, _, _ = await remote_failure_case(
        session_factory, tmp_path
    )
    repairs = SubscriptionRemoteRemediationController(
        dispatch._store, validator, validator._git_factory
    )
    async with factory() as work:
        await repairs.execute(source, work)
    _, resume = await pause_for_resume(factory, session_factory, source.run_id, source=source)
    handler = ResumeRunHandler(artifact_store=dispatch._store, subscription_remote_repairs=repairs)
    return factory, proposal, source, resume, handler


@pytest.mark.integration
@pytest.mark.parametrize("change", ["repair_event", "live_lease"])
async def test_subscription_resume_does_not_ack_unproved_or_live_repair(
    session_factory, tmp_path, change
):
    factory, _, source, resume, handler = await repaired_pause_case(session_factory, tmp_path)
    async with factory() as work:
        if change == "repair_event":
            event = await work.session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == source.run_id, RunEvent.event_type == EVENT
                )
            )
            event.payload = {**event.payload, "unexpected": True}
        else:
            (await work.session.get(RunCommand, source.id)).lease_expires_at = datetime.now(
                UTC
            ) + timedelta(seconds=60)
        await work.commit()
    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired):
            await handler(resume, work)
    async with factory() as work:
        assert (await work.runs.get(source.run_id)).state is RunState.PAUSED
        assert (await work.commands.get(source.id)).status is CommandStatus.LEASED


@pytest.mark.integration
async def test_subscription_resume_final_lease_expiry_rolls_back_ack_and_transition(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, source, resume, handler = await repaired_pause_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        paused = await work.runs.get(source.run_id)
        append = work.events.append

        async def expire(event):
            saved = await append(event)
            if event.event_type == "run.resumed":
                (await work.session.get(RunCommand, resume.id)).lease_expires_at = datetime.now(
                    UTC
                ) - timedelta(seconds=1)
                await work.session.flush()
            return saved

        monkeypatch.setattr(work.events, "append", expire)
        with pytest.raises(CommandLeaseLost):
            await handler(resume, work)
    async with factory() as work:
        assert await work.runs.get(source.run_id) == paused
        assert (await work.commands.get(source.id)).status is CommandStatus.LEASED
        scheduled = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        assert scheduled.state == "queued" and scheduled.repairs == 1
        assert not [
            event
            for event in await work.events.list_after(source.run_id, 0)
            if event.event_type
            in {"run.resumed", "subscription_remote_repair.acknowledged_on_resume"}
        ]
