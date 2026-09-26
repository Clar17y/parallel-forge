"""Selected routing becomes immutable in the run-creation transaction."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.services.runs import RunService
from forge.domain.subscription import OperatorProfile, RolePreference, SpecialistPurpose
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import _remove_disposable_subscription_rows, _route  # noqa: F401
from test_task10_run_service_integration import StableInspector, _seed_project_task


@pytest.mark.integration
async def test_invalid_selected_profile_rolls_back_run_command_and_receipt(
    session_factory, tmp_path
):
    from forge.persistence.models import ApiMutation, Run, RunCommand
    from forge.persistence.repositories.runs import RunCreationError
    from sqlalchemy import func, select

    actor, project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        await work.subscription.select_project_profile(
            project_id, OperatorProfile(profile_id=uuid4(), version=1, preferences=())
        )
        await work.commit()
    service = RunService(
        factory,
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    )
    with pytest.raises(RunCreationError, match="profile"):
        await service.create_run(actor=actor, idempotency_key="invalid-profile", task_id=task_id)
    async with factory() as work:
        assert (
            await work.session.scalar(
                select(func.count()).select_from(Run).where(Run.task_id == task_id)
            )
            == 0
        )
        assert await work.session.scalar(select(func.count()).select_from(RunCommand)) == 0
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(ApiMutation)
                .where(ApiMutation.action == "create_run")
            )
            == 0
        )


@pytest.mark.integration
async def test_run_freezes_selected_profile_and_replay_ignores_later_selection(
    session_factory, tmp_path
):
    actor, project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    selected = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(
            RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=_route("openai")),
        ),
    )
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        await work.subscription.select_project_profile(project_id, selected)
        await work.commit()
    service = RunService(
        factory,
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    )
    run = await service.create_run(actor=actor, idempotency_key="subscription-run", task_id=task_id)
    async with factory() as work:
        envelope = await work.subscription.envelope_for_run(run.id)
        assert envelope is not None
        assert (envelope.profile_id, envelope.profile_version, envelope.safety_policy_version) == (
            selected.profile_id,
            1,
            run.policy_version,
        )
        assert (
            envelope.route_for(SpecialistPurpose.PRIMARY).effective
            == selected.preferences[0].preferred_route
        )
        await work.subscription.select_project_profile(project_id, replace(selected, version=2))
        await work.commit()
    assert (
        await service.create_run(actor=actor, idempotency_key="subscription-run", task_id=task_id)
        == run
    )
    async with factory() as work:
        assert await work.subscription.envelope_for_run(run.id) == envelope
        command = await work.commands.get_by_idempotency_key(f"{run.id}:start-planning")
        assert command.command_type == "start_planning"
