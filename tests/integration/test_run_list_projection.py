"""PostgreSQL projection aggregation and filtering contracts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.run import RunState
from forge.persistence.models import ModelUsage, PullRequest, Run
from forge.persistence.queries.run_list import RunListQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def test_run_list_aggregates_costs_and_keeps_latest_pr_unique(session_factory, persisted_run):
    run = persisted_run
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                PullRequest(
                    run_id=run.id,
                    repository="owner/repo",
                    branch="feature/a",
                    base_ref="main",
                    pull_request_number=1,
                    head_sha="a" * 40,
                    base_sha="b" * 40,
                    checks={},
                    review_state={},
                    state="OPEN",
                    created_at=now - timedelta(seconds=3),
                ),
                PullRequest(
                    run_id=run.id,
                    repository="owner/repo",
                    branch="feature/a",
                    base_ref="main",
                    pull_request_number=2,
                    head_sha="c" * 40,
                    base_sha="b" * 40,
                    checks={},
                    review_state={},
                    state="OPEN",
                    created_at=now - timedelta(seconds=2),
                ),
            ]
        )
        await session.flush()
        prs = list(await session.scalars(select(PullRequest).where(PullRequest.run_id == run.id)))
        prs[0].updated_at = now - timedelta(seconds=2)
        prs[1].updated_at = now - timedelta(seconds=1)
        await session.flush()
        executions = []
        from forge.persistence.models import AgentExecution

        for i in range(3):
            execution = AgentExecution(
                run_id=run.id,
                role="planner",
                provider="fake",
                model="model",
                instruction_version="v1",
                status="SUCCEEDED",
                input_artifact_id=None,
            )
            executions.append(execution)
        session.add_all(executions)
        await session.flush()
        session.add_all(
            [
                ModelUsage(
                    run_id=run.id,
                    agent_execution_id=executions[0].id,
                    provider="fake",
                    model="model",
                    prompt_version="v1",
                    input_tokens=1,
                    output_tokens=1,
                    duration_ms=1,
                    pricing_version="v1",
                    estimated_cost_minor=10,
                    currency="USD",
                ),
                ModelUsage(
                    run_id=run.id,
                    agent_execution_id=executions[1].id,
                    provider="fake",
                    model="model",
                    prompt_version="v1",
                    input_tokens=1,
                    output_tokens=1,
                    duration_ms=1,
                    pricing_version="v1",
                    estimated_cost_minor=None,
                    unknown_price_reason="unknown",
                    currency="USD",
                ),
                ModelUsage(
                    run_id=run.id,
                    agent_execution_id=executions[2].id,
                    provider="fake",
                    model="model",
                    prompt_version="v1",
                    input_tokens=1,
                    output_tokens=1,
                    duration_ms=1,
                    pricing_version="v1",
                    estimated_cost_minor=4,
                    currency="EUR",
                ),
            ]
        )
    rows, truncated = await RunListQuery(session_factory).list(limit=1)
    assert truncated is False
    item = next(row for row in rows if row["run_id"] == run.id)
    assert item["pull_request"]["number"] == 2
    assert item["cost_summary"]["unpriced_calls"] == 1
    assert {c["currency"] for c in item["cost_summary"]["currencies"]} == {"USD", "EUR"}


async def test_active_elapsed_advances_beyond_last_mutation(session_factory, persisted_run):
    async with session_factory() as session, session.begin():
        row = await session.get(Run, persisted_run.id)
        row.created_at = datetime.now(UTC) - timedelta(minutes=30)
        row.updated_at = row.created_at + timedelta(seconds=1)
    items, _ = await RunListQuery(session_factory).list()
    assert items[0]["elapsed_ms"] >= 30 * 60 * 1000


async def test_run_list_filters_state_and_project_and_truncates(session_factory, persisted_run):
    rows, truncated = await RunListQuery(session_factory).list(
        state=RunState.CREATED, project_id=persisted_run.project_id, limit=1
    )
    assert len(rows) == 1
    assert rows[0]["run_id"] == persisted_run.id
    assert truncated is False


async def test_run_list_page_boundary_attention_and_updated_filters(session_factory, persisted_run):
    newer = replace(persisted_run, id=uuid4())
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(newer)
        await work.commit()
    cutoff = datetime(2026, 1, 2, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        old_row = await session.get(Run, persisted_run.id)
        new_row = await session.get(Run, newer.id)
        new_row.state = RunState.FAILED
        old_row.updated_at = cutoff - timedelta(days=1)
        new_row.updated_at = cutoff + timedelta(days=1)
    query = RunListQuery(session_factory)
    first, more = await query.list(limit=1)
    second, end = await query.list(limit=1, offset=1)
    assert more is True and end is False
    assert first[0]["run_id"] == newer.id and second[0]["run_id"] == persisted_run.id
    recent, _ = await query.list(updated_since=cutoff)
    assert [row["run_id"] for row in recent] == [newer.id]
    attention, _ = await query.list(attention=True)
    assert [row["run_id"] for row in attention] == [newer.id]
    unattended, _ = await query.list(attention=False)
    assert [row["run_id"] for row in unattended] == [persisted_run.id]
    rows, truncated = await RunListQuery(session_factory).list(project_id=uuid4(), limit=1)
    assert rows == []
    assert truncated is False
