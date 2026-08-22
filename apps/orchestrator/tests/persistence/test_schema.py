"""Executable contract for Forge's initial PostgreSQL schema."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from forge.persistence.models import Base
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql.sqltypes import Enum as SqlEnum
from sqlalchemy.sql.sqltypes import Uuid

EXPECTED_TABLES = {
    "projects",
    "project_policy_versions",
    "tasks",
    "runs",
    "operator_sessions",
    "approval_challenges",
    "run_commands",
    "run_events",
    "steps",
    "approvals",
    "agent_executions",
    "tool_calls",
    "model_usage",
    "artifacts",
    "validation_results",
    "reviews",
    "pull_requests",
    "operation_intents",
}


async def _inspect_database(
    database_url: str,
    operation: Callable[[Any], object],
) -> object:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(operation)
    finally:
        await engine.dispose()


def _table_names(database_url: str) -> set[str]:
    result = asyncio.run(
        _inspect_database(
            database_url, lambda connection: set(inspect(connection).get_table_names())
        )
    )
    assert isinstance(result, set)
    return result - {"alembic_version"}


def _execute(database_url: str, statement: str) -> None:
    async def execute() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(statement)
        finally:
            await engine.dispose()

    asyncio.run(execute())


@pytest.mark.integration
def test_initial_migration_creates_the_complete_v01_schema(
    migrated_database_url: str,
) -> None:
    assert _table_names(migrated_database_url) == EXPECTED_TABLES


@pytest.mark.integration
def test_initial_migration_matches_the_declared_models(
    migrated_database_url: str,
    alembic_config_factory: Callable[[str], Config],
) -> None:
    command.check(alembic_config_factory(migrated_database_url))


@pytest.mark.integration
def test_initial_migration_downgrades_and_reapplies_cleanly(
    test_database_url: str,
    alembic_config_factory: Callable[[str], Config],
) -> None:
    config = alembic_config_factory(test_database_url)
    _execute(test_database_url, "CREATE TABLE external_sentinel (id integer PRIMARY KEY)")

    command.upgrade(config, "head")
    assert _table_names(test_database_url) == EXPECTED_TABLES | {"external_sentinel"}

    command.downgrade(config, "base")
    assert _table_names(test_database_url) == {"external_sentinel"}

    command.upgrade(config, "head")
    assert _table_names(test_database_url) == EXPECTED_TABLES | {"external_sentinel"}


@pytest.mark.integration
def test_schema_contains_required_identity_and_safety_constraints(
    migrated_database_url: str,
) -> None:
    def inventory(connection: Any) -> dict[str, object]:
        inspector = inspect(connection)
        return {
            "project_uniques": inspector.get_unique_constraints("projects"),
            "policy_primary_key": inspector.get_pk_constraint("project_policy_versions"),
            "task_uniques": inspector.get_unique_constraints("tasks"),
            "approval_uniques": inspector.get_unique_constraints("approvals"),
            "artifact_uniques": inspector.get_unique_constraints("artifacts"),
            "pr_uniques": inspector.get_unique_constraints("pull_requests"),
            "intent_uniques": inspector.get_unique_constraints("operation_intents"),
            "run_indexes": inspector.get_indexes("runs"),
            "command_indexes": inspector.get_indexes("run_commands"),
            "session_checks": inspector.get_check_constraints("operator_sessions"),
            "run_checks": inspector.get_check_constraints("runs"),
            "project_fks": inspector.get_foreign_keys("projects"),
            "run_fks": inspector.get_foreign_keys("runs"),
            "challenge_uniques": inspector.get_unique_constraints("approval_challenges"),
            "all_foreign_keys": {
                table: inspector.get_foreign_keys(table) for table in EXPECTED_TABLES
            },
        }

    result = asyncio.run(_inspect_database(migrated_database_url, inventory))
    assert isinstance(result, dict)

    def constrained_columns(key: str) -> set[tuple[str, ...]]:
        values = result[key]
        assert isinstance(values, list)
        return {tuple(item["column_names"]) for item in values}

    assert {("github_repository",), ("canonical_path",)} <= constrained_columns("project_uniques")
    policy_primary_key = result["policy_primary_key"]
    assert isinstance(policy_primary_key, dict)
    assert tuple(policy_primary_key["constrained_columns"]) == ("project_id", "version")
    assert ("external_source", "external_id") in constrained_columns("task_uniques")
    assert ("run_id", "gate", "evidence_digest", "run_version") in constrained_columns(
        "approval_uniques"
    )
    assert ("digest",) in constrained_columns("artifact_uniques")
    assert ("repository", "pull_request_number") in constrained_columns("pr_uniques")
    assert ("idempotency_key",) in constrained_columns("intent_uniques")
    assert ("token_hash",) in constrained_columns("challenge_uniques")

    run_index_columns = constrained_columns("run_indexes")
    assert {("state",), ("project_id",), ("updated_at",)} <= run_index_columns
    command_index_columns = constrained_columns("command_indexes")
    assert {("status",), ("available_at",), ("lease_expires_at",)} <= command_index_columns

    session_checks = result["session_checks"]
    run_checks = result["run_checks"]
    assert isinstance(session_checks, list)
    assert isinstance(run_checks, list)
    assert {item["name"] for item in session_checks} >= {
        "ck_operator_sessions_credential_kind",
        "ck_operator_sessions_credential_shape",
    }
    assert {item["name"] for item in run_checks} >= {
        "ck_runs_database_state",
        "ck_runs_database_resource_shape",
        "ck_runs_pending_gate_shape",
    }

    project_fks = result["project_fks"]
    run_fks = result["run_fks"]
    assert isinstance(project_fks, list)
    assert isinstance(run_fks, list)
    assert any(
        foreign_key["referred_table"] == "project_policy_versions"
        and tuple(foreign_key["constrained_columns"]) == ("id", "current_policy_version")
        for foreign_key in project_fks
    )
    assert any(
        foreign_key["referred_table"] == "project_policy_versions"
        and tuple(foreign_key["constrained_columns"]) == ("project_id", "policy_version")
        for foreign_key in run_fks
    )

    all_foreign_keys = result["all_foreign_keys"]
    assert isinstance(all_foreign_keys, dict)
    for foreign_keys in all_foreign_keys.values():
        assert all(foreign_key["options"].get("ondelete") for foreign_key in foreign_keys)


@pytest.mark.integration
def test_every_execution_and_evidence_table_is_linked_to_a_run(
    migrated_database_url: str,
) -> None:
    run_owned = EXPECTED_TABLES - {
        "projects",
        "project_policy_versions",
        "tasks",
        "runs",
        "operator_sessions",
    }

    def run_foreign_keys(connection: Any) -> dict[str, set[str]]:
        inspector = inspect(connection)
        return {
            table: {
                foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys(table)
            }
            for table in run_owned
        }

    result = asyncio.run(_inspect_database(migrated_database_url, run_foreign_keys))
    assert isinstance(result, dict)
    assert all("runs" in targets for targets in result.values())


def test_database_name_guard_rejects_non_test_databases(
    database_name_validator: Callable[[str], str],
) -> None:
    for unsafe_name in ("forge", "postgres", "forge_test_", "forge_test_deadbeef;drop"):
        with pytest.raises(ValueError, match="refusing to manage"):
            database_name_validator(unsafe_name)


def test_migration_has_one_exact_reviewable_head(
    alembic_config_factory: Callable[[str], Config],
) -> None:
    config = alembic_config_factory("postgresql+asyncpg://unused:unused@127.0.0.1/unused")
    assert ScriptDirectory.from_config(config).get_heads() == ["20260821_0001"]


def test_models_define_exact_tables_uuid_keys_and_versioned_jsonb() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES

    expected_json_versions = {
        ("project_policy_versions", "document"): "document_schema_version",
        ("runs", "suspension_context"): "suspension_context_schema_version",
        ("run_commands", "payload"): "payload_schema_version",
        ("run_events", "payload"): "payload_schema_version",
        ("tool_calls", "normalized_arguments"): "arguments_schema_version",
        ("tool_calls", "result_metadata"): "result_metadata_schema_version",
        ("artifacts", "metadata"): "metadata_schema_version",
        ("pull_requests", "checks"): "checks_schema_version",
        ("pull_requests", "reviews"): "reviews_schema_version",
        ("operation_intents", "request_payload"): "request_schema_version",
        ("operation_intents", "outcome_payload"): "outcome_schema_version",
    }

    observed_json: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        assert not any(isinstance(column.type, SqlEnum) for column in table.columns)
        if table.name == "project_policy_versions":
            assert isinstance(table.c.project_id.type, Uuid)
        else:
            assert isinstance(table.c.id.type, Uuid)
            assert table.c.id.primary_key
        for column in table.columns:
            if isinstance(column.type, JSONB):
                observed_json.add((table.name, column.name))
                assert expected_json_versions[(table.name, column.name)] in table.c

    assert observed_json == set(expected_json_versions)


def test_database_module_import_does_not_construct_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy.ext.asyncio

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("database connection machinery ran during import")

    module = importlib.import_module("forge.persistence.database")
    with monkeypatch.context() as scoped:
        scoped.setattr(sqlalchemy.ext.asyncio, "create_async_engine", fail_if_called)
        importlib.reload(module)
    importlib.reload(module)
