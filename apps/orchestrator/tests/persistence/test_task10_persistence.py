"""Task 10 persistence foundation contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from forge.domain.run import RunSnapshot
from forge.persistence.models import Base, OperatorAuditEvent, Project, Task
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError


def test_task10_models_declare_identity_sources_receipts_and_audit_tables() -> None:
    assert {"api_mutations", "operator_audit_events"} <= set(Base.metadata.tables)
    assert {"name", "canonical_path_key"} <= set(Project.__table__.c.keys())
    assert {
        "title",
        "body",
        "source_url",
        "source_updated_at",
        "untrusted_external_content",
    } <= set(Task.__table__.c.keys())


def test_run_snapshot_carries_policy_and_base_binding() -> None:
    snapshot = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        policy_version=3,
        base_ref="refs/heads/main",
        base_sha="a" * 40,
    )

    assert snapshot.policy_version == 3
    assert snapshot.base_ref == "refs/heads/main"
    assert snapshot.base_sha == "a" * 40


def test_task10_forward_migration_is_the_single_head(
    alembic_config_factory: object,
) -> None:
    assert callable(alembic_config_factory)
    config = alembic_config_factory("postgresql+asyncpg://unused:unused@127.0.0.1/unused")
    assert ScriptDirectory.from_config(config).get_heads() == ["20260822_0002"]


@pytest.mark.integration
async def test_task10_repositories_preserve_task_sources_policy_versions_and_snapshots(
    session_factory: object,
) -> None:
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    project_id = uuid4()
    task_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        project = await work.projects.create(
            project_id=project_id,
            name="Parallel",
            canonical_path="D:/Code/Parallel",
            canonical_path_key="d:/code/parallel",
            github_repository="clar17y/parallel",
            default_branch="main",
            policy_digest="a" * 64,
            policy_document={"runner_mode": "docker"},
        )
        assert project.current_policy_version == 1
        policy = await work.projects.append_policy(
            project_id=project_id,
            expected_policy_version=1,
            policy_digest="b" * 64,
            policy_document={"runner_mode": "docker", "commands": []},
        )
        assert policy.version == 2
        task = await work.tasks.create(
            task_id=task_id,
            project_id=project_id,
            title="Exact\r\nTitle",
            body="Body\r\nwith café",
            source_url="https://example.test/issues/4",
            source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            external_source="github",
            external_id="4",
        )
        assert task.title == "Exact\r\nTitle"
        assert task.body == "Body\r\nwith café"
        assert task.normalized_text == "Exact\nTitle\n\nBody\nwith café"
        assert task.untrusted_external_content is True
        second_project = await work.projects.create(
            project_id=uuid4(),
            name="Other",
            canonical_path="D:/Code/Other",
            canonical_path_key="d:/code/other",
            github_repository="clar17y/other",
            default_branch="main",
            policy_digest="d" * 64,
            policy_document={"runner_mode": "docker"},
        )
        second_task = await work.tasks.create(
            task_id=uuid4(),
            project_id=second_project.id,
            title="Exact\r\nTitle",
            body="Body\r\nwith café",
            source_url="https://example.test/issues/4",
            source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            external_source="github",
            external_id="5",
        )
        assert second_task.external_id == "5"
        assert second_task.task_digest != task.task_digest
        await work.commit()

    async with PostgresUnitOfWork(session_factory) as work:
        loaded_task = await work.tasks.get(task_id)
        assert loaded_task.task_digest == task.task_digest
        run = RunSnapshot(
            id=uuid4(),
            project_id=project_id,
            task_id=task_id,
            policy_version=2,
            base_ref="refs/heads/main",
            base_sha="c" * 40,
        )
        await work.runs.create(run)
        await work.commit()

    async with PostgresUnitOfWork(session_factory) as work:
        loaded_run = await work.runs.get(run.id)
        assert loaded_run == run


@pytest.mark.integration
async def test_task10_mutation_receipts_hash_keys_and_reject_changed_requests(
    session_factory: object,
) -> None:
    from forge.persistence.repositories.mutations import MutationConflict
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    actor_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        receipt = await work.mutations.reserve(
            actor_id=actor_id,
            action="task.create",
            scope="project:1",
            idempotency_key="raw-key",
            request_digest="a" * 64,
        )
        await work.mutations.complete(
            receipt.id,
            response_status=201,
            response_payload={"id": "safe"},
            resource_kind="task",
            resource_id=uuid4(),
        )
        await work.commit()

    async with PostgresUnitOfWork(session_factory) as work:
        replay = await work.mutations.reserve(
            actor_id=actor_id,
            action="task.create",
            scope="project:1",
            idempotency_key="raw-key",
            request_digest="a" * 64,
        )
        assert replay.lifecycle_state == "COMPLETED"
        assert replay.key_hash != "raw-key"
        with pytest.raises(MutationConflict):
            await work.mutations.reserve(
                actor_id=actor_id,
                action="task.create",
                scope="project:1",
                idempotency_key="raw-key",
                request_digest="b" * 64,
            )


@pytest.mark.integration
async def test_task10_mutation_completion_requires_replayable_resource(
    session_factory: object,
) -> None:
    from forge.persistence.repositories.mutations import MutationRepositoryError
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    async with PostgresUnitOfWork(session_factory) as work:
        receipt = await work.mutations.reserve(
            actor_id=uuid4(),
            action="task.create",
            scope="project:resource-required",
            idempotency_key="resource-required",
            request_digest="a" * 64,
        )
        with pytest.raises(MutationRepositoryError, match="resource"):
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload={"id": "safe"},
            )

    async with session_factory() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO api_mutations "
                    "(id, actor_id, action, scope, key_hash, request_digest, lifecycle_state, "
                    "response_status, response_payload) VALUES "
                    "(:id, :actor_id, 'malformed', 'scope', :key_hash, :request_digest, "
                    "'COMPLETED', 201, '{}'::jsonb)"
                ),
                {
                    "id": uuid4(),
                    "actor_id": uuid4(),
                    "key_hash": "c" * 64,
                    "request_digest": "d" * 64,
                },
            )


@pytest.mark.integration
async def test_task10_audit_repository_redacts_payload_and_is_append_only(
    session_factory: object,
) -> None:
    from forge.persistence.models import OperatorAuditEvent
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from sqlalchemy import select

    actor_id = uuid4()
    subject_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        event = await work.audit.append(
            actor_id=actor_id,
            event_type="task.created",
            subject_type="task",
            subject_id=subject_id,
            payload={"authorization": "Bearer abcdefghijklmnop"},
        )
        await work.commit()
    assert event.id is not None

    async with session_factory() as session:
        stored = await session.scalar(
            select(OperatorAuditEvent).where(OperatorAuditEvent.id == event.id)
        )
        assert stored is not None
        assert stored.payload == {"authorization": "[REDACTED]"}


@pytest.mark.integration
async def test_task10_identity_task_and_audit_rows_reject_updates_and_deletes(
    session_factory: object,
) -> None:
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    project_id = uuid4()
    task_id = uuid4()
    actor_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.projects.create(
            project_id=project_id,
            name="Immutable",
            canonical_path="D:/Code/Immutable",
            canonical_path_key="d:/code/immutable",
            github_repository="owner/immutable",
            default_branch="main",
            policy_digest="a" * 64,
            policy_document={},
        )
        await work.tasks.create(task_id=task_id, project_id=project_id, title="Task", body="Body")
        audit = await work.audit.append(
            actor_id=actor_id,
            event_type="task.created",
            subject_type="task",
            subject_id=task_id,
            payload={"id": str(task_id)},
        )
        await work.commit()

    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(DBAPIError):
                await session.execute(
                    update(Project).where(Project.id == project_id).values(default_branch="release")
                )
        async with session.begin():
            with pytest.raises(DBAPIError):
                await session.execute(delete(Task).where(Task.id == task_id))
        async with session.begin():
            with pytest.raises(DBAPIError):
                await session.execute(
                    delete(OperatorAuditEvent).where(OperatorAuditEvent.id == audit.id)
                )


@pytest.mark.integration
def test_task10_upgrade_backfills_0001_rows_deterministically(
    test_database_url: str,
    alembic_config_factory: object,
) -> None:
    from alembic import command
    from forge.persistence.database import create_engine

    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "20260821_0001")

    async def insert_legacy_rows() -> None:
        engine = create_engine(test_database_url)
        try:
            async with engine.begin() as connection:
                project_id = uuid4()
                task_id = uuid4()
                await connection.execute(
                    text(
                        "INSERT INTO projects (id, canonical_path, github_repository, default_branch) "
                        "VALUES (:project_id, '/TMP/Legacy', 'Owner/Repo', 'main')"
                    ),
                    {"project_id": project_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO tasks (id, project_id, normalized_text, task_digest) "
                        "VALUES (:task_id, :project_id, 'legacy task', :digest)"
                    ),
                    {"task_id": task_id, "project_id": project_id, "digest": "b" * 64},
                )
                return project_id, task_id
        finally:
            await engine.dispose()

    project_id, task_id = asyncio.run(insert_legacy_rows())
    try:
        command.upgrade(config, "head")

        async def load_rows() -> tuple[object, object]:
            engine = create_engine(test_database_url)
            try:
                async with engine.connect() as connection:
                    project = (
                        await connection.execute(
                            text(
                                "SELECT name, canonical_path_key, github_repository FROM projects WHERE id = :id"
                            ),
                            {"id": project_id},
                        )
                    ).one()
                    task = (
                        await connection.execute(
                            text(
                                "SELECT title, body, untrusted_external_content FROM tasks WHERE id = :id"
                            ),
                            {"id": task_id},
                        )
                    ).one()
                    return project, task
            finally:
                await engine.dispose()

        project, task = asyncio.run(load_rows())
        assert tuple(project) == ("Project", "/tmp/legacy", "owner/repo")
        assert tuple(task) == ("Imported task", "legacy task", False)
    finally:
        command.downgrade(config, "base")
