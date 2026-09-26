"""Base effects require current source authority and exact retained receipts."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.services.subscription_base_update import ADMISSION_EVENT, EVENT
from forge.domain.run import RunState
from forge.domain.subscription import TaskBudget
from forge.persistence.models import Approval, OperationIntent, RunEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_base_adoption import base_adoption_case


@pytest.mark.integration
@pytest.mark.parametrize("limit", ["provider_budget", "repair_limit"])
async def test_exhaustion_stops_before_base_effects(session_factory, tmp_path, monkeypatch, limit):
    if limit == "provider_budget":
        import test_subscription_acceptance_preparation as fixture

        # Configure the fixture's real initial contract. Production admission and
        # accounting remain unchanged and consume all three provider attempts.
        monkeypatch.setattr(fixture, "TaskBudget", lambda **_: TaskBudget(max_provider_attempts=3))
    case = await base_adoption_case(session_factory, tmp_path)
    async with case.factory() as work:
        if limit == "repair_limit":
            scheduled = await work.session.get(
                SubscriptionScheduledTask, case.original.decision.task_id
            )
            scheduled.repairs = scheduled.max_repairs
        else:
            usage = await work.subscription_budget.usage(
                case.command.run_id, case.original.decision.task_id
            )
            assert usage.consumed.provider_attempts == 3
        await work.commit()
    for _ in range(2):
        async with case.factory() as work:
            await case.service.execute(case.command, work)
    async with case.factory() as work:
        run = await work.runs.get(case.command.run_id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        assert (
            await work.session.get(SubscriptionSchedulerRun, run.id)
        ).candidate_state == "closed"
        assert await work.session.get(SubscriptionRepairDebit, case.original.attempt_id) is None
        await work.runs.transition(run.id, run.version, RunState.CANCELLED, "test.cleanup", {})
        await work.commit()
    assert case.updates == case.adoptions == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "change", ["approval", "epoch", "task_pause", "observation", "scheduling_scope"]
)
async def test_base_effects_reject_changed_source(session_factory, tmp_path, change):
    case = await base_adoption_case(session_factory, tmp_path)
    async with case.factory() as work:
        if change == "approval":
            record = await work.releases.get_for_run(case.command.run_id)
            publication = await work.operations.get(record.publication_intent_id)
            (
                await work.session.get(Approval, UUID(publication.request_payload["approval_id"]))
            ).invalidated_at = datetime.now(UTC)
        elif change == "epoch":
            (
                await work.session.get(SubscriptionSchedulerRun, case.command.run_id)
            ).candidate_epoch += 1
        elif change == "task_pause":
            (
                await work.session.get(SubscriptionTask, case.original.decision.task_id)
            ).pause_requested = True
        elif change == "scheduling_scope":
            (
                await work.session.get(SubscriptionScheduledTask, case.original.decision.task_id)
            ).owned_paths = ["unapproved"]
        else:
            event = await work.session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == case.command.run_id, RunEvent.event_type == "run.pr_observed"
                )
            )
            event.payload = {**event.payload, "remediation_key": "changed"}
        await work.commit()
    async with case.factory() as work:
        with pytest.raises((CommandRecoveryRequired, SubscriptionDecisionError)):
            await case.service.execute(case.command, work)
    assert case.updates == case.adoptions == []


@pytest.mark.integration
@pytest.mark.parametrize("change", ["admission", "adoption", "effect", "debit"])
async def test_base_adoption_replay_rejects_altered_history(session_factory, tmp_path, change):
    case = await base_adoption_case(session_factory, tmp_path)
    async with case.factory() as work:
        await case.service.execute(case.command, work)
    async with case.factory() as work:
        if change == "debit":
            await work.session.delete(
                await work.session.get(SubscriptionRepairDebit, case.original.attempt_id)
            )
        elif change == "effect":
            record = await work.releases.get_for_run(case.command.run_id)
            operation = await work.session.get(OperationIntent, record.base_adoption_intent_id)
            operation.request_payload = {**operation.request_payload, "previous_head_sha": "d" * 40}
        else:
            event = await work.session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == case.command.run_id,
                    RunEvent.event_type == (ADMISSION_EVENT if change == "admission" else EVENT),
                )
            )
            event.payload = {**event.payload, "unexpected": True}
        await work.commit()
    async with case.factory() as work:
        with pytest.raises((CommandRecoveryRequired, SubscriptionDecisionError)):
            await case.service.execute(case.command, work)
    assert len(case.updates) == len(case.adoptions) == 1
