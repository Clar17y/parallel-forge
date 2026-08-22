"""Integration coverage for priced model-usage persistence and totals."""

from __future__ import annotations

from uuid import uuid4

import pytest
from forge.observability.usage import PricingCatalog, UsageRecord
from forge.persistence.models import AgentExecution, ModelUsage
from forge.persistence.repositories.usage import UsageRepository, UsageRepositoryError
from sqlalchemy.exc import IntegrityError


@pytest.mark.integration
async def test_usage_round_trip_and_grouped_totals(session_factory, persisted_run) -> None:
    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=persisted_run.id,
                role="planner",
                instruction_version="v1",
                provider="gemini",
                model="test",
                status="SUCCEEDED",
            )
        )

    catalog = PricingCatalog.from_mapping(
        version="v1",
        entries={"gemini:test": {"input_per_million": "1.25", "output_per_million": "5.00"}},
    )
    repository = UsageRepository(session_factory)
    record = await repository.record(
        persisted_run.id,
        execution_id,
        UsageRecord(provider="gemini", model="test", input_tokens=2_000_000, output_tokens=500_000),
        catalog=catalog,
        currency="USD",
    )

    assert record.estimated_cost_minor == 500
    assert record.id is not None
    assert await repository.get(record.id) == record
    totals = await repository.totals(persisted_run.id)
    assert totals == (
        {
            "provider": "gemini",
            "model": "test",
            "currency": "USD",
            "request_count": 1,
            "input_tokens": 2_000_000,
            "cached_input_tokens": 0,
            "output_tokens": 500_000,
            "duration_ms": 0,
            "tool_call_count": 0,
            "known_estimated_cost_minor": 500,
            "unknown_price_count": 0,
            "estimate_complete": True,
        },
    )


@pytest.mark.integration
async def test_unknown_price_is_persisted_without_becoming_zero(
    session_factory, persisted_run
) -> None:
    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=persisted_run.id,
                role="planner",
                instruction_version="v1",
                provider="provider",
                model="missing",
                status="SUCCEEDED",
            )
        )

    repository = UsageRepository(session_factory)
    record = await repository.record(
        persisted_run.id,
        execution_id,
        UsageRecord(provider="provider", model="missing", input_tokens=10),
        catalog=PricingCatalog.from_mapping(version="v1", entries={}),
        currency="USD",
    )

    assert record.estimated_cost_minor is None
    assert record.unknown_price_reason == "unknown_model_price"
    assert (await repository.totals(persisted_run.id))[0]["estimate_complete"] is False


@pytest.mark.integration
async def test_totals_expose_known_sum_and_unknown_count_without_claiming_completeness(
    session_factory, persisted_run
) -> None:
    execution_ids = (uuid4(), uuid4())
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                AgentExecution(
                    id=execution_id,
                    run_id=persisted_run.id,
                    role="planner",
                    instruction_version="v1",
                    provider="provider",
                    model="model",
                    status="SUCCEEDED",
                )
                for execution_id in execution_ids
            ]
        )

    catalog = PricingCatalog.from_mapping(
        version="v1",
        entries={
            "provider:model": {
                "input_per_million": "1",
                "output_per_million": "1",
            }
        },
    )
    repository = UsageRepository(session_factory)
    await repository.record(
        persisted_run.id,
        execution_ids[0],
        UsageRecord(provider="provider", model="model", input_tokens=1_000_000),
        catalog=catalog,
    )
    await repository.record(
        persisted_run.id,
        execution_ids[1],
        UsageRecord(provider="provider", model="model", cached_input_tokens=1),
        catalog=catalog,
    )

    totals = (await repository.totals(persisted_run.id))[0]
    assert totals["request_count"] == 2
    assert totals["known_estimated_cost_minor"] == 100
    assert totals["unknown_price_count"] == 1
    assert totals["estimate_complete"] is False


@pytest.mark.integration
async def test_usage_rejects_agent_execution_from_another_run(
    session_factory, persisted_run
) -> None:
    other_execution_id = uuid4()
    other_run = type(persisted_run)(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
    )
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(other_run)
        await work.commit()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=other_execution_id,
                run_id=other_run.id,
                role="planner",
                instruction_version="v1",
                provider="provider",
                model="model",
                status="SUCCEEDED",
            )
        )

    with pytest.raises(UsageRepositoryError):
        await UsageRepository(session_factory).record(
            persisted_run.id,
            other_execution_id,
            UsageRecord(provider="provider", model="model"),
            catalog=PricingCatalog.from_mapping(version="v1", entries={}),
            currency="USD",
        )

    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            session.add(
                ModelUsage(
                    run_id=persisted_run.id,
                    agent_execution_id=other_execution_id,
                    provider="provider",
                    model="model",
                    prompt_version="v1",
                    input_tokens=0,
                    output_tokens=0,
                    cached_input_tokens=0,
                    duration_ms=0,
                    tool_call_count=0,
                    pricing_version="v1",
                    estimated_cost_minor=None,
                    currency="USD",
                    unknown_price_reason="unknown_model_price",
                )
            )
            await session.flush()
