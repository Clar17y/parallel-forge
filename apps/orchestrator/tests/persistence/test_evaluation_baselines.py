"""Tests for evaluation baselines persistence, promotion, and immutability."""

from uuid import uuid4

import pytest
from forge.persistence.repositories.evaluations import (
    EvaluationConflict,
    EvaluationRepository,
)
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.asyncio


async def _insert_test_suite_and_case(
    session,
    suite_id,
    *,
    name="live",
    status="passed",
    case_status="passed",
    fixture_version="eval-fixture-v1",
    metric_version="eval-metrics-v1",
    case_key="developer/basic-change",
    role="developer",
):
    await session.execute(
        text(
            "INSERT INTO evaluation_suites "
            "(id, name, fixture_version, metric_version, status, idempotency_key) "
            "VALUES (:id, :name, :fixture, :metric, :status, :key)"
        ),
        {
            "id": suite_id,
            "name": name,
            "fixture": fixture_version,
            "metric": metric_version,
            "status": status,
            "key": f"key-{suite_id}",
        },
    )
    case_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO evaluation_cases "
            "(id, suite_id, case_key, fixture_version, metric_version, role, status, metrics) "
            "VALUES (:id, :suite_id, :case_key, :fixture, :metric, :role, :status, :metrics)"
        ),
        {
            "id": case_id,
            "suite_id": suite_id,
            "case_key": case_key,
            "fixture": fixture_version,
            "metric": metric_version,
            "role": role,
            "status": case_status,
            "metrics": '{"required_test_pass": 1.0, "duration_ms": 500}',
        },
    )


async def test_evaluation_baselines_table_schema(session_factory):
    async with session_factory() as session:
        tables = await session.run_sync(lambda sync: set(inspect(sync.bind).get_table_names()))
        assert "evaluation_baselines" in tables
        columns = await session.run_sync(
            lambda sync: {
                item["name"] for item in inspect(sync.bind).get_columns("evaluation_baselines")
            }
        )
        assert {
            "id",
            "suite_id",
            "name",
            "fixture_version",
            "metric_version",
            "cases",
            "floors",
            "ceilings",
            "promoted_by",
            "promoted_at",
        } <= columns


async def test_promote_passed_suite_as_durable_baseline(session_factory):
    suite_id = uuid4()
    async with session_factory() as session, session.begin():
        await _insert_test_suite_and_case(session, suite_id, name="live", status="passed")
        repo = EvaluationRepository(session)
        baseline = await repo.promote_baseline(
            suite_id=suite_id,
            name="live",
            floors={"required_test_pass": 1.0},
            ceilings={"duration_ms": 1000.0},
            promoted_by="operator-alice",
        )
        assert baseline.suite_id == suite_id
        assert baseline.name == "live"
        assert baseline.fixture_version == "eval-fixture-v1"
        assert baseline.metric_version == "eval-metrics-v1"
        assert "developer/basic-change" in baseline.cases
        assert baseline.floors["required_test_pass"] == 1.0
        assert baseline.ceilings["duration_ms"] == 1000.0
        assert baseline.promoted_by == "operator-alice"

    # Query back
    async with session_factory() as session:
        repo = EvaluationRepository(session)
        loaded = await repo.get_baseline(name="live", fixture_version="eval-fixture-v1", metric_version="eval-metrics-v1")
        assert loaded is not None
        assert loaded.id == baseline.id
        assert loaded.suite_id == suite_id
        assert loaded.cases["developer/basic-change"]["status"] == "passed"


async def test_promote_rejects_non_passed_or_unsettled_suite(session_factory):
    failed_suite_id = uuid4()
    running_suite_id = uuid4()
    unsettled_case_suite_id = uuid4()

    async with session_factory() as session, session.begin():
        await _insert_test_suite_and_case(session, failed_suite_id, status="failed", case_status="failed")
        await _insert_test_suite_and_case(session, running_suite_id, status="running", case_status="passed")
        await _insert_test_suite_and_case(session, unsettled_case_suite_id, status="passed", case_status="pending")

        repo = EvaluationRepository(session)
        with pytest.raises(EvaluationConflict, match="cannot promote non-passed"):
            await repo.promote_baseline(suite_id=failed_suite_id, name="base-fail")

        with pytest.raises(EvaluationConflict, match="cannot promote non-passed"):
            await repo.promote_baseline(suite_id=running_suite_id, name="base-running")

        with pytest.raises(EvaluationConflict, match="non-passed cases"):
            await repo.promote_baseline(suite_id=unsettled_case_suite_id, name="base-unsettled")


@pytest.mark.parametrize("assignment", [
    "name='tampered'", "promoted_by='tampered'", "promoted_at=promoted_at + interval '1 second'",
])
async def test_evaluation_baseline_is_immutable(session_factory, assignment):
    suite_id = uuid4()
    async with session_factory() as session, session.begin():
        await _insert_test_suite_and_case(session, suite_id, status="passed")
        repo = EvaluationRepository(session)
        baseline = await repo.promote_baseline(suite_id=suite_id, name="immutable-base")

    async with session_factory() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.execute(
                text(f"UPDATE evaluation_baselines SET {assignment} WHERE id=:id"),
                {"id": baseline.id},
            )


async def test_baseline_delete_is_rejected(session_factory):
    suite_id = uuid4()
    async with session_factory() as session, session.begin():
        await _insert_test_suite_and_case(session, suite_id)
        baseline = await EvaluationRepository(session).promote_baseline(
            suite_id=suite_id, name="retained-baseline"
        )
    async with session_factory() as session, session.begin():
        with pytest.raises(DBAPIError, match="immutable"):
            await session.execute(
                text("DELETE FROM evaluation_baselines WHERE id=:id"), {"id": baseline.id}
            )


@pytest.mark.parametrize("same_suite", [True, False])
async def test_duplicate_baseline_promotion_is_domain_conflict(session_factory, same_suite):
    first, second = uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        await _insert_test_suite_and_case(session, first)
        await _insert_test_suite_and_case(session, second)
        repo = EvaluationRepository(session)
        original = await repo.promote_baseline(suite_id=first, name="unique-baseline")
        with pytest.raises(EvaluationConflict, match="already"):
            await repo.promote_baseline(
                suite_id=first if same_suite else second,
                name="other-name" if same_suite else "unique-baseline",
            )
        assert (await repo.get_baseline(baseline_id=original.id)).id == original.id
