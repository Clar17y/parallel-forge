"""A paused base delivery resumes only after its admitted effect is reconciled."""

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.recovery import RecoveryService
from forge.domain.run import RunState
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.release.fake_github_write import FakeGitHubWriteCrash
from forge.worker.base_recovery import base_recovery_adapters
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_base_adoption import base_adoption_case
from test_subscription_resume_controls import pause_for_resume


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["update", "adopt"])
async def test_paused_admitted_base_effect_reconciles_before_resumed_delivery(
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
        original = case.service._adoption(case.original.policy)

        class Adoption:
            async def adopt(self, *args):
                await original.adopt(*args)
                raise FakeGitHubWriteCrash()

            async def inspect(self, *args):
                await original.inspect(*args)

        case.service._adoption = lambda _: Adoption()
    async with case.factory() as work:
        with pytest.raises(FakeGitHubWriteCrash):
            await case.service.execute(case.command, work)
    commands, resume = await pause_for_resume(
        case.factory, session_factory, case.command.run_id, source=case.command
    )
    handler = ResumeRunHandler(
        artifact_store=case.dispatch._store, subscription_remote_repairs=case.repairs
    )
    async with case.factory() as work:
        with pytest.raises(CommandRecoveryRequired):
            await handler(resume, work)
    operations = PostgresOperationRepository(session_factory)
    recovered = await RecoveryService(operations).reconcile_all(
        base_recovery_adapters(
            session_factory,
            case.dispatch._store,
            case.validator,
            case.reads,
            case.writes,
            case.service._adoption,
        )
    )
    assert len(recovered) == 1
    for _ in range(2):
        async with case.factory() as work:
            await handler(resume, work)
    await commands.complete(resume.id, worker_id=resume.lease_owner)
    continued = await commands.claim_next(worker_id="recovered-base", lease_seconds=120)
    assert continued is not None and continued.command_type == "update_base"
    async with case.factory() as work:
        await case.service.execute(continued, work)
    async with case.factory() as work:
        assert (await work.runs.get(case.command.run_id)).state is RunState.REMEDIATING
        task = await work.session.get(SubscriptionScheduledTask, case.original.decision.task_id)
        assert task.state == "queued" and task.repairs == 1
    assert case.updates == [case.original.review.candidate.head_sha]
    assert case.adoptions == [case.new_head]
