"""Early subscription rollback boundaries retain durable work and launch receipts."""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_schema import _insert_project_policy_task_run

SCHEDULER_TABLES = (
    "subscription_scheduled_tasks",
    "subscription_scheduler_runs",
    "subscription_scheduled_effects",
    "subscription_scheduler_capacity_policies",
)


async def _snapshot(database_url):
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            columns = tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name, column_name, data_type, is_nullable "
                            "FROM information_schema.columns WHERE table_schema = 'public' "
                            "ORDER BY table_name, ordinal_position"
                        )
                    )
                ).all()
            )
            tables = {row[0] for row in columns}
            rows = {}
            for table in (
                "alembic_version",
                "runs",
                "subscription_tasks",
                "subscription_attempts",
                "subscription_client_launches",
                *SCHEDULER_TABLES,
            ):
                if table in tables:
                    rows[table] = (
                        (
                            await connection.execute(
                                text(
                                    f"SELECT to_jsonb(t) FROM {table} AS t ORDER BY to_jsonb(t)::text"
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
            return columns, rows
    finally:
        await engine.dispose()


async def _seed(database_url, *, kind, state="queued"):
    _, _, run_id = await _insert_project_policy_task_run(database_url)
    values = {"run": run_id, "task": uuid4(), "row": uuid4(), "attempt": uuid4(), "state": state}
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            # Keep the logical task after a partial downgrade to expose lost scheduling state.
            await connection.execute(
                text(
                    "INSERT INTO subscription_tasks "
                    "(id, run_id, task_id, idempotency_key, payload) "
                    "VALUES (:task, :run, :task, 'rollback-task', '{}'::jsonb)"
                ),
                values,
            )
            statements = {
                "task": (
                    "INSERT INTO subscription_scheduled_tasks "
                    "(id, run_id, task_id, worktree_id, provider, state) "
                    "VALUES (:row, :run, :task, 'managed-worktree', 'fake', :state)"
                ),
                "paused": (
                    "INSERT INTO subscription_scheduled_tasks "
                    "(id, run_id, task_id, worktree_id, provider, pause_requested) "
                    "VALUES (:row, :run, :task, 'managed-worktree', 'fake', true)"
                ),
                "cancelled": (
                    "INSERT INTO subscription_scheduled_tasks "
                    "(id, run_id, task_id, worktree_id, provider, cancel_requested) "
                    "VALUES (:row, :run, :task, 'managed-worktree', 'fake', true)"
                ),
                "run": (
                    "INSERT INTO subscription_scheduler_runs (run_id, admitted) VALUES (:run, true)"
                ),
                "effect": (
                    "INSERT INTO subscription_scheduled_effects "
                    "(id, run_id, task_id, lease_owner, lease_generation, candidate_epoch, state, "
                    "whole_worktree_exclusive) "
                    "VALUES (:row, :run, :task, 'worker', 1, 0, :state, false)"
                ),
                "capacity": (
                    "INSERT INTO subscription_scheduler_capacity_policies "
                    "(version, global_limit, run_limit, provider_limit) VALUES (1, 8, 4, 2)"
                ),
                "launch": (
                    "INSERT INTO subscription_client_launches "
                    "(id, attempt_id, launch_id, worker_identity, state) "
                    "VALUES (:row, :attempt, 'launch-1', 'worker', :state)"
                ),
            }
            if kind == "launch":
                await connection.execute(
                    text(
                        "INSERT INTO subscription_attempts "
                        "(id, run_id, task_row_id, attempt_number, idempotency_key, route_payload) "
                        "VALUES (:attempt, :run, :task, 1, 'rollback-attempt', '{}'::jsonb)"
                    ),
                    values,
                )
            await connection.execute(text(statements[kind]), values)
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    "revision,kind,state",
    [
        ("20260910_0009", "task", "queued"),
        *[
            ("head", "task", state)
            for state in ("queued", "leased", "blocked", "reconciling", "terminal")
        ],
        ("head", "paused", "queued"),
        ("head", "cancelled", "queued"),
        ("head", "run", "queued"),
        ("head", "capacity", "queued"),
        *[
            ("head", "effect", state)
            for state in ("admitted", "reconciling", "settled", "rejected")
        ],
    ],
)
def test_scheduler_downgrade_preserves_retained_state(
    test_database_url, alembic_config_factory, revision, kind, state
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, revision)
    asyncio.run(_seed(test_database_url, kind=kind, state=state))
    before = asyncio.run(_snapshot(test_database_url))

    with pytest.raises(RuntimeError, match="cannot discard retained subscription scheduler state"):
        command.downgrade(config, "20260910_0008")

    assert asyncio.run(_snapshot(test_database_url)) == before
    command.upgrade(config, "head")
    after = asyncio.run(_snapshot(test_database_url))
    for table in SCHEDULER_TABLES:
        assert after[1][table] == before[1][table]


@pytest.mark.integration
@pytest.mark.parametrize(
    "revision,state",
    [
        ("20260910_0010", "intent"),
        *[("head", state) for state in ("intent", "started", "uncertain", "terminal")],
    ],
)
def test_broker_downgrade_preserves_launch_receipts(
    test_database_url, alembic_config_factory, revision, state
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, revision)
    asyncio.run(_seed(test_database_url, kind="launch", state=state))
    before = asyncio.run(_snapshot(test_database_url))

    with pytest.raises(RuntimeError, match="cannot discard retained subscription client launches"):
        command.downgrade(config, "20260910_0009")

    assert asyncio.run(_snapshot(test_database_url)) == before
    command.upgrade(config, "head")
    assert (
        asyncio.run(_snapshot(test_database_url))[1]["subscription_client_launches"]
        == before[1]["subscription_client_launches"]
    )
