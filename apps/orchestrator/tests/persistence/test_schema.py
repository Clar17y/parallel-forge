"""Executable contract for Forge's initial PostgreSQL schema."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from forge.application.services.state_engine import LEGAL, StateEngine
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Base
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql.sqltypes import Enum as SqlEnum
from sqlalchemy.sql.sqltypes import Uuid

EXPECTED_TABLES = {
    "api_mutations",
    "projects",
    "project_policy_versions",
    "tasks",
    "runs",
    "operator_sessions",
    "operator_audit_events",
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
    "artifact_lineages",
    "artifact_lineage_parents",
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


async def _insert_project_policy_task_run(
    database_url: str,
    *,
    project_id: UUID | None = None,
    task_id: UUID | None = None,
    policy_version: int = 1,
    run_id: UUID | None = None,
    project_path_suffix: str | None = None,
) -> tuple[UUID, UUID, UUID]:
    project_id = project_id or uuid4()
    task_id = task_id or uuid4()
    run_id = run_id or uuid4()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, canonical_path, github_repository, default_branch) "
                    "VALUES (:id, :path, :repository, 'main')"
                ),
                {
                    "id": project_id,
                    "path": project_path_suffix or f"/tmp/forge-{project_id}",
                    "repository": f"Clar17y/forge-{project_id}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO project_policy_versions "
                    "(project_id, version, policy_digest, document_schema_version, document) "
                    "VALUES (:project_id, :version, :digest, 1, '{}'::jsonb)"
                ),
                {
                    "project_id": project_id,
                    "version": policy_version,
                    "digest": "a" * 64,
                },
            )
            await connection.execute(
                text(
                    "UPDATE projects SET current_policy_version = :version WHERE id = :project_id"
                ),
                {"project_id": project_id, "version": policy_version},
            )
            await connection.execute(
                text(
                    "INSERT INTO tasks "
                    "(id, project_id, normalized_text, task_digest) "
                    "VALUES (:id, :project_id, 'task', :digest)"
                ),
                {"id": task_id, "project_id": project_id, "digest": "b" * 64},
            )
            await connection.execute(
                text(
                    "INSERT INTO runs "
                    "(id, project_id, task_id, policy_version, state, version, "
                    "local_remediation_count, remote_remediation_count, token_budget, "
                    "cost_budget_minor, duration_budget_seconds, database_state) "
                    "VALUES (:id, :project_id, :task_id, :policy_version, 'CREATED', 0, "
                    "0, 0, 0, 0, 0, 'DISABLED')"
                ),
                {
                    "id": run_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "policy_version": policy_version,
                },
            )
    finally:
        await engine.dispose()
    return project_id, task_id, run_id


async def _rejects(database_url: str, statement: str, parameters: dict[str, object]) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text(statement), parameters)
            except DBAPIError:
                await transaction.rollback()
            else:
                await transaction.rollback()
                raise AssertionError(f"statement unexpectedly succeeded: {parameters}")
    finally:
        await engine.dispose()


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
def test_database_run_state_constraint_matches_domain_values(
    migrated_database_url: str,
) -> None:
    def state_constraint(connection: Any) -> str:
        checks = inspect(connection).get_check_constraints("runs")
        state_check = next(item for item in checks if item["name"] == "ck_runs_state")
        assert isinstance(state_check["sqltext"], str)
        return state_check["sqltext"]

    sqltext = asyncio.run(_inspect_database(migrated_database_url, state_constraint))
    database_states = set(re.findall(r"'([^']+)'", sqltext))
    assert database_states == {state.value for state in RunState}


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
            "intent_columns": inspector.get_columns("operation_intents"),
            "intent_indexes": inspector.get_indexes("operation_intents"),
            "artifact_indexes": inspector.get_indexes("artifact_lineages"),
            "artifact_parent_indexes": inspector.get_indexes("artifact_lineage_parents"),
            "artifact_columns": inspector.get_columns("artifacts"),
            "artifact_checks": inspector.get_check_constraints("artifacts"),
            "artifact_lineage_checks": inspector.get_check_constraints("artifact_lineages"),
            "artifact_parent_checks": inspector.get_check_constraints("artifact_lineage_parents"),
            "usage_columns": inspector.get_columns("model_usage"),
            "usage_indexes": inspector.get_indexes("model_usage"),
            "usage_checks": inspector.get_check_constraints("model_usage"),
            "usage_fks": inspector.get_foreign_keys("model_usage"),
            "agent_uniques": inspector.get_unique_constraints("agent_executions"),
            "run_indexes": inspector.get_indexes("runs"),
            "command_indexes": inspector.get_indexes("run_commands"),
            "command_columns": inspector.get_columns("run_commands"),
            "session_checks": inspector.get_check_constraints("operator_sessions"),
            "command_checks": inspector.get_check_constraints("run_commands"),
            "intent_checks": inspector.get_check_constraints("operation_intents"),
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
    assert ("project_id", "external_source", "external_id") in constrained_columns("task_uniques")
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
    assert {("status", "available_at", "created_at"), ("run_id", "status", "lease_expires_at")} <= {
        tuple(item["column_names"]) for item in result["command_indexes"] if isinstance(item, dict)
    }

    command_columns = result["command_columns"]
    intent_columns = result["intent_columns"]
    assert isinstance(command_columns, list)
    assert isinstance(intent_columns, list)
    assert "request_digest" in {item["name"] for item in intent_columns}
    assert "remote_resource_id" in {item["name"] for item in intent_columns}
    assert "execution_owner" in {item["name"] for item in intent_columns}
    assert "execution_lease_expires_at" in {item["name"] for item in intent_columns}
    assert "resource_identity" not in {item["name"] for item in intent_columns}
    command_checks = result["command_checks"]
    intent_checks = result["intent_checks"]
    assert isinstance(command_checks, list)
    assert isinstance(intent_checks, list)
    assert "ck_run_commands_lease_shape" in {item["name"] for item in command_checks}
    assert "ck_run_commands_terminal_timestamp_shape" in {item["name"] for item in command_checks}
    assert "ck_operation_intents_status_shape" in {item["name"] for item in intent_checks}
    assert "ck_operation_intents_request_digest" in {item["name"] for item in intent_checks}
    assert "ck_operation_intents_execution_lease_shape" in {item["name"] for item in intent_checks}
    assert {
        "ck_operation_intents_attempt_count_nonnegative",
        "ck_operation_intents_request_schema_version_positive",
        "ck_operation_intents_outcome_shape",
    } <= {item["name"] for item in intent_checks}
    intent_index_columns = constrained_columns("intent_indexes")
    assert ("status", "execution_lease_expires_at", "updated_at") in intent_index_columns

    artifact_columns = result["artifact_columns"]
    assert isinstance(artifact_columns, list)
    assert {"digest", "storage_pointer", "size_bytes", "metadata_schema_version"} <= {
        item["name"] for item in artifact_columns
    }
    assert not {"run_id", "producer_kind", "parent_artifact_id"} & {
        item["name"] for item in artifact_columns
    }
    artifact_checks = result["artifact_checks"]
    assert isinstance(artifact_checks, list)
    assert {
        "ck_artifacts_digest_lowercase_sha256",
        "ck_artifacts_media_type_nonempty",
        "ck_artifacts_storage_pointer_canonical",
        "ck_artifacts_size_and_schema_version",
    } <= {item["name"] for item in artifact_checks}
    lineage_checks = result["artifact_lineage_checks"]
    parent_checks = result["artifact_parent_checks"]
    assert isinstance(lineage_checks, list)
    assert isinstance(parent_checks, list)
    assert "ck_artifact_lineages_producer_kind_nonempty" in {
        item["name"] for item in lineage_checks
    }
    assert "ck_artifact_lineage_parents_no_self_parent" in {item["name"] for item in parent_checks}
    artifact_index_columns = constrained_columns("artifact_indexes")
    assert {("run_id", "created_at"), ("artifact_id",)} <= artifact_index_columns
    assert constrained_columns("artifact_parent_indexes") >= {("parent_artifact_id", "run_id")}

    usage_columns = result["usage_columns"]
    usage_checks = result["usage_checks"]
    usage_fks = result["usage_fks"]
    assert isinstance(usage_columns, list)
    assert isinstance(usage_checks, list)
    assert isinstance(usage_fks, list)
    usage_column_names = {item["name"] for item in usage_columns}
    assert {
        "prompt_version",
        "cached_input_tokens",
        "duration_ms",
        "tool_call_count",
        "provider_request_id",
        "pricing_version",
        "currency",
        "unknown_price_reason",
    } <= usage_column_names
    assert "latency_ms" not in usage_column_names
    assert {
        "ck_model_usage_usage_nonnegative",
        "ck_model_usage_currency",
        "ck_model_usage_identity_nonempty",
        "ck_model_usage_versions_nonempty",
        "ck_model_usage_price_shape",
    } <= {item["name"] for item in usage_checks}
    assert {
        ("run_id", "created_at"),
        ("agent_execution_id",),
        ("provider", "model"),
    } <= constrained_columns("usage_indexes")
    assert ("id", "run_id") in constrained_columns("agent_uniques")
    assert any(
        tuple(foreign_key["constrained_columns"]) == ("agent_execution_id", "run_id")
        and foreign_key["referred_table"] == "agent_executions"
        for foreign_key in usage_fks
    )

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
        "artifacts",
    }

    def run_foreign_keys(connection: Any) -> dict[str, set[str]]:
        inspector = inspect(connection)
        return {
            table: {
                foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys(table)
            }
            for table in run_owned
            if table not in {"api_mutations", "operator_audit_events"}
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
    assert ScriptDirectory.from_config(config).get_heads() == ["20260822_0002"]


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
        ("operator_audit_events", "payload"): "schema_version",
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
                if (table.name, column.name) == ("api_mutations", "response_payload"):
                    continue
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


@pytest.mark.integration
def test_run_event_sequence_is_application_assigned_per_run_and_positive(
    migrated_database_url: str,
) -> None:
    async def exercise() -> None:
        _, _, first_run = await _insert_project_policy_task_run(migrated_database_url)
        _, _, second_run = await _insert_project_policy_task_run(migrated_database_url)
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                values = {
                    "run_version": 0,
                    "actor_class": "system",
                    "occurred_at": datetime.now(UTC),
                }
                await connection.execute(
                    text(
                        "INSERT INTO run_events "
                        "(id, sequence, run_id, run_version, event_type, actor_class, "
                        "occurred_at, payload_schema_version, payload) "
                        "VALUES (:id, 1, :run_id, :run_version, 'created', :actor_class, "
                        ":occurred_at, 1, '{}'::jsonb)"
                    ),
                    {**values, "id": uuid4(), "run_id": first_run},
                )
                await connection.execute(
                    text(
                        "INSERT INTO run_events "
                        "(id, sequence, run_id, run_version, event_type, actor_class, "
                        "occurred_at, payload_schema_version, payload) "
                        "VALUES (:id, 1, :run_id, :run_version, 'created', :actor_class, "
                        ":occurred_at, 1, '{}'::jsonb)"
                    ),
                    {**values, "id": uuid4(), "run_id": second_run},
                )
            await _rejects(
                migrated_database_url,
                "INSERT INTO run_events "
                "(id, sequence, run_id, run_version, event_type, actor_class, occurred_at, "
                "payload_schema_version, payload) VALUES (:id, 1, :run_id, 0, 'duplicate', "
                ":actor_class, :occurred_at, 1, '{}'::jsonb)",
                {
                    "id": uuid4(),
                    "run_id": first_run,
                    "actor_class": "system",
                    "occurred_at": datetime.now(UTC),
                },
            )
            await _rejects(
                migrated_database_url,
                "INSERT INTO run_events "
                "(id, sequence, run_id, run_version, event_type, actor_class, occurred_at, "
                "payload_schema_version, payload) VALUES (:id, 0, :run_id, 0, 'invalid', "
                ":actor_class, :occurred_at, 1, '{}'::jsonb)",
                {
                    "id": uuid4(),
                    "run_id": first_run,
                    "actor_class": "system",
                    "occurred_at": datetime.now(UTC),
                },
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_run_event_uses_bounded_actor_class_and_timezone_occurred_at(
    migrated_database_url: str,
) -> None:
    _, _, run_id = asyncio.run(_insert_project_policy_task_run(migrated_database_url))
    insert_statement = (
        "INSERT INTO run_events "
        "(id, sequence, run_id, run_version, event_type, actor_class, "
        "occurred_at, payload_schema_version, payload) "
        "VALUES (:id, :sequence, :run_id, 0, 'round_trip', :actor_class, "
        ":occurred_at, 1, '{}'::jsonb)"
    )

    def columns(connection: Any) -> dict[str, Any]:
        return {column["name"]: column for column in inspect(connection).get_columns("run_events")}

    observed = asyncio.run(_inspect_database(migrated_database_url, columns))
    assert isinstance(observed, dict)
    assert "created_at" not in observed
    assert observed["actor_class"]["nullable"] is False
    assert observed["actor_class"]["type"].length <= 32
    assert observed["occurred_at"]["nullable"] is False
    assert observed["occurred_at"]["type"].timezone is True

    async def round_trip() -> None:
        engine = create_async_engine(migrated_database_url)
        when = datetime(2026, 8, 22, 1, 2, 3, tzinfo=UTC)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(insert_statement),
                    {
                        "id": uuid4(),
                        "sequence": 1,
                        "run_id": run_id,
                        "actor_class": "operator",
                        "occurred_at": when,
                    },
                )
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT actor_class, occurred_at FROM run_events WHERE run_id = :run_id"
                        ),
                        {"run_id": run_id},
                    )
                ).one()
                assert row.actor_class == "operator"
                assert row.occurred_at == when
        finally:
            await engine.dispose()

    asyncio.run(round_trip())
    for sequence, actor_class, occurred_at in (
        (2, "unknown", datetime.now(UTC)),
        (3, None, datetime.now(UTC)),
        (4, "operator", None),
    ):
        asyncio.run(
            _rejects(
                migrated_database_url,
                insert_statement,
                {
                    "id": uuid4(),
                    "sequence": sequence,
                    "run_id": run_id,
                    "actor_class": actor_class,
                    "occurred_at": occurred_at,
                },
            )
        )


@pytest.mark.integration
def test_run_suspension_metadata_is_state_specific_and_versioned(
    migrated_database_url: str,
) -> None:
    async def exercise() -> None:
        project_id, task_id, _ = await _insert_project_policy_task_run(migrated_database_url)
        base = {
            "project_id": project_id,
            "task_id": task_id,
            "policy_version": 1,
            "local_remediation_count": 0,
            "remote_remediation_count": 0,
            "token_budget": 0,
            "cost_budget_minor": 0,
            "duration_budget_seconds": 0,
            "database_state": "DISABLED",
        }
        common = (
            "(id, project_id, task_id, policy_version, state, version, "
            "suspended_state, suspension_kind, suspension_context_schema_version, "
            "suspension_context, local_remediation_count, remote_remediation_count, "
            "token_budget, cost_budget_minor, duration_budget_seconds, database_state)"
        )
        values = (
            "VALUES (:id, :project_id, :task_id, :policy_version, :state, 0, "
            ":suspended_state, :suspension_kind, :context_version, :context, "
            ":local_remediation_count, :remote_remediation_count, :token_budget, "
            ":cost_budget_minor, :duration_budget_seconds, :database_state)"
        )
        for state, kind, context_version, context in (
            ("PAUSED", None, 1, '{"state":"CREATED"}'),
            ("PAUSED", "INTERVENTION", 1, '{"state":"CREATED"}'),
            ("PAUSED", "PAUSE", None, '{"state":"CREATED"}'),
            ("AWAITING_HUMAN_INTERVENTION", "INTERVENTION", 1, '{"state":"CREATED"}'),
            ("AWAITING_HUMAN_INTERVENTION", "PAUSE", None, None),
            ("CREATED", "PAUSE", 1, '{"state":"CREATED"}'),
        ):
            await _rejects(
                migrated_database_url,
                f"INSERT INTO runs {common} {values}",
                {
                    **base,
                    "id": uuid4(),
                    "state": state,
                    "suspended_state": "CREATED",
                    "suspension_kind": kind,
                    "context_version": context_version,
                    "context": context,
                },
            )

        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                for state, kind in (
                    ("PAUSED", "PAUSE"),
                    ("AWAITING_HUMAN_INTERVENTION", "INTERVENTION"),
                ):
                    await connection.execute(
                        text(f"INSERT INTO runs {common} {values}"),
                        {
                            **base,
                            "id": uuid4(),
                            "state": state,
                            "suspended_state": "PLANNING"
                            if state == "AWAITING_HUMAN_INTERVENTION"
                            else "CREATED",
                            "suspension_kind": kind,
                            "context_version": 1 if state == "PAUSED" else None,
                            "context": '{"state":"CREATED"}' if state == "PAUSED" else None,
                        },
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_state_engine_intervention_snapshot_persists_without_context(
    migrated_database_url: str,
) -> None:
    async def exercise() -> None:
        project_id, task_id, _ = await _insert_project_policy_task_run(migrated_database_url)
        initial = RunSnapshot(
            id=uuid4(),
            project_id=project_id,
            task_id=task_id,
            state=RunState.PLANNING,
        )
        intervened = StateEngine().intervene(initial)
        assert intervened.state is RunState.AWAITING_HUMAN_INTERVENTION
        assert intervened.suspended_state is RunState.PLANNING
        assert intervened.suspension_kind is not None
        assert intervened.suspension_kind.value == "INTERVENTION"
        assert intervened.suspension_context is None

        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO runs "
                        "(id, project_id, task_id, policy_version, state, version, "
                        "suspended_state, suspension_kind, suspension_context_schema_version, "
                        "suspension_context, local_remediation_count, remote_remediation_count, "
                        "token_budget, cost_budget_minor, duration_budget_seconds, database_state) "
                        "VALUES (:id, :project_id, :task_id, 1, :state, :version, "
                        ":suspended_state, :suspension_kind, NULL, NULL, 0, 0, 0, 0, 0, 'DISABLED')"
                    ),
                    {
                        "id": intervened.id,
                        "project_id": intervened.project_id,
                        "task_id": intervened.task_id,
                        "state": intervened.state.value,
                        "version": intervened.version,
                        "suspended_state": intervened.suspended_state.value
                        if intervened.suspended_state is not None
                        else None,
                        "suspension_kind": intervened.suspension_kind.value
                        if intervened.suspension_kind is not None
                        else None,
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_suspended_state_matches_exact_state_engine_source_sets(
    migrated_database_url: str,
) -> None:
    async def exercise() -> None:
        project_id, task_id, _ = await _insert_project_policy_task_run(migrated_database_url)
        engine = create_async_engine(migrated_database_url)
        state_engine = StateEngine()
        terminal_states = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
        pause_sources = set(RunState) - terminal_states - {RunState.PAUSED}
        intervention_sources = {
            source
            for source, targets in LEGAL.items()
            if RunState.AWAITING_HUMAN_INTERVENTION in targets
        }
        insert_statement = text(
            "INSERT INTO runs "
            "(id, project_id, task_id, policy_version, state, version, suspended_state, "
            "suspension_kind, suspension_context_schema_version, suspension_context, "
            "local_remediation_count, remote_remediation_count, token_budget, "
            "cost_budget_minor, duration_budget_seconds, database_state) "
            "VALUES (:id, :project_id, :task_id, 1, :state, :version, :suspended_state, "
            ":suspension_kind, :context_version, CAST(:context AS jsonb), "
            "0, 0, 0, 0, 0, 'DISABLED')"
        )

        def parameters(snapshot: RunSnapshot) -> dict[str, object]:
            context = snapshot.suspension_context
            serialized_context = None
            if context is not None:
                serialized_context = json.dumps(
                    {
                        "state": context.state.value,
                        "suspended_state": context.suspended_state.value
                        if context.suspended_state is not None
                        else None,
                        "suspension_kind": context.suspension_kind.value
                        if context.suspension_kind is not None
                        else None,
                    }
                )
            return {
                "id": snapshot.id,
                "project_id": snapshot.project_id,
                "task_id": snapshot.task_id,
                "state": snapshot.state.value,
                "version": snapshot.version,
                "suspended_state": snapshot.suspended_state.value
                if snapshot.suspended_state is not None
                else None,
                "suspension_kind": snapshot.suspension_kind.value
                if snapshot.suspension_kind is not None
                else None,
                "context_version": 1 if context is not None else None,
                "context": serialized_context,
            }

        try:
            async with engine.begin() as connection:
                for source in pause_sources:
                    if source is RunState.AWAITING_HUMAN_INTERVENTION:
                        active = state_engine.intervene(
                            RunSnapshot(
                                id=uuid4(),
                                project_id=project_id,
                                task_id=task_id,
                                state=RunState.PLANNING,
                            )
                        )
                    else:
                        active = RunSnapshot(
                            id=uuid4(),
                            project_id=project_id,
                            task_id=task_id,
                            state=source,
                        )
                    await connection.execute(
                        insert_statement, parameters(state_engine.pause(active))
                    )

                for source in intervention_sources:
                    active = RunSnapshot(
                        id=uuid4(),
                        project_id=project_id,
                        task_id=task_id,
                        state=source,
                    )
                    await connection.execute(
                        insert_statement,
                        parameters(state_engine.intervene(active)),
                    )
        finally:
            await engine.dispose()

        invalid_rows = (
            ("PAUSED", "NOT_A_STATE", "PAUSE", 1, '{"state":"CREATED"}'),
            ("PAUSED", "PAUSED", "PAUSE", 1, '{"state":"PAUSED"}'),
            *(
                ("PAUSED", state.value, "PAUSE", 1, f'{{"state":"{state.value}"}}')
                for state in terminal_states
            ),
            ("AWAITING_HUMAN_INTERVENTION", "CREATED", "INTERVENTION", None, None),
        )
        for state, suspended_state, kind, context_version, context in invalid_rows:
            await _rejects(
                migrated_database_url,
                str(insert_statement),
                {
                    "id": uuid4(),
                    "project_id": project_id,
                    "task_id": task_id,
                    "state": state,
                    "version": 1,
                    "suspended_state": suspended_state,
                    "suspension_kind": kind,
                    "context_version": context_version,
                    "context": context,
                },
            )

    asyncio.run(exercise())


@pytest.mark.integration
def test_runs_require_task_and_project_to_match(
    migrated_database_url: str,
) -> None:
    async def exercise() -> None:
        first_project, task_id, _ = await _insert_project_policy_task_run(migrated_database_url)
        second_project, _, _ = await _insert_project_policy_task_run(migrated_database_url)
        await _rejects(
            migrated_database_url,
            "INSERT INTO runs "
            "(id, project_id, task_id, policy_version, state, version, "
            "local_remediation_count, remote_remediation_count, token_budget, "
            "cost_budget_minor, duration_budget_seconds, database_state) "
            "VALUES (:id, :project_id, :task_id, 1, 'CREATED', 0, 0, 0, 0, 0, 0, 'DISABLED')",
            {"id": uuid4(), "project_id": second_project, "task_id": task_id},
        )
        # Keep the first project referenced so the setup is explicit and not accidental.
        assert first_project != second_project

    asyncio.run(exercise())


@pytest.mark.integration
def test_policy_versions_are_immutable_but_new_versions_append(
    migrated_database_url: str,
) -> None:
    async def exercise() -> None:
        project_id, _, _ = await _insert_project_policy_task_run(migrated_database_url)
        await _rejects(
            migrated_database_url,
            "UPDATE project_policy_versions SET policy_digest = :digest "
            "WHERE project_id = :project_id AND version = 1",
            {"digest": "c" * 64, "project_id": project_id},
        )
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO project_policy_versions "
                        "(project_id, version, policy_digest, document_schema_version, document) "
                        "VALUES (:project_id, 2, :digest, 1, '{}'::jsonb)"
                    ),
                    {"project_id": project_id, "digest": "d" * 64},
                )
        finally:
            await engine.dispose()
        await _rejects(
            migrated_database_url,
            "DELETE FROM project_policy_versions WHERE project_id = :project_id AND version = 2",
            {"project_id": project_id},
        )
        await _rejects(
            migrated_database_url,
            "DELETE FROM projects WHERE id = :project_id",
            {"project_id": project_id},
        )

    asyncio.run(exercise())
