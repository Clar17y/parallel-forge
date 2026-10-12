"""Real PostgreSQL rollback checks for durable monitoring evidence."""

import asyncio
from uuid import UUID, uuid4

import pytest
from alembic import command
from forge.persistence.database import create_engine
from sqlalchemy import text

from tests.acceptance.test_task_usage_monitoring_process import _seed_checkpoint

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _snapshot(url: str, run_id: str) -> dict[str, object]:
    engine = create_engine(url)
    try:
        async with engine.connect() as db:
            result: dict[str, object] = {}
            tables = {
                "task_usage_policy_revisions": "project_id = (SELECT project_id FROM runs WHERE id = :run)",
                "task_usage_work_units": "subject_id = :run",
                "task_usage_observations": "work_unit_id IN (SELECT id FROM task_usage_work_units WHERE subject_id = :run)",
                "task_usage_baselines": "subject_id = :run",
                "task_usage_checkpoints": "subject_id = :run",
                "task_usage_owner_commands": "checkpoint_id IN (SELECT id FROM task_usage_checkpoints WHERE subject_id = :run)",
                "task_usage_subject_admissions": "subject_id = :run",
                "model_usage": "run_id = :run",
            }
            for table, predicate in tables.items():
                order = "revision" if table == "task_usage_policy_revisions" else (
                    "work_unit_id, sequence" if table == "task_usage_observations" else (
                    "project_id, subject_kind, subject_id" if table == "task_usage_subject_admissions" else (
                    "work_unit_id" if table == "task_usage_baselines" else "id"
                )))
                result[table] = (await db.execute(text(
                    f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY {order}), '[]'::jsonb) "
                    f"FROM {table} t WHERE {predicate}"
                ), {"run": UUID(run_id)})).scalar_one()
            result["revision"] = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            return result
    finally:
        await engine.dispose()


def test_populated_admission_refuses_downgrade_without_changing_rows(
    test_database_url: str, alembic_config_factory,
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "head")
    run_id, checkpoint_id = asyncio.run(_seed_checkpoint(
        test_database_url, generation=2, usage_pauses_disabled=True,
        monitoring_policy_revision=1,
    ))

    async def admit() -> None:
        engine = create_engine(test_database_url)
        try:
            async with engine.begin() as db:
                await db.execute(text(
                    "INSERT INTO task_usage_subject_admissions "
                    "(project_id, subject_kind, subject_id, run_id, policy_revision, configured_route_digest, configured_route_members, admitted_at) "
                    "SELECT project_id, 'run', id, id, 1, :digest, '[]'::jsonb, now() FROM runs WHERE id = :run"
                ), {"run": UUID(run_id), "digest": "a" * 64})
                await db.execute(text(
                    "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, "
                    "counter_kind, scope, unknown_dimensions, counter_epoch, final, observed_at, schema_version) "
                    "SELECT :id, project_id, id, 'metadata-event', 2, 'delta', 'source', "
                    "'[\"output_tokens\"]'::jsonb, 'attempt-2', false, now(), 1 "
                    "FROM task_usage_work_units WHERE subject_id = :run"
                ), {"id": uuid4(), "run": UUID(run_id)})
                await db.execute(text(
                    "INSERT INTO task_usage_owner_commands "
                    "(id, project_id, checkpoint_id, idempotency_key, action, expected_version, "
                    "result_version, actor_id, state, command_payload, warnings, affected_snapshot_digest) "
                    "SELECT :id, project_id, id, 'retained-stop', 'stop', 0, 1, :actor, "
                    "'applied', '{\"reason\":\"owner stop\"}'::jsonb, '[\"cancel pending\"]'::jsonb, :digest "
                    "FROM task_usage_checkpoints WHERE id = :checkpoint"
                ), {"id": uuid4(), "actor": uuid4(), "digest": "d" * 64,
                    "checkpoint": UUID(checkpoint_id)})
                await db.execute(text(
                    "INSERT INTO model_usage (id, run_id, agent_execution_id, provider, model, prompt_version, "
                    "input_tokens, output_tokens, cached_input_tokens, input_tokens_complete, "
                    "output_tokens_complete, cached_input_tokens_complete, duration_ms, tool_call_count, "
                    "pricing_version, currency, estimated_cost_minor) "
                    "SELECT :id, run_id, id, 'fixture', 'fixture', 'v1', 100, 0, 0, true, true, "
                    "true, 1, 0, 'fixture', 'USD', 0 FROM agent_executions WHERE run_id = :run"
                ), {"id": uuid4(), "run": UUID(run_id)})
        finally:
            await engine.dispose()

    asyncio.run(admit())
    before = asyncio.run(_snapshot(test_database_url, run_id))
    assert all(before[table] for table in before if table != "revision")
    assert before["task_usage_checkpoints"][0]["generation"] == 2
    assert before["task_usage_checkpoints"][0]["usage_pauses_disabled"] is True
    assert before["task_usage_observations"][1]["counter_epoch"] == "attempt-2"
    assert before["task_usage_owner_commands"][0]["action"] == "stop"
    with pytest.raises(RuntimeError, match="explicit provider telemetry evidence"):
        command.downgrade(config, "20261010_0039")
    assert asyncio.run(_snapshot(test_database_url, run_id)) == before


def test_compatible_checkpoint_downgrade_preserves_prior_rows(
    test_database_url: str, alembic_config_factory,
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "head")
    run_id, _ = asyncio.run(_seed_checkpoint(test_database_url))
    before = asyncio.run(_snapshot(test_database_url, run_id))
    command.downgrade(config, "20261010_0039")
    engine = create_engine(test_database_url)

    async def old_snapshot() -> tuple[object, ...]:
        try:
            async with engine.connect() as db:
                return (
                    (await db.execute(text("SELECT state, version, process_settled FROM task_usage_checkpoints WHERE subject_id = :run"), {"run": UUID(run_id)})).one(),
                    (await db.execute(text("SELECT count(*) FROM task_usage_observations WHERE work_unit_id IN (SELECT id FROM task_usage_work_units WHERE subject_id = :run)"), {"run": UUID(run_id)})).scalar_one(),
                )
        finally:
            await engine.dispose()

    prior = asyncio.run(old_snapshot())
    checkpoint = before["task_usage_checkpoints"][0]
    assert prior == ((checkpoint["state"], checkpoint["version"], checkpoint["process_settled"]),
                     len(before["task_usage_observations"]))
    command.upgrade(config, "head")
    restored = asyncio.run(_snapshot(test_database_url, run_id))
    for table in before:
        assert restored[table] == before[table], table


@pytest.mark.parametrize(("kind", "message"), [
    ("admission", "admission snapshots"),
    ("stop", "stop commands"),
    ("continuation", "checkpoint continuation"),
])
def test_each_new_checkpoint_guard_refuses_without_losing_receipts(
    test_database_url: str, alembic_config_factory, kind: str, message: str,
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "head")
    run_id, checkpoint_id = asyncio.run(_seed_checkpoint(
        test_database_url, generation=2 if kind == "continuation" else 1,
        next_threshold=125 if kind == "continuation" else None,
    ))

    async def retain() -> None:
        engine = create_engine(test_database_url)
        try:
            async with engine.begin() as db:
                if kind == "admission":
                    await db.execute(text(
                        "INSERT INTO task_usage_subject_admissions "
                        "(project_id, subject_kind, subject_id, run_id, policy_revision, "
                        "configured_route_digest, configured_route_members, admitted_at) "
                        "SELECT project_id, 'run', id, id, 1, :digest, '[]'::jsonb, now() "
                        "FROM runs WHERE id = :run"
                    ), {"run": UUID(run_id), "digest": "a" * 64})
                if kind == "stop":
                    await db.execute(text(
                        "INSERT INTO task_usage_owner_commands "
                        "(id, project_id, checkpoint_id, idempotency_key, action, expected_version, "
                        "result_version, actor_id, state, command_payload, warnings, affected_snapshot_digest) "
                        "SELECT :id, project_id, id, 'stop-only', 'stop', 0, 1, :actor, 'applied', "
                        "'{}'::jsonb, '[]'::jsonb, :digest FROM task_usage_checkpoints WHERE id = :checkpoint"
                    ), {"id": uuid4(), "actor": uuid4(), "digest": "d" * 64,
                        "checkpoint": UUID(checkpoint_id)})
        finally:
            await engine.dispose()

    asyncio.run(retain())
    before = asyncio.run(_snapshot(test_database_url, run_id))
    with pytest.raises(RuntimeError, match=message):
        command.downgrade(config, "20261010_0039")
    assert asyncio.run(_snapshot(test_database_url, run_id)) == before
