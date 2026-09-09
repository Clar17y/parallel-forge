"""Evaluation migration schema contract."""

from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.asyncio


async def _insert_suite(session, suite_id, key="run-1", fixture="v1", metric="m1"):
    await session.execute(
        text(
            "INSERT INTO evaluation_suites "
            "(id,name,fixture_version,metric_version,status,idempotency_key) "
            "VALUES (:id,'suite',:fixture,:metric,'pending',:key)"
        ),
        {"id": suite_id, "fixture": fixture, "metric": metric, "key": key},
    )


async def test_evaluation_tables_have_versioned_case_identity(session_factory):
    async with session_factory() as session:
        tables = await session.run_sync(lambda sync: set(inspect(sync.bind).get_table_names()))
        columns = await session.run_sync(
            lambda sync: {
                item["name"] for item in inspect(sync.bind).get_columns("evaluation_cases")
            }
        )
        constraints = await session.run_sync(
            lambda sync: {
                item["name"]
                for item in inspect(sync.bind).get_check_constraints("evaluation_cases")
            }
        )
        suite_constraints = await session.run_sync(
            lambda sync: {
                item["name"]
                for item in inspect(sync.bind).get_check_constraints("evaluation_suites")
            }
        )
    assert {"evaluation_suites", "evaluation_cases"} <= tables
    assert {
        "suite_id",
        "case_key",
        "fixture_version",
        "metric_version",
        "metrics",
        "model_usage_id",
        "input_artifact_digest",
        "output_artifact_digest",
    } <= columns
    assert all(
        any(name.endswith(expected) for name in constraints)
        for expected in (
            "evaluation_case_role",
            "evaluation_case_status",
            "evaluation_case_input_digest",
            "evaluation_case_output_digest",
            "evaluation_case_versions_nonempty",
        )
    )
    assert all(
        any(name.endswith(expected) for name in suite_constraints)
        for expected in ("evaluation_suite_status", "evaluation_suite_versions_nonempty")
    )


async def test_evaluation_valid_case_and_identity_constraints(session_factory) -> None:
    suite_id, other_id, case_id = uuid4(), uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        await _insert_suite(session, suite_id)
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await _insert_suite(session, uuid4())
        await _insert_suite(session, other_id, key="run-2")
        await session.execute(
            text(
                "INSERT INTO evaluation_cases "
                "(id,suite_id,case_key,fixture_version,metric_version,role,status,metrics) "
                "VALUES (:id,:suite,'case-1','v1','m1','planner','pending','{}')"
            ),
            {"id": case_id, "suite": suite_id},
        )
        await session.execute(
            text("UPDATE evaluation_cases SET status='passed' WHERE id=:id"), {"id": case_id}
        )
        for sql, params in (
            (
                "UPDATE evaluation_cases SET id=:other WHERE id=:id",
                {"other": uuid4(), "id": case_id},
            ),
            (
                "UPDATE evaluation_cases SET suite_id=:suite WHERE id=:id",
                {"suite": other_id, "id": case_id},
            ),
            ("UPDATE evaluation_cases SET case_key='case-2' WHERE id=:id", {"id": case_id}),
        ):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(text(sql), params)


async def test_evaluation_rejects_invalid_values_and_duplicate_identity(session_factory) -> None:
    suite_id, case_id = uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        await _insert_suite(session, suite_id)
        for column, value in (
            ("role", "unknown"),
            ("status", "unknown"),
            ("input_artifact_digest", "A" * 64),
        ):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "INSERT INTO evaluation_cases "
                            "(id,suite_id,case_key,fixture_version,metric_version,role,status,metrics,input_artifact_digest) "
                            "VALUES (:id,:suite,:key,'v1','m1',:role,:status,'{}',:digest)"
                        ),
                        {
                            "id": uuid4(),
                            "suite": suite_id,
                            "key": column,
                            "role": value if column == "role" else "planner",
                            "status": value if column == "status" else "pending",
                            "digest": value if column == "input_artifact_digest" else None,
                        },
                    )
        await session.execute(
            text(
                "INSERT INTO evaluation_cases (id,suite_id,case_key,fixture_version,metric_version,role,status,metrics) "
                "VALUES (:id,:suite,'duplicate','v1','m1','planner','pending','{}')"
            ),
            {"id": case_id, "suite": suite_id},
        )
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO evaluation_cases (id,suite_id,case_key,fixture_version,metric_version,role,status,metrics) "
                        "VALUES (:id,:suite,'duplicate','v1','m1','planner','pending','{}')"
                    ),
                    {"id": uuid4(), "suite": suite_id},
                )
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO evaluation_cases (id,suite_id,case_key,fixture_version,metric_version,role,status,metrics) "
                        "VALUES (:id,:suite,'wrong-version','other','m1','planner','pending','{}')"
                    ),
                    {"id": uuid4(), "suite": suite_id},
                )


async def test_empty_suite_identity_is_immutable(session_factory) -> None:
    suite_id = uuid4()
    async with session_factory() as session, session.begin():
        await _insert_suite(session, suite_id)
        await session.execute(
            text("UPDATE evaluation_suites SET name='renamed', status='running' WHERE id=:id"),
            {"id": suite_id},
        )
        for assignment in (
            "id=:other",
            "fixture_version='v2'",
            "metric_version='m2'",
            "idempotency_key='changed'",
        ):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text(f"UPDATE evaluation_suites SET {assignment} WHERE id=:id"),
                        {"id": suite_id, "other": uuid4()},
                    )
