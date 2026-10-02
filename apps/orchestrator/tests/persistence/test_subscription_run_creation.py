"""Selected routing becomes immutable in the run-creation transaction."""

import asyncio
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
    from forge.application.services.runs import RunProfileSelectionError

    with pytest.raises(RunCreationError, match="profile") as exc_info:
        await service.create_run(actor=actor, idempotency_key="invalid-profile", task_id=task_id)
    assert isinstance(exc_info.value, RunProfileSelectionError)
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
async def test_missing_explicit_profile_rolls_back_run_command_and_receipt(session_factory, tmp_path):
    from forge.persistence.models import ApiMutation, Run, RunCommand, SubscriptionEnvelope
    from forge.persistence.models.execution import RunEvent as RunEventRecord
    from forge.persistence.repositories.runs import RunCreationError
    from sqlalchemy import func, select

    actor, _project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    service = RunService(
        lambda: PostgresUnitOfWork(session_factory),
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    )
    from forge.application.services.runs import RunProfileSelectionError

    with pytest.raises(RunCreationError) as exc_info:
        await service.create_run(
            actor=actor, idempotency_key="missing-override", task_id=task_id,
            profile_id=uuid4(), profile_version=1,
        )
    assert isinstance(exc_info.value, RunProfileSelectionError)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(RunCommand)) == 0
        assert await session.scalar(select(func.count()).select_from(RunEventRecord)) == 0
        assert await session.scalar(select(func.count()).select_from(SubscriptionEnvelope)) == 0
        assert await session.scalar(
            select(func.count()).select_from(ApiMutation).where(ApiMutation.action == "create_run")
        ) == 0


@pytest.mark.integration
@pytest.mark.parametrize("corruption", ["malformed", "unsupported", "identity", "version"])
async def test_corrupt_explicit_profile_rolls_back_every_run_creation_record(
    session_factory, tmp_path, corruption
):
    import json

    from forge.domain.subscription import encode_subscription_record
    from forge.persistence.models import ApiMutation, Run, RunCommand, SubscriptionEnvelope
    from forge.persistence.models.execution import RunEvent as RunEventRecord
    from forge.persistence.repositories.runs import RunCreationError
    from sqlalchemy import func, select, text

    actor, _project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    profile = OperatorProfile(
        profile_id=uuid4(), version=1,
        preferences=(RolePreference(
            purpose=SpecialistPurpose.PRIMARY, preferred_route=_route("openai")
        ),),
    )
    payload = encode_subscription_record(profile)
    if corruption == "malformed":
        payload = {}
    elif corruption == "unsupported":
        payload["schema_version"] = 999
    elif corruption == "identity":
        payload["record"]["fields"][0][1]["$uuid"] = str(uuid4())
    else:
        payload["record"]["fields"][1][1] = 2
    async with session_factory() as session:
        from forge.persistence.repositories.subscription import PostgresSubscriptionRepository

        await PostgresSubscriptionRepository(session).store_profile(profile)
        await session.execute(
            text("UPDATE subscription_profile_versions SET payload = CAST(:payload AS jsonb) WHERE profile_id = :id"),
            {"id": profile.profile_id, "payload": json.dumps(payload)},
        )
        await session.commit()
    service = RunService(
        lambda: PostgresUnitOfWork(session_factory),
        repository_inspector=StableInspector(tmp_path / "repo"), data_root=tmp_path / "data",
    )
    from forge.application.services.runs import RunProfileSelectionError

    with pytest.raises(RunCreationError) as exc_info:
        await service.create_run(
            actor=actor, idempotency_key="corrupt-override", task_id=task_id,
            profile_id=profile.profile_id, profile_version=1,
        )
    assert isinstance(exc_info.value, RunProfileSelectionError)
    async with session_factory() as session:
        for model in (Run, RunCommand, RunEventRecord, SubscriptionEnvelope):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert await session.scalar(
            select(func.count()).select_from(ApiMutation).where(ApiMutation.action == "create_run")
        ) == 0


@pytest.mark.integration
async def test_project_default_profile_decoder_error_keeps_its_original_type(session_factory, tmp_path):
    from sqlalchemy import text

    _actor, project_id, _task_id = await _seed_project_task(session_factory, tmp_path)
    profile = OperatorProfile(profile_id=uuid4(), version=1, preferences=())
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.store_profile(profile)
        await work.subscription.select_project_profile(project_id, profile)
        await work.commit()
    async with session_factory() as session:
        await session.execute(
            text("UPDATE subscription_profile_versions SET payload = '{}'::jsonb WHERE profile_id = :id"),
            {"id": profile.profile_id},
        )
        await session.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(ValueError) as caught:
            await work.subscription.project_profile(project_id)
        assert type(caught.value) is ValueError


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


@pytest.mark.integration
async def test_run_override_freezes_requested_profile_without_changing_project_default(
    session_factory, tmp_path
):
    from forge.persistence.repositories.mutations import MutationConflict

    actor, project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    default = OperatorProfile(
        profile_id=uuid4(), version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=_route("openai")),),
    )
    override = OperatorProfile(
        profile_id=uuid4(), version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=_route("anthropic")),),
    )
    other = OperatorProfile(
        profile_id=uuid4(), version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=_route("google")),),
    )
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        await work.subscription.select_project_profile(project_id, default)
        await work.subscription.store_profile(override)
        await work.subscription.store_profile(other)
        await work.commit()
    service = RunService(
        factory, repository_inspector=StableInspector(tmp_path / "repo"), data_root=tmp_path / "data"
    )
    run = await service.create_run(
        actor=actor, idempotency_key="run-profile-override", task_id=task_id,
        profile_id=override.profile_id, profile_version=override.version,
    )
    async with factory() as work:
        envelope = await work.subscription.envelope_for_run(run.id)
        selected = await work.subscription.project_profile(project_id)
        events = await work.events.list_after(run.id, 0)
        assert envelope is not None
        assert envelope.profile_id == override.profile_id
        assert selected == default
        created = next(event for event in events if event.event_type == "run.created")
        assert created.payload["subscription_profile_selection_source"] == "run_override"
        await work.commit()
    assert await service.create_run(
        actor=actor, idempotency_key="run-profile-override", task_id=task_id,
        profile_id=override.profile_id, profile_version=override.version,
    ) == run
    with pytest.raises(MutationConflict):
        await service.create_run(actor=actor, idempotency_key="run-profile-override", task_id=task_id)
    first, second = await asyncio.gather(
        service.create_run(
            actor=actor, idempotency_key="concurrent-profile-a", task_id=task_id,
            profile_id=override.profile_id, profile_version=1,
        ),
        service.create_run(
            actor=actor, idempotency_key="concurrent-profile-b", task_id=task_id,
            profile_id=other.profile_id, profile_version=1,
        ),
    )
    async with factory() as work:
        first_envelope = await work.subscription.envelope_for_run(first.id)
        second_envelope = await work.subscription.envelope_for_run(second.id)
        assert first_envelope is not None and first_envelope.profile_id == override.profile_id
        assert second_envelope is not None and second_envelope.profile_id == other.profile_id
        assert await work.subscription.project_profile(project_id) == default
        await work.commit()


@pytest.mark.integration
async def test_profile_selection_batch_reads_frozen_headers_and_creation_marker_only(
    session_factory, tmp_path
):
    from sqlalchemy import text

    actor, _project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    factory = lambda: PostgresUnitOfWork(session_factory)
    profiles = [
        OperatorProfile(
            profile_id=uuid4(), version=1,
            preferences=(RolePreference(
                purpose=SpecialistPurpose.PRIMARY, preferred_route=_route("openai")
            ),),
        )
        for _ in range(2)
    ]
    async with factory() as work:
        for profile in profiles:
            await work.subscription.store_profile(profile)
        await work.commit()
    service = RunService(
        factory, repository_inspector=StableInspector(tmp_path / "repo"), data_root=tmp_path / "data"
    )
    selected = [
        await service.create_run(
            actor=actor, idempotency_key=f"batch-profile-{i}", task_id=task_id,
            profile_id=profile.profile_id, profile_version=profile.version,
        )
        for i, profile in enumerate(profiles)
    ]
    legacy = await service.create_run(actor=actor, idempotency_key="batch-profile-legacy", task_id=task_id)
    async with session_factory() as session:
        # A historical creation event without the optional marker remains unknown.
        await session.execute(
            text("UPDATE run_events SET payload = payload - 'subscription_profile_selection_source' WHERE run_id = :id AND sequence = 1"),
            {"id": selected[0].id},
        )
        # Display reads only the frozen envelope header and treats non-string provenance as unknown.
        await session.execute(
            text("UPDATE run_events SET payload = jsonb_set(payload, '{subscription_profile_selection_source}', '[]'::jsonb) WHERE run_id = :id AND sequence = 1"),
            {"id": selected[1].id},
        )
        await session.execute(
            text("UPDATE subscription_envelopes SET payload = '{}'::jsonb WHERE run_id = :id"),
            {"id": selected[1].id},
        )
        # Later events are outside this read path, even if their payload is malformed.
        await session.execute(
            text("INSERT INTO run_events (id, run_id, sequence, run_version, event_type, actor_class, payload_schema_version, payload, occurred_at) VALUES (gen_random_uuid(), :id, 2, 0, 'unrelated', 'system', 1, '[]'::jsonb, now())"),
            {"id": selected[1].id},
        )
        await session.commit()
    result = await service.profile_selections(tuple(run.id for run in [*selected, legacy]))
    assert result[selected[0].id]["profile_id"] == profiles[0].profile_id
    assert result[selected[0].id]["selection_source"] is None
    assert result[selected[1].id]["profile_id"] == profiles[1].profile_id
    assert result[selected[1].id]["selection_source"] is None
    assert legacy.id not in result
    assert await service.profile_selection(legacy.id) is None
