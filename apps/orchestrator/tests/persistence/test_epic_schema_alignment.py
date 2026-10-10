"""Migrated epic authority/evidence objects match the runtime ORM contract."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from forge.domain.operation import canonical_digest
from forge.persistence.database import create_engine
from forge.persistence.models.base import Base
from forge.persistence.models.execution import OperationIntent
from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.exc import DBAPIError

from apps.orchestrator.tests.epic_run_bridge.test_launch import BridgeWork, setup


@pytest.mark.integration
async def test_epic_indexes_and_checks_match_migrated_postgres(
    test_database_url, alembic_config_factory
):
    await asyncio.to_thread(command.upgrade, alembic_config_factory(test_database_url), "head")
    expected_indexes = {
        "epic_budget_admission_permits": {
            "ix_epic_budget_admission_permits_run": ["run_id", "created_at"],
        },
        "epic_child_budget_holds": {"ix_epic_child_budget_holds_epic": ["epic_id"]},
        "epic_control_intents": {
            "ix_epic_control_intents_pending": ["status", "execution_id"],
        },
        "epic_execution_controls": {"ix_epic_execution_controls_epic": ["epic_id"]},
        "epic_dispatch_settings": {
            "ix_epic_dispatch_scan": ["enabled", "checked_at", "execution_id"],
        },
    }
    expected_checks = {
        "epic_completion_handoffs": 3,
        "epic_dispatch_settings": 5,
        "epic_execution_controls": 2,
    }
    engine = create_engine(test_database_url)
    try:
        async with engine.connect() as connection:
            reflected = await connection.run_sync(
                lambda bind: {
                    table: (
                        {
                            index["name"]: index["column_names"]
                            for index in inspect(bind).get_indexes(table)
                            if not index.get("duplicates_constraint")
                        },
                        {check["name"] for check in inspect(bind).get_check_constraints(table)},
                    )
                    for table in expected_indexes.keys() | expected_checks.keys()
                }
            )
        for table, indexes in expected_indexes.items():
            metadata = {
                index.name: [column.name for column in index.columns]
                for index in Base.metadata.tables[table].indexes
            }
            assert metadata == indexes
            assert reflected[table][0] == indexes
        for table, count in expected_checks.items():
            metadata = {
                constraint.name
                for constraint in Base.metadata.tables[table].constraints
                if isinstance(constraint, CheckConstraint)
            }
            assert len(metadata) == count
            assert reflected[table][1] == metadata
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_dispatch_and_control_checks_reject_partial_authority(
    session_factory, migrated_database_url, alembic_config_factory
):
    from forge.application.services.epic_dispatch import EpicDispatchRequest, EpicDispatchService

    factory = lambda: BridgeWork(session_factory)
    bridge, _, actor, epic_id, _, _, _ = await setup(session_factory, factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="schema-execution",
        expected_epic_version=5,
    )
    await EpicDispatchService(factory).configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="schema-dispatch",
        request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    invalid = (
        ("epic_execution_controls", "version = 0"),
        ("epic_execution_controls", "state = 'INVALID'"),
        ("epic_dispatch_settings", "version = 0"),
        ("epic_dispatch_settings", "profile_id = :identity"),
        ("epic_dispatch_settings", "profile_version = 0"),
        ("epic_dispatch_settings", "claim_item_id = :identity"),
        ("epic_dispatch_settings", "claim_token = :identity"),
        ("epic_dispatch_settings", "claim_expires_at = now()"),
    )
    for table, assignment in invalid:
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    text(f"UPDATE {table} SET {assignment} WHERE execution_id = :execution"),
                    {"identity": uuid4(), "execution": execution.execution_id},
                )
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE epic_dispatch_settings SET profile_id = :profile, profile_version = 1, "
                 "claim_item_id = :item, claim_token = :token, claim_expires_at = now() "
                 "WHERE execution_id = :execution"),
            {"profile": uuid4(), "item": uuid4(), "token": uuid4(),
             "execution": execution.execution_id},
        )
        await session.execute(
            text("UPDATE epic_dispatch_settings SET profile_id = NULL, profile_version = NULL, "
                 "claim_item_id = NULL, claim_token = NULL, claim_expires_at = NULL "
                 "WHERE execution_id = :execution"),
            {"execution": execution.execution_id},
        )
    with pytest.raises(DBAPIError, match="cannot discard epic dispatch authority"):
        await asyncio.to_thread(
            command.downgrade,
            alembic_config_factory(migrated_database_url), "20261007_0036",
        )
    async with session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "20261010_0038"


@pytest.mark.integration
def test_empty_dispatch_and_handoff_schema_downgrade_reupgrades(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "20261005_0035")
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.integration
async def test_handoff_hash_checks_immutability_and_nonempty_downgrade_refusal(
    session_factory, migrated_database_url, alembic_config_factory
):
    factory = lambda: BridgeWork(session_factory)
    bridge, _, actor, epic_id, item_id, _, request = await setup(session_factory, factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="schema-handoff", request=request(item_id)
    )
    intent_id = uuid4()
    payload = {"approval_id": str(uuid4())}
    digest = canonical_digest(payload)
    async with session_factory() as session, session.begin():
        session.add(OperationIntent(
            id=intent_id, run_id=child.run_id, operation_kind="merge_pr",
            idempotency_key=f"{child.run_id}:merge_pr:{digest}",
            request_digest=digest, request_payload=payload, status="SUCCEEDED",
            outcome_schema_version=1, outcome_payload={"merge_sha": "a" * 40},
            completed_at=datetime.now(UTC),
        ))
    values = {
        "id": uuid4(), "execution": child.execution_id, "item": item_id,
        "attempt": child.attempt_id, "run": child.run_id, "intent": intent_id,
        "merge": "a" * 40, "base": "b" * 40, "digest": "c" * 64,
    }
    insert = text(
        "INSERT INTO epic_completion_handoffs (id, execution_id, item_id, attempt_id, "
        "run_id, run_version, merge_intent_id, merge_sha, integration_ref, "
        "verified_base_sha, evidence_digest) VALUES (:id, :execution, :item, :attempt, "
        ":run, 1, :intent, :merge, 'refs/heads/main', :base, :digest)"
    )
    for field in ("merge", "base", "digest"):
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(insert, {**values, field: "invalid"})
    async with session_factory() as session, session.begin():
        await session.execute(insert, values)
    for statement in (
        "UPDATE epic_completion_handoffs SET merge_sha = merge_sha WHERE id = :id",
        "DELETE FROM epic_completion_handoffs WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="epic completion handoffs are immutable"):
            async with session_factory() as session, session.begin():
                await session.execute(text(statement), {"id": values["id"]})
    with pytest.raises(DBAPIError, match="cannot discard epic completion handoffs"):
        await asyncio.to_thread(
            command.downgrade, alembic_config_factory(migrated_database_url),
            "20261005_0035",
        )
    async with session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM epic_completion_handoffs")) == 1
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "20261010_0038"
