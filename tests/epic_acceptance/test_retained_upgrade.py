"""Stopped v0.3 upgrade preserves pre-dispatch epic and legacy task facts."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from forge.domain.operation import canonical_digest
from forge.domain.subscription import OperatorProfile, RolePreference, RouteSpec, SpecialistPurpose
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.models import Approval, Artifact, Project, ProjectPolicyVersion, Run
from forge.persistence.models.subscription_recovery import SubscriptionRecoveryWorker
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from apps.orchestrator.tests.persistence.test_epic_brief_migration import _seed_epic, _seed_task

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)

SOURCE_REVISION = "20261004_0034"


async def _retained_facts(database_url: str, epic_id) -> dict:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            facts = {}
            for label, query, values in (
                ("task", "SELECT to_jsonb(t) FROM tasks t ORDER BY id", {}),
                ("epic", "SELECT to_jsonb(e) FROM epics e WHERE id = :id", {"id": epic_id}),
                ("brief", "SELECT to_jsonb(b) FROM epic_brief_revisions b WHERE epic_id = :id", {"id": epic_id}),
            ):
                rows = (await connection.execute(text(query), values)).scalars().all()
                # Comparing digests keeps any future private fields out of test output.
                facts[label] = (len(rows), canonical_digest(rows))
            return facts
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_stopped_upgrade_repeated_head_retains_pre_dispatch_source_and_refuses_loss(
    test_database_url, alembic_config_factory
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, SOURCE_REVISION)
    epic_id, revision_id, digest = asyncio.run(_seed_epic(test_database_url))
    before = asyncio.run(_retained_facts(test_database_url, epic_id))
    assert before["task"][0] == before["epic"][0] == before["brief"][0] == 1

    command.upgrade(config, "head")
    assert asyncio.run(_retained_facts(test_database_url, epic_id)) == before
    command.upgrade(config, "head")
    assert asyncio.run(_retained_facts(test_database_url, epic_id)) == before

    async def selected_source() -> tuple:
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT accepted_brief_revision_id, accepted_brief_digest "
                         "FROM epics WHERE id = :id"), {"id": epic_id}
                )
                return result.one()
        finally:
            await engine.dispose()

    assert tuple(asyncio.run(selected_source())) == (revision_id, digest)
    with pytest.raises(DBAPIError, match="cannot downgrade retained epic data"):
        command.downgrade(config, "20260929_0030")
    assert asyncio.run(_retained_facts(test_database_url, epic_id)) == before


@pytest.mark.integration
def test_v02_selected_profile_survives_epic_upgrade_reupgrade_and_safe_downgrade(
    test_database_url, alembic_config_factory, tmp_path
) -> None:
    config = alembic_config_factory(test_database_url)
    old_head = "20260929_0030"
    command.upgrade(config, old_head)
    task_id, task_row = asyncio.run(_seed_task(test_database_url))
    run_id, approval_id, artifact_id = uuid4(), uuid4(), uuid4()
    artifact_bytes = b"retained-v02-artifact\x00\xff"
    artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_pointer = f"sha256/{artifact_digest[:2]}/{artifact_digest[2:]}.blob"
    artifact_path = tmp_path / artifact_pointer
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    profile = OperatorProfile(
        profile_id=uuid4(), version=1,
        preferences=(RolePreference(
            purpose=SpecialistPurpose.PRIMARY,
            preferred_route=RouteSpec(provider="openai", client="codex", model="retained"),
        ),),
    )

    async def selected_profile() -> tuple:
        engine = create_engine(test_database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                project_id = await session.scalar(
                    text("SELECT project_id FROM tasks WHERE id = :id"), {"id": task_id}
                )
                repo = PostgresSubscriptionRepository(session)
                stored = await repo.profile(profile.profile_id, profile.version)
                selected = await repo.project_profile(project_id)
                task = await session.scalar(
                    text("SELECT row_to_json(t)::text FROM tasks t WHERE id = :id"),
                    {"id": task_id},
                )
                rows = []
                for table, identity in (
                    ("runs", run_id), ("approvals", approval_id), ("artifacts", artifact_id)
                ):
                    rows.append(await session.scalar(
                        text(f"SELECT row_to_json(t)::text FROM {table} t WHERE id = :id"),
                        {"id": identity},
                    ))
                worker = await session.scalar(text(
                    "SELECT row_to_json(t)::text FROM subscription_recovery_workers t "
                    "WHERE worker_id = 'retained-worker'"
                ))
                return stored, selected, task, *rows, worker
        finally:
            await engine.dispose()

    async def seed_profile() -> None:
        engine = create_engine(test_database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                project_id = await session.scalar(
                    text("SELECT project_id FROM tasks WHERE id = :id"), {"id": task_id}
                )
                repo = PostgresSubscriptionRepository(session)
                await repo.store_profile(profile)
                await repo.select_project_profile_expected(
                    project_id, profile, expected_profile_id=None, expected_profile_version=None
                )
                session.add(ProjectPolicyVersion(
                    project_id=project_id, version=1, policy_digest="b" * 64,
                    document_schema_version=1, document={},
                ))
                await session.flush()
                project = await session.get(Project, project_id)
                project.current_policy_version = 1
                session.add(Run(
                    id=run_id, project_id=project_id, task_id=task_id, policy_version=1,
                    state="AWAITING_PLAN_APPROVAL", version=1, pending_gate="plan",
                    pending_evidence_digest="c" * 64,
                ))
                await session.flush()
                session.add(Approval(
                    id=approval_id, run_id=run_id, gate="plan", evidence_digest="c" * 64,
                    run_version=1, policy_version=1, authenticated_actor_id=uuid4(),
                ))
                session.add(Artifact(
                    id=artifact_id, digest=artifact_digest, media_type="application/octet-stream",
                    storage_pointer=artifact_pointer, size_bytes=len(artifact_bytes),
                    metadata_schema_version=1, artifact_metadata={"schema_version": 1},
                ))
                session.add(SubscriptionRecoveryWorker(
                    worker_id="retained-worker", contract_version=1,
                    observed_at=datetime.now(UTC),
                ))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed_profile())
    before = asyncio.run(selected_profile())
    assert before[:3] == (profile, profile, task_row)
    assert all(row is not None for row in before[3:])
    assert artifact_path.read_bytes() == artifact_bytes
    command.upgrade(config, "head")
    assert asyncio.run(selected_profile()) == before
    assert artifact_path.read_bytes() == artifact_bytes
    command.upgrade(config, "head")
    assert asyncio.run(selected_profile()) == before
    command.downgrade(config, old_head)
    assert asyncio.run(selected_profile()) == before
    command.upgrade(config, "head")
    assert asyncio.run(selected_profile()) == before
    assert artifact_path.read_bytes() == artifact_bytes
