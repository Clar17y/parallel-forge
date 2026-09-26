"""Base update effects and paused deliveries retain one bounded primary repair."""

from datetime import UTC, datetime, timedelta

import pytest
from forge.application.services.subscription_base_update import ADMISSION_EVENT, EVENT
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from forge.persistence.repositories.commands import CommandLeaseLost
from forge.release.fake_github_write import FakeGitHubWriteCrash
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_base_adoption import base_adoption_case
from test_subscription_resume_controls import pause_and_resume


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["update", "adopt"])
async def test_base_adoption_recovers_effect_response_without_repeating_repair(
    session_factory, tmp_path, stage
):
    case = await base_adoption_case(session_factory, tmp_path)
    if stage == "update":
        update = case.writes.update_branch

        async def lost(*args):
            await update(*args)
            raise FakeGitHubWriteCrash()

        case.writes.update_branch = lost
    else:
        adoption = case.service._adoption(case.original.policy)

        class LostAdoption:
            async def adopt(self, *args):
                await adoption.adopt(*args)
                raise FakeGitHubWriteCrash()

            async def inspect(self, *args):
                await adoption.inspect(*args)

        case.service._adoption = lambda _: LostAdoption()
    async with case.factory() as work:
        with pytest.raises(FakeGitHubWriteCrash):
            await case.service.execute(case.command, work)
    async with case.factory() as work:
        scheduled = await work.session.get(
            SubscriptionScheduledTask, case.original.decision.task_id
        )
        assert scheduled.state == "blocked" and scheduled.repairs == 0
        assert await work.session.get(SubscriptionRepairDebit, case.original.attempt_id) is not None
        assert (
            await work.session.get(SubscriptionSchedulerRun, case.command.run_id)
        ).candidate_state == "closed"
    for _ in range(2):
        async with case.factory() as work:
            await case.service.execute(case.command, work)
    async with case.factory() as work:
        scheduled = await work.session.get(
            SubscriptionScheduledTask, case.original.decision.task_id
        )
        assert scheduled.state == "queued" and scheduled.repairs == 1
    assert case.updates == [case.original.review.candidate.head_sha]
    assert case.adoptions == [case.new_head]


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["before", "committed", "acknowledged"])
async def test_base_adoption_pause_resume_retains_original_command_and_budget(
    session_factory, tmp_path, stage
):
    case = await base_adoption_case(session_factory, tmp_path)
    if stage != "before":
        async with case.factory() as work:
            await case.service.execute(case.command, work)
    if stage == "acknowledged":
        await case.commands.complete(case.command.id, worker_id=case.command.lease_owner)
    continued = await pause_and_resume(
        case.factory,
        session_factory,
        case.command.run_id,
        case.dispatch._store,
        source=None if stage == "acknowledged" else case.command,
        repairs=case.repairs,
    )
    if stage == "before":
        assert continued is not None and continued.command_type == "update_base"
        async with case.factory() as work:
            await case.service.execute(continued, work)
    else:
        assert continued is None
    async with case.factory() as work:
        assert (await work.runs.get(case.command.run_id)).state is RunState.REMEDIATING
        scheduled = await work.session.get(
            SubscriptionScheduledTask, case.original.decision.task_id
        )
        assert scheduled.state == "queued" and scheduled.repairs == 1
    assert case.updates == [case.original.review.candidate.head_sha]
    assert case.adoptions == [case.new_head]


@pytest.mark.integration
@pytest.mark.parametrize("event_type", [ADMISSION_EVENT, EVENT])
async def test_base_adoption_fences_final_lease_and_recovers_only_committed_effects(
    session_factory, tmp_path, monkeypatch, event_type
):
    case = await base_adoption_case(session_factory, tmp_path)
    async with case.factory() as work:
        append = work.events.append

        async def expire(event):
            result = await append(event)
            if event.event_type == event_type:
                (await work.session.get(RunCommand, case.command.id)).lease_expires_at = (
                    datetime.now(UTC) - timedelta(seconds=1)
                )
                await work.session.flush()
            return result

        monkeypatch.setattr(work.events, "append", expire)
        with pytest.raises(CommandLeaseLost):
            await case.service.execute(case.command, work)
    async with case.factory() as work:
        scheduled = await work.session.get(
            SubscriptionScheduledTask, case.original.decision.task_id
        )
        assert scheduled.state == "blocked" and scheduled.repairs == 0
        debit = await work.session.get(SubscriptionRepairDebit, case.original.attempt_id)
        assert (debit is None) is (event_type == ADMISSION_EVENT)
        assert not [
            event
            for event in await work.events.list_after(case.command.run_id, 0)
            if event.event_type == EVENT
        ]
    async with case.factory() as work:
        await case.service.execute(case.command, work)
    assert case.updates == [case.original.review.candidate.head_sha]
    assert case.adoptions == [case.new_head]
