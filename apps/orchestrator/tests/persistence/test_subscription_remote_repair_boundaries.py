"""Remote repair never bypasses current approval, task, lease, or budget authority."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.services.subscription_remote_remediation import (
    EVENT,
    SubscriptionRemoteRemediationController,
)
from forge.domain.run import RunState
from forge.persistence.models import Approval, RunCommand, RunEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from forge.persistence.repositories.commands import CommandLeaseLost
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_remote_remediation import remote_failure_case


@pytest.mark.integration
@pytest.mark.parametrize("change", ["approval", "epoch", "task_pause", "observation"])
async def test_remote_repair_rejects_stale_or_altered_authority(session_factory, tmp_path, change):
    factory, proposal, dispatch, validator, _, _, command, _, _ = await remote_failure_case(
        session_factory, tmp_path
    )
    controller = SubscriptionRemoteRemediationController(
        dispatch._store, validator, validator._git_factory
    )
    async with factory() as work:
        if change == "approval":
            record = await work.releases.get_for_run(command.run_id)
            intent = await work.operations.get(record.publication_intent_id)
            (
                await work.session.get(Approval, UUID(intent.request_payload["approval_id"]))
            ).invalidated_at = datetime.now(UTC)
        elif change == "epoch":
            (await work.session.get(SubscriptionSchedulerRun, command.run_id)).candidate_epoch += 1
        elif change == "task_pause":
            (
                await work.session.get(SubscriptionTask, proposal.decision.task_id)
            ).pause_requested = True
        else:
            event = await work.session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == command.run_id, RunEvent.event_type == "run.pr_observed"
                )
            )
            event.payload = {**event.payload, "remediation_key": "changed"}
        await work.commit()
    async with factory() as work:
        with pytest.raises((CommandRecoveryRequired, SubscriptionDecisionError)):
            await controller.execute(command, work)
    async with factory() as work:
        assert (await work.runs.get(command.run_id)).state is RunState.REMEDIATING
        assert (
            await work.session.get(SubscriptionTask, proposal.decision.task_id)
        ).state == "blocked"
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None


@pytest.mark.integration
async def test_remote_repair_final_lease_expiry_rolls_back_every_mutation(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, dispatch, validator, _, _, command, _, _ = await remote_failure_case(
        session_factory, tmp_path
    )
    controller = SubscriptionRemoteRemediationController(
        dispatch._store, validator, validator._git_factory
    )
    async with factory() as work:
        append = work.events.append

        async def expire(event):
            value = await append(event)
            if event.event_type == EVENT:
                (await work.session.get(RunCommand, command.id)).lease_expires_at = datetime.now(
                    UTC
                ) - timedelta(seconds=1)
                await work.session.flush()
            return value

        monkeypatch.setattr(work.events, "append", expire)
        with pytest.raises(CommandLeaseLost):
            await controller.execute(command, work)
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionTask, proposal.decision.task_id)
        ).state == "blocked"
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        assert not [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == EVENT
        ]


@pytest.mark.integration
async def test_remote_task_repair_exhaustion_requires_intervention(session_factory, tmp_path):
    factory, proposal, dispatch, validator, _, _, command, _, _ = await remote_failure_case(
        session_factory, tmp_path
    )
    controller = SubscriptionRemoteRemediationController(
        dispatch._store, validator, validator._git_factory
    )
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        scheduled.repairs = scheduled.max_repairs
        await work.commit()
    async with factory() as work:
        result = await controller.execute(command, work)
        assert result.state is RunState.AWAITING_HUMAN_INTERVENTION
    async with factory() as work:
        assert await controller.execute(command, work) == result
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        assert (
            await work.session.get(SubscriptionSchedulerRun, command.run_id)
        ).candidate_state == "closed"
        run = await work.runs.get(command.run_id)
        assert run.remote_remediation_count == 1 and run.local_remediation_count == 0
        # Resolve the disposable intervention before exercising guarded downgrades.
        await work.runs.transition(run.id, run.version, RunState.CANCELLED, "test.cleanup", {})
        await work.commit()
