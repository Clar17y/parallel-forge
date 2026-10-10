"""PostgreSQL compatibility for the serialized accounting/policy migration."""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from forge.persistence.database import create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .test_migration import _legacy_usage


@pytest.mark.integration
def test_execution_monitoring_settings_are_optional_immutable_and_project_bound(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "20261010_0038")
    asyncio.run(_legacy_usage(test_database_url))
    command.upgrade(config, "20261010_0039")

    async def verify():
        engine = create_engine(test_database_url)
        try:
            async with engine.begin() as connection:
                legacy = (
                    await connection.execute(
                        text(
                            "SELECT id,run_id,monitoring_policy_revision,hard_token_cap FROM agent_executions LIMIT 1"
                        )
                    )
                ).one()
                assert tuple(legacy[2:]) == (None, None)
                shape = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT column_name,data_type FROM information_schema.columns WHERE table_name='agent_executions'"
                            )
                        )
                    ).all()
                )
                assert shape["monitoring_policy_revision"] == shape["hard_token_cap"] == "bigint"
                project = await connection.scalar(
                    text("SELECT project_id FROM runs WHERE id=:run"), {"run": legacy.run_id}
                )
                await connection.execute(
                    text(
                        "INSERT INTO task_usage_policy_revisions (project_id,revision,mode,warning_multiplier,checkpoint_multiplier,history_limit,minimum_comparables,actor_id) "
                        "VALUES (:project,1,'warn',1.5,2,50,20,:actor)"
                    ),
                    {"project": project, "actor": uuid4()},
                )
                insert = text(
                    "INSERT INTO agent_executions (id,run_id,role,instruction_version,provider,model,status,monitoring_policy_revision,hard_token_cap) "
                    "VALUES (:id,:run,'planner','1','test','test','RUNNING',:revision,:cap)"
                )
                args = {"id": uuid4(), "run": legacy.run_id, "revision": 1, "cap": 2**63 - 1}
                with pytest.raises(IntegrityError, match="does not belong to the run project"):
                    async with connection.begin_nested():
                        await connection.execute(insert, {**args, "id": uuid4(), "revision": 2})
                await connection.execute(insert, args)
                for column in ("hard_token_cap", "monitoring_policy_revision"):
                    with pytest.raises(IntegrityError, match="settings are immutable"):
                        async with connection.begin_nested():
                            await connection.execute(
                                text(f"UPDATE agent_executions SET {column}=NULL WHERE id=:id"),
                                {"id": args["id"]},
                            )
                await connection.execute(
                    text("UPDATE agent_executions SET status='SUCCEEDED' WHERE id=:id"),
                    {"id": args["id"]},
                )
                frozen = (
                    await connection.execute(
                        text(
                            "SELECT monitoring_policy_revision,hard_token_cap FROM agent_executions WHERE id=:id"
                        ),
                        {"id": args["id"]},
                    )
                ).one()
                assert tuple(frozen) == (1, 2**63 - 1)
        finally:
            await engine.dispose()

    asyncio.run(verify())
    with pytest.raises(RuntimeError, match="without losing retained evidence or owner settings"):
        command.downgrade(config, "20261010_0038")


@pytest.mark.integration
def test_optional_run_cap_uses_nullable_signed64_storage_without_rewriting_history(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "20261010_0038")
    asyncio.run(_legacy_usage(test_database_url))
    command.upgrade(config, "20261010_0039")

    async def verify():
        engine = create_engine(test_database_url)
        try:
            async with engine.begin() as connection:
                run_id = await connection.scalar(text("SELECT id FROM runs LIMIT 1"))
                assert (
                    await connection.scalar(
                        text("SELECT token_budget FROM runs WHERE id=:id"), {"id": run_id}
                    )
                    == 0
                )
                shape = (
                    await connection.execute(
                        text(
                            "SELECT data_type,is_nullable FROM information_schema.columns "
                            "WHERE table_name='runs' AND column_name='token_budget'"
                        )
                    )
                ).one()
                assert tuple(shape) == ("bigint", "YES")
                await connection.execute(
                    text("UPDATE runs SET token_budget=NULL WHERE id=:id"), {"id": run_id}
                )
                assert (
                    await connection.scalar(
                        text("SELECT token_budget FROM runs WHERE id=:id"), {"id": run_id}
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(verify())
    with pytest.raises(RuntimeError, match="without losing retained evidence or owner settings"):
        command.downgrade(config, "20261010_0038")

    async def maximum():
        engine = create_engine(test_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE runs SET token_budget=:cap"), {"cap": 2**63 - 1}
                )
                assert (
                    await connection.scalar(text("SELECT token_budget FROM runs LIMIT 1"))
                    == 2**63 - 1
                )
        finally:
            await engine.dispose()

    asyncio.run(maximum())
    with pytest.raises(RuntimeError, match="without losing retained evidence or owner settings"):
        command.downgrade(config, "20261010_0038")


@pytest.mark.integration
def test_upgrade_keeps_usage_and_new_policy_settings_survive_refused_downgrade(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "20261010_0038")
    legacy_id = asyncio.run(_legacy_usage(test_database_url))
    command.upgrade(config, "20261010_0039")

    async def write_and_inspect():
        engine = create_engine(test_database_url)
        try:
            async with engine.begin() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT input_tokens FROM model_usage WHERE id=:id"), {"id": legacy_id}
                    )
                    == 17
                )
                columns = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT column_name, data_type FROM information_schema.columns "
                                "WHERE table_name = 'task_usage_policy_revisions'"
                            )
                        )
                    ).all()
                )
                assert columns["manual_estimate_tokens"] == "bigint"
                assert columns["window_days"] == "bigint"
                assert columns["allow_cross_project_fallback"] == "boolean"
                counters = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT column_name, data_type FROM information_schema.columns "
                                "WHERE table_name = 'task_usage_baselines'"
                            )
                        )
                    ).all()
                )
                assert all(
                    counters[name] == "bigint"
                    for name in ("coverage_count", "outcome_count", "source_count")
                )
                project_id = await connection.scalar(text("SELECT project_id FROM runs LIMIT 1"))
                await connection.execute(
                    text(
                        "INSERT INTO task_usage_policy_revisions "
                        "(project_id,revision,mode,warning_multiplier,checkpoint_multiplier,history_limit,"
                        "minimum_comparables,manual_estimate_tokens,window_days,allow_cross_project_fallback,actor_id) "
                        "VALUES (:project,1,'checkpoint',1.5,2,50,20,:estimate,:estimate,false,:actor)"
                    ),
                    {"project": project_id, "estimate": 2**63 - 1, "actor": uuid4()},
                )
                return project_id
        finally:
            await engine.dispose()

    project_id = asyncio.run(write_and_inspect())
    with pytest.raises(RuntimeError, match="without losing retained evidence or owner settings"):
        command.downgrade(config, "20261010_0038")

    async def verify_retention():
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "20261010_0039"
                )
                row = (
                    await connection.execute(
                        text(
                            "SELECT manual_estimate_tokens,window_days,allow_cross_project_fallback FROM task_usage_policy_revisions "
                            "WHERE project_id=:project"
                        ),
                        {"project": project_id},
                    )
                ).one()
                assert tuple(row) == (2**63 - 1, 2**63 - 1, False)
                assert (
                    await connection.scalar(
                        text("SELECT input_tokens FROM model_usage WHERE id=:id"), {"id": legacy_id}
                    )
                    == 17
                )
        finally:
            await engine.dispose()

    asyncio.run(verify_retention())


@pytest.mark.integration
def test_empty_batch_downgrade_reupgrade_preserves_legacy_usage(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "20261010_0038")
    legacy_id = asyncio.run(_legacy_usage(test_database_url))
    command.upgrade(config, "20261010_0039")
    command.downgrade(config, "20261010_0038")
    command.upgrade(config, "20261010_0039")

    async def verify():
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT input_tokens FROM model_usage WHERE id=:id"), {"id": legacy_id}
                    )
                    == 17
                )
                assert (
                    await connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "20261010_0039"
                )
        finally:
            await engine.dispose()

    asyncio.run(verify())
