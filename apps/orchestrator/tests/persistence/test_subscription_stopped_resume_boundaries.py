"""Stopped-attempt resumption proves process, source, accounting and final authority."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.domain.subscription import TaskBudget
from forge.persistence.models import RunCommand
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_stopped_resume import stopped_case


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "result",
        "launch",
        "usage",
        "epoch",
        "task_version",
        "task_pause",
        "effect",
        "budget",
        "repair_limit",
        "scheduling_scope",
        "route",
    ],
)
async def test_stopped_subscription_resume_rejects_unproved_authority(
    session_factory, tmp_path, change
):
    options = {"primary_budget": TaskBudget(max_provider_attempts=2)} if change == "budget" else {}
    case = await stopped_case(session_factory, tmp_path, **options)
    identity = case.admission.attempt.attempt_id
    async with case.factory() as work:
        task = await work.session.get(SubscriptionTask, case.admission.task.task_id)
        source = await work.session.get(SubscriptionAttemptResult, identity)
        if change == "result":
            source.result_payload = {**source.result_payload, "failure_detail": "changed"}
        elif change == "launch":
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == identity
                )
            )
            launch.terminal_payload = {**launch.terminal_payload, "stop_confirmed": False}
        elif change == "usage":
            usage = await work.session.get(SubscriptionAttemptConsumption, identity)
            usage.charged = {**usage.charged, "provider_attempts": 0}
        elif change == "epoch":
            (await work.session.get(SubscriptionSchedulerRun, case.run_id)).candidate_epoch += 1
        elif change == "task_version":
            task.version += 1
        elif change == "task_pause":
            task.pause_requested = True
        elif change == "effect":
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=case.run_id,
                    task_id=task.id,
                    lease_owner=case.admission.lease.owner,
                    lease_generation=case.admission.lease.generation,
                    candidate_epoch=case.admission.candidate_epoch,
                    state="reconciling",
                )
            )
        elif change in {"repair_limit", "scheduling_scope", "route"}:
            scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
            if change == "repair_limit":
                scheduled.max_repairs += 1
            elif change == "scheduling_scope":
                scheduled.owned_paths = ["unapproved"]
            else:
                scheduled.provider = "unapproved"
        await work.commit()
    async with case.factory() as work:
        with pytest.raises(CommandRecoveryRequired):
            await ResumeRunHandler(artifact_store=case.store)(case.resume, work)
    async with case.factory() as work:
        assert (await work.runs.get(case.run_id)).state is RunState.PAUSED
        assert (
            await work.session.get(SubscriptionAttemptResult, identity)
        ).application_payload is None
        assert await work.session.get(SubscriptionRepairDebit, identity) is None
        assert (
            await work.session.get(SubscriptionScheduledTask, case.admission.task.task_id)
        ).state == "reconciling"


@pytest.mark.integration
@pytest.mark.parametrize("change", ["application", "boolean", "debit", "result"])
async def test_stopped_subscription_resume_replay_rejects_changed_receipts(
    session_factory, tmp_path, change
):
    case = await stopped_case(session_factory, tmp_path)
    identity = case.admission.attempt.attempt_id
    handler = ResumeRunHandler(artifact_store=case.store)
    async with case.factory() as work:
        await handler(case.resume, work)
    async with case.factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, identity)
        if change == "debit":
            await work.session.delete(await work.session.get(SubscriptionRepairDebit, identity))
        elif change == "result":
            source.result_payload = {**source.result_payload, "failure_detail": "changed"}
        else:
            source.application_payload = {
                **source.application_payload,
                **({"unexpected": True} if change == "application" else {"repairs": True}),
            }
            source.application_digest = canonical_digest(source.application_payload)
        await work.commit()
    async with case.factory() as work:
        with pytest.raises(CommandRecoveryRequired):
            await handler(case.resume, work)


@pytest.mark.integration
async def test_stopped_subscription_resume_lease_expiry_rolls_back_repair(
    session_factory, tmp_path, monkeypatch
):
    case = await stopped_case(session_factory, tmp_path)
    identity = case.admission.attempt.attempt_id
    async with case.factory() as work:
        paused = await work.runs.get(case.run_id)
        append = work.events.append

        async def expire(event):
            saved = await append(event)
            if event.event_type == "run.resumed":
                (await work.session.get(RunCommand, case.resume.id)).lease_expires_at = (
                    datetime.now(UTC) - timedelta(seconds=1)
                )
                await work.session.flush()
            return saved

        monkeypatch.setattr(work.events, "append", expire)
        with pytest.raises(CommandLeaseLost):
            await ResumeRunHandler(artifact_store=case.store)(case.resume, work)
    async with case.factory() as work:
        assert await work.runs.get(case.run_id) == paused
        assert (
            await work.session.get(SubscriptionAttemptResult, identity)
        ).application_payload is None
        assert await work.session.get(SubscriptionRepairDebit, identity) is None
        assert (await work.session.get(SubscriptionAttempt, identity)).status == "reconciling"
