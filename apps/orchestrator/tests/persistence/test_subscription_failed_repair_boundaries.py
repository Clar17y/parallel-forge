"""Resume cannot acknowledge damaged repair evidence or commit past its lease."""

from datetime import timedelta

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.subscription_remote_remediation import (
    SubscriptionRemoteRemediationController,
)
from forge.domain.run import RunState
from forge.persistence.models import RunEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_committed_repair_resume import failed_repair_case
from test_subscription_remote_remediation import remote_failure_case
from test_subscription_resume_controls import pause_for_resume


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["remote", "base"])
@pytest.mark.parametrize("changed", ["receipt", "missing_receipt", "debit", "lease"])
async def test_failed_repair_acknowledgment_is_atomic_and_source_bound(
    session_factory, tmp_path, monkeypatch, kind, changed
):
    case = await failed_repair_case(session_factory, tmp_path, kind)
    _, resume = await pause_for_resume(case.factory, session_factory, case.command.run_id)
    async with case.factory() as work:
        event = await work.session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == case.command.run_id,
                RunEvent.event_type
                == (
                    "run.subscription_base_adopted"
                    if kind == "base"
                    else "run.subscription_remote_repair_requested"
                ),
            )
        )
        if changed == "receipt":
            event.payload = dict(event.payload) | {"extra": True}
        elif changed == "missing_receipt":
            await work.session.delete(event)
        elif changed == "debit":
            await work.session.delete(
                await work.session.get(
                    SubscriptionRepairDebit,
                    case.original.attempt_id,
                )
            )
        await work.commit()
    with pytest.raises((CommandRecoveryRequired, SubscriptionDecisionError, CommandLeaseLost)):
        async with case.factory() as work:
            if changed == "lease":
                original = work.runs.resume

                async def expire_before_final_fence(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    monkeypatch.setattr(
                        "forge.persistence.repositories.commands._utc_now",
                        lambda: resume.lease_expires_at + timedelta(seconds=1),
                    )
                    return result

                work.runs.resume = expire_before_final_fence
            await ResumeRunHandler(
                artifact_store=case.dispatch._store,
                subscription_remote_repairs=case.repairs,
            )(resume, work)
    async with case.factory() as work:
        assert (await work.runs.get(case.command.run_id)).state is RunState.PAUSED
        assert await work.commands.get(case.command.id) == case.failed
        scheduled = await work.session.get(
            SubscriptionScheduledTask, case.original.decision.task_id
        )
        assert scheduled.state == "queued" and scheduled.repairs == 1
        assert not [
            event
            for event in await work.events.list_after(case.command.run_id, 0)
            if event.event_type == "run.resumed"
        ]


@pytest.mark.integration
async def test_paused_source_inspection_cannot_authorize_a_repair(session_factory, tmp_path):
    factory, original, dispatch, validator, commands, _, command, _, _ = await remote_failure_case(
        session_factory, tmp_path
    )
    repairs = SubscriptionRemoteRemediationController(
        dispatch._store, validator, validator._git_factory
    )
    failed = await commands.fail(
        command.id, worker_id=command.lease_owner, error="unadmitted failure"
    )
    await pause_for_resume(factory, session_factory, command.run_id)
    async with factory() as work:
        approved = await ApprovedPlanLoader(dispatch._store).load(work, command.run_id)
        source, evidence, feedback = await repairs._source(failed, work, approved)
        proposal, _ = await work.subscription_decisions.acceptance_remote_source(
            source.attempt_id, allow_paused=True
        )
        from forge.domain.approval import canonical_digest

        with pytest.raises(SubscriptionDecisionError):
            await work.subscription_decisions.reopen_acceptance_remote(
                proposal, failed.id, canonical_digest(evidence), feedback
            )
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, original.decision.task_id)
        assert scheduled.state == "blocked" and scheduled.repairs == 0
