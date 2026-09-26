"""A leased start command atomically queues one frozen subscription primary."""

from uuid import uuid4

import pytest
from forge.application.services.runs import RunService
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.run import RunState
from forge.domain.subscription import OperatorProfile, RolePreference, SpecialistPurpose, TaskBudget
from forge.domain.tool import repository_resource_identity
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import _remove_disposable_subscription_rows, _route  # noqa: F401
from test_subscription_usage import _reservation
from test_task10_run_service_integration import StableInspector, _seed_project_task


async def planning_start_case(session_factory, tmp_path):
    actor, project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    factory = lambda: PostgresUnitOfWork(session_factory)
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(
            RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=_route("openai")),
        ),
    )
    async with factory() as work:
        await work.subscription.select_project_profile(project_id, profile)
        await work.commit()
    run = await RunService(
        factory,
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    ).create_run(actor=actor, idempotency_key="subscription-start", task_id=task_id)
    from forge.persistence.repositories.commands import PostgresCommandRepository

    command = await PostgresCommandRepository(session_factory).claim_next(
        worker_id="planning-owner", lease_seconds=60
    )
    assert command is not None and command.run_id == run.id
    return factory, run, command


@pytest.mark.integration
async def test_start_subscription_planning_queues_primary_once(session_factory, tmp_path):
    from forge.application.services.subscription_planning import SubscriptionPlanningService

    factory, original, command = await planning_start_case(session_factory, tmp_path)
    service = SubscriptionPlanningService(TaskBudget(max_provider_attempts=64))
    from types import SimpleNamespace

    from forge.application.handlers.planning import PlanningHandler

    async def legacy(*args):
        raise AssertionError("subscription run must not invoke legacy planning")

    handler = PlanningHandler(SimpleNamespace(execute=legacy), subscription_service=service)
    async with factory() as work:
        primary_id = await handler(command, work)
    async with factory() as work:
        assert await service.execute(command, work) == primary_id
        run = await work.runs.get(original.id)
        assert run.state is RunState.PLANNING and run.version == 1
        assert run.worktree_path is None
    admission = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert admission is not None and admission.task.task_id == primary_id
    assert admission.task.purpose is SpecialistPurpose.PRIMARY
    assert admission.task.owned_paths == ()
    assert admission.task.budget.max_provider_attempts == 64
    async with factory() as work:
        from forge.persistence.models.scheduling import SubscriptionScheduledTask

        row = await work.session.get(SubscriptionScheduledTask, primary_id)
        assert row.worktree_id == repository_resource_identity(original.project_id)
        # A worker configuration change cannot rewrite the admitted run budget.
        changed_config = SubscriptionPlanningService(TaskBudget(max_provider_attempts=1))
        assert await changed_config.execute(command, work) == primary_id
        assert (
            await work.subscription.get_task(original.id, primary_id)
        ).budget.max_provider_attempts == 64


@pytest.mark.integration
@pytest.mark.parametrize("change", ["payload", "paused", "expired", "pending_cancel"])
async def test_start_subscription_planning_respects_command_and_control_fences(
    session_factory, tmp_path, change
):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from forge.application.services.auth import AuthenticatedActor
    from forge.application.services.runs import RunCommandRequest, RunCommandService
    from forge.application.services.subscription_planning import SubscriptionPlanningService
    from forge.persistence.models import RunCommand
    from forge.persistence.models.subscription import SubscriptionTask
    from sqlalchemy import func, select

    factory, run, command = await planning_start_case(session_factory, tmp_path)
    if change == "payload":
        command = replace(command, payload={"unexpected": True})
    elif change == "pending_cancel":
        await RunCommandService(factory).enqueue(
            actor=AuthenticatedActor(
                actor_id=command.actor_id, actor_class="operator", session_id=uuid4()
            ),
            run_id=run.id,
            idempotency_key="cancel-before-plan",
            request=RunCommandRequest(command_type="cancel", expected_run_version=run.version),
        )
    else:
        async with factory() as work:
            if change == "paused":
                await work.runs.pause(run.id, run.version, "run.paused", {})
            else:
                row = await work.session.get(RunCommand, command.id)
                row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await work.commit()
    from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired

    with pytest.raises((CommandRecoveryRequired, CommandLeaseLost)):
        async with factory() as work:
            await SubscriptionPlanningService(TaskBudget()).execute(command, work)
    async with factory() as work:
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(SubscriptionTask)
                .where(SubscriptionTask.run_id == run.id)
            )
            == 0
        )


@pytest.mark.integration
async def test_start_planning_rolls_back_primary_if_transition_fails(
    session_factory, tmp_path, monkeypatch
):
    from forge.application.services.subscription_planning import SubscriptionPlanningService
    from forge.persistence.models.scheduling import (
        SubscriptionScheduledTask,
        SubscriptionSchedulerRun,
    )
    from forge.persistence.models.subscription import SubscriptionTask
    from forge.persistence.repositories.runs import PostgresRunRepository
    from sqlalchemy import func, select

    factory, run, command = await planning_start_case(session_factory, tmp_path)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected transition failure")

    monkeypatch.setattr(PostgresRunRepository, "transition", fail)
    with pytest.raises(RuntimeError, match="injected"):
        async with factory() as work:
            await SubscriptionPlanningService(TaskBudget()).execute(command, work)
    async with factory() as work:
        assert (await work.runs.get(run.id)).state is RunState.CREATED
        assert await work.session.get(SubscriptionSchedulerRun, run.id) is None
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(SubscriptionTask)
                .where(SubscriptionTask.run_id == run.id)
            )
            == 0
        )
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.run_id == run.id)
            )
            == 0
        )


@pytest.mark.integration
async def test_start_replay_rejects_changed_primary_budget(session_factory, tmp_path):
    from dataclasses import replace

    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.application.services.subscription_planning import SubscriptionPlanningService
    from forge.domain.subscription import encode_subscription_record
    from forge.persistence.models.subscription import SubscriptionTask

    factory, run, command = await planning_start_case(session_factory, tmp_path)
    service = SubscriptionPlanningService(TaskBudget(max_provider_attempts=64))
    async with factory() as work:
        primary_id = await service.execute(command, work)
    async with factory() as work:
        current = await work.subscription.get_task(run.id, primary_id)
        row = await work.session.get(SubscriptionTask, primary_id)
        row.payload = encode_subscription_record(replace(current, budget=TaskBudget()))
        await work.commit()
    with pytest.raises(CommandRecoveryRequired):
        async with factory() as work:
            await service.execute(command, work)
