"""Integration coverage for the run and event persistence boundary."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.services.state_engine import StateEngine
from forge.domain.event import RunEvent, thaw_payload
from forge.domain.run import RunSnapshot, RunState, SuspensionContext, SuspensionKind
from forge.persistence.models import Project, ProjectPolicyVersion, Run, Task
from forge.persistence.repositories.events import InvalidEventCursor
from forge.persistence.repositories.runs import (
    PersistenceDataError,
    PostgresRunRepository,
    RunCreationError,
    RunNotFound,
)
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError


@pytest.mark.integration
async def test_create_and_get_snapshot_round_trip(persisted_run, uow) -> None:
    """A new run persists as CREATED and can be loaded without losing fields."""

    assert isinstance(persisted_run, RunSnapshot)
    async with uow:
        loaded = await uow.runs.get(persisted_run.id)

    assert loaded == persisted_run
    assert loaded.state is RunState.CREATED
    assert PostgresRunRepository is not None


@pytest.mark.integration
async def test_create_snapshots_current_policy_version_and_schema_defaults(
    persisted_run, session_factory, uow
) -> None:
    async with session_factory() as session, session.begin():
        policy = ProjectPolicyVersion(
            project_id=persisted_run.project_id,
            version=2,
            policy_digest="c" * 64,
            document_schema_version=1,
            document={"version": 2},
        )
        session.add(policy)
        await session.flush()
        project = await session.get(Project, persisted_run.project_id)
        assert project is not None
        project.current_policy_version = 2

    new_run = RunSnapshot(
        id=uuid4(), project_id=persisted_run.project_id, task_id=persisted_run.task_id
    )
    async with uow:
        await uow.runs.create(new_run)
        await uow.commit()

    async with session_factory() as session:
        record = await session.get(Run, new_run.id)
    assert record is not None
    assert record.policy_version == 2
    assert record.token_budget == 0
    assert record.cost_budget_minor == 0
    assert record.duration_budget_seconds == 0
    assert record.database_state == "DISABLED"
    assert record.database_name is None
    assert record.database_role is None
    assert record.secret_id is None


@pytest.mark.integration
async def test_create_refreshes_current_policy_after_external_update(
    session_factory, uow, persisted_run
) -> None:
    first = RunSnapshot(
        id=uuid4(), project_id=persisted_run.project_id, task_id=persisted_run.task_id
    )
    second = RunSnapshot(
        id=uuid4(), project_id=persisted_run.project_id, task_id=persisted_run.task_id
    )
    async with uow:
        await uow.runs.create(first)
        await uow.commit()
        cached_project = await uow.session.get(Project, persisted_run.project_id)
        assert cached_project is not None
        assert cached_project.current_policy_version == 1

        async with session_factory() as external, external.begin():
            external.add(
                ProjectPolicyVersion(
                    project_id=persisted_run.project_id,
                    version=2,
                    policy_digest="f" * 64,
                    document_schema_version=1,
                    document={"version": 2},
                )
            )
            await external.flush()
            project = await external.get(Project, persisted_run.project_id)
            assert project is not None
            project.current_policy_version = 2

        assert cached_project.current_policy_version == 1
        await uow.runs.create(second)
        await uow.commit()

    async with session_factory() as session:
        first_record = await session.get(Run, first.id)
        second_record = await session.get(Run, second.id)
    assert first_record is not None
    assert second_record is not None
    assert first_record.policy_version == 1
    assert second_record.policy_version == 2


@pytest.mark.integration
@pytest.mark.parametrize(
    "snapshot",
    [
        lambda run: RunSnapshot(
            id=uuid4(), project_id=run.project_id, task_id=run.task_id, state=RunState.PLANNING
        ),
        lambda run: RunSnapshot(
            id=uuid4(), project_id=run.project_id, task_id=run.task_id, version=1
        ),
    ],
)
async def test_create_rejects_non_new_snapshot_without_writing(
    snapshot, persisted_run, uow
) -> None:
    candidate = snapshot(persisted_run)
    with pytest.raises(RunCreationError):
        async with uow:
            await uow.runs.create(candidate)

    with pytest.raises(RunNotFound):
        async with uow:
            await uow.runs.get(candidate.id)


@pytest.mark.integration
async def test_create_rejects_missing_project_without_writing(uow) -> None:
    candidate = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4())
    with pytest.raises(RunCreationError):
        async with uow:
            await uow.runs.create(candidate)
    with pytest.raises(RunNotFound):
        async with uow:
            await uow.runs.get(candidate.id)


@pytest.mark.integration
async def test_create_rejects_project_without_current_policy_without_writing(
    session_factory, uow
) -> None:
    project_id = uuid4()
    task_id = uuid4()
    candidate = RunSnapshot(id=uuid4(), project_id=project_id, task_id=task_id)
    async with session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/forge-{project_id}",
                github_repository=f"Clar17y/forge-{project_id}",
                default_branch="main",
            )
        )
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                normalized_text="task without policy",
                task_digest="1" * 64,
            )
        )

    with pytest.raises(RunCreationError, match="current policy"):
        async with uow:
            await uow.runs.create(candidate)
    with pytest.raises(RunNotFound):
        async with uow:
            await uow.runs.get(candidate.id)


@pytest.mark.integration
async def test_policy_foreign_key_prevents_dangling_current_policy(
    session_factory, persisted_run
) -> None:
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="append-only|foreign key"):
            async with session.begin():
                await session.execute(
                    delete(ProjectPolicyVersion).where(
                        ProjectPolicyVersion.project_id == persisted_run.project_id,
                        ProjectPolicyVersion.version == 1,
                    )
                )


@pytest.mark.integration
async def test_create_rejects_task_from_another_project(
    session_factory, uow, persisted_run
) -> None:
    other_project_id = uuid4()
    other_task_id = uuid4()
    async with session_factory() as session, session.begin():
        project = Project(
            id=other_project_id,
            canonical_path=f"/tmp/forge-{other_project_id}",
            github_repository=f"Clar17y/forge-{other_project_id}",
            default_branch="main",
        )
        policy = ProjectPolicyVersion(
            project_id=other_project_id,
            version=1,
            policy_digest="d" * 64,
            document_schema_version=1,
            document={},
        )
        task = Task(
            id=other_task_id,
            project_id=other_project_id,
            normalized_text="other task",
            task_digest="e" * 64,
        )
        session.add_all([project, policy, task])
        await session.flush()
        project.current_policy_version = 1

    candidate = RunSnapshot(id=uuid4(), project_id=persisted_run.project_id, task_id=other_task_id)
    with pytest.raises(RunCreationError):
        async with uow:
            await uow.runs.create(candidate)
    with pytest.raises(RunNotFound):
        async with uow:
            await uow.runs.get(candidate.id)


@pytest.mark.integration
async def test_get_rejects_unknown_suspension_context_version(
    migrated_database_url, persisted_run
) -> None:
    engine_url = migrated_database_url
    from forge.persistence.database import create_engine

    engine = create_engine(engine_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE runs SET state = 'PAUSED', suspended_state = 'PLANNING', "
                    "suspension_kind = 'PAUSE', suspension_context_schema_version = 2, "
                    "suspension_context = CAST(:context AS jsonb) WHERE id = :run_id"
                ),
                {
                    "context": '{"state":"PLANNING","suspended_state":null,"suspension_kind":null}',
                    "run_id": persisted_run.id,
                },
            )
        from forge.persistence.database import create_session_factory

        factory = create_session_factory(engine)
        from forge.persistence.unit_of_work import PostgresUnitOfWork

        with pytest.raises(PersistenceDataError):
            async with PostgresUnitOfWork(factory) as work:
                await work.runs.get(persisted_run.id)
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    "context",
    [
        '{"state":"PLANNING"}',
        '{"state":"NOT_A_STATE","suspended_state":null,"suspension_kind":null}',
        '{"state":"PLANNING","suspended_state":null,"suspension_kind":"NOT_A_KIND"}',
    ],
)
async def test_get_rejects_malformed_suspension_context(
    session_factory, persisted_run, context
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE runs SET state = 'PAUSED', suspended_state = 'PLANNING', "
                "suspension_kind = 'PAUSE', suspension_context_schema_version = 1, "
                "suspension_context = CAST(:context AS jsonb) WHERE id = :run_id"
            ),
            {"context": context, "run_id": persisted_run.id},
        )

    from forge.persistence.unit_of_work import PostgresUnitOfWork

    with pytest.raises(PersistenceDataError):
        async with PostgresUnitOfWork(session_factory) as work:
            await work.runs.get(persisted_run.id)


@pytest.mark.integration
async def test_get_rejects_malformed_persisted_state(uow, monkeypatch) -> None:
    record = Run(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        policy_version=1,
        state="NOT_A_STATE",
        version=0,
        local_remediation_count=0,
        remote_remediation_count=0,
        token_budget=0,
        cost_budget_minor=0,
        duration_budget_seconds=0,
        database_state="DISABLED",
    )
    async with uow:

        async def fake_get(_model, _run_id):
            return record

        monkeypatch.setattr(uow.session, "get", fake_get)
        with pytest.raises(PersistenceDataError, match="unknown state"):
            await uow.runs.get(record.id)


@pytest.mark.integration
async def test_nested_pause_context_round_trips(session_factory, persisted_run) -> None:
    engine = StateEngine()
    planning = engine.transition(persisted_run, RunState.PLANNING)
    intervened = engine.intervene(planning)
    paused = engine.pause(intervened)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE runs SET state = :state, version = :version, "
                "suspended_state = :suspended_state, suspension_kind = :kind, "
                "suspension_context_schema_version = 1, suspension_context = CAST(:context AS jsonb) "
                "WHERE id = :run_id"
            ),
            {
                "state": paused.state.value,
                "version": paused.version,
                "suspended_state": paused.suspended_state.value,
                "kind": paused.suspension_kind.value,
                "context": '{"state":"AWAITING_HUMAN_INTERVENTION","suspended_state":"PLANNING","suspension_kind":"INTERVENTION"}',
                "run_id": persisted_run.id,
            },
        )
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    async with PostgresUnitOfWork(session_factory) as work:
        loaded = await work.runs.get(persisted_run.id)
    assert loaded == paused
    assert loaded.suspension_context == SuspensionContext(
        state=RunState.AWAITING_HUMAN_INTERVENTION,
        suspended_state=RunState.PLANNING,
        suspension_kind=SuspensionKind.INTERVENTION,
    )


@pytest.mark.integration
async def test_event_actor_payload_timestamp_and_immutability(uow, persisted_run) -> None:
    occurred_at = datetime(2026, 8, 22, 2, 0, tzinfo=UTC)
    source = {"nested": {"value": 1}, "items": ["a"]}
    event = RunEvent(
        run_id=persisted_run.id,
        run_version=0,
        event_type="run.created",
        actor_class="operator",
        actor_id=uuid4(),
        payload=source,
        occurred_at=occurred_at,
    )
    source["nested"]["value"] = 99
    source["items"].append("mutated")

    with pytest.raises(TypeError):
        event.payload["nested"] = {"value": 2}  # type: ignore[index]
    assert event.payload["nested"]["value"] == 1  # type: ignore[index]

    async with uow:
        stored = await uow.events.append(event)
        await uow.commit()

    assert stored.sequence == 1
    assert stored.actor_class == "operator"
    assert stored.actor_id == event.actor_id
    assert stored.occurred_at == occurred_at
    assert thaw_payload(stored.payload) == {"nested": {"value": 1}, "items": ["a"]}

    async with uow:
        listed = await uow.events.list_after(persisted_run.id, sequence=0)
    assert listed == [stored]


@pytest.mark.integration
async def test_event_sequences_are_per_run_and_list_after_is_strict(uow, persisted_run) -> None:
    second = RunSnapshot(
        id=uuid4(), project_id=persisted_run.project_id, task_id=persisted_run.task_id
    )
    async with uow:
        await uow.runs.create(second)
        await uow.commit()

    async with uow:
        first_event = await uow.events.append(
            RunEvent(run_id=persisted_run.id, run_version=0, event_type="first", payload={})
        )
        second_event = await uow.events.append(
            RunEvent(run_id=persisted_run.id, run_version=0, event_type="second", payload={})
        )
        other_event = await uow.events.append(
            RunEvent(run_id=second.id, run_version=0, event_type="other", payload={})
        )
        await uow.commit()

    assert (first_event.sequence, second_event.sequence, other_event.sequence) == (1, 2, 1)
    async with uow:
        assert [item.sequence for item in await uow.events.list_after(persisted_run.id, 0)] == [
            1,
            2,
        ]
        assert [item.sequence for item in await uow.events.list_after(persisted_run.id, 1)] == [2]
        assert [item.sequence for item in await uow.events.list_after(persisted_run.id, 2)] == []
        assert [item.sequence for item in await uow.events.list_after(second.id, 0)] == [1]
        with pytest.raises(InvalidEventCursor):
            await uow.events.list_after(persisted_run.id, -1)
