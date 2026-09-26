"""Operator totals use durable measurements without turning missing data into zero."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_usage import SubscriptionUsagePage
from forge.domain.run import RunSnapshot
from forge.domain.subscription import (
    AttemptIdentity,
    AttemptTelemetry,
    LogicalTaskContract,
    ReasoningEffort,
    RouteBinding,
    RouteMapping,
    SpecialistPurpose,
    TaskBudget,
    encode_subscription_record,
)
from forge.domain.subscription_budget import project_attempt_charge
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import (
    SubscriptionAttemptConsumption,
    SubscriptionAttemptReservation,
)
from forge.persistence.queries.subscription_usage import SubscriptionUsageQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import event, select
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _remove_disposable_subscription_rows,
    _route,
)


async def _attempts(session_factory, run, *, worker_fallbacks=()):
    async with PostgresUnitOfWork(session_factory) as work:
        task_id = await _admit_run(
            work,
            run,
            (_route("primary"), _route("worker")),
            worker_fallbacks=worker_fallbacks,
        )
        task = await work.subscription.get_task(run.id, task_id)
        ids = [uuid4() for _ in range(3)]
        for number, attempt_id in enumerate(ids, 1):
            await work.subscription.create_attempt(
                AttemptIdentity(
                    run_id=run.id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    attempt_number=number,
                ),
                route_payload=task.route,
                idempotency_key=str(attempt_id),
            )
        await work.commit()
    budget = TaskBudget(
        max_provider_attempts=1,
        max_repairs=0,
        max_input_tokens=100,
        max_output_tokens=100,
        max_cost_minor=100,
    )
    measured = (
        AttemptTelemetry(
            input_tokens=10,
            duration_ms=100,
            tool_call_count=1,
            estimated_api_cost_minor=7,
            cached_input_tokens=2,
        ),
        AttemptTelemetry(input_tokens=0, output_tokens=0, estimated_api_cost_minor=0),
    )
    async with session_factory() as session:
        for index, telemetry in enumerate(measured):
            attempt_id = ids[index]
            charge = project_attempt_charge(budget, telemetry, repair=False)
            session.add(
                SubscriptionAttemptReservation(
                    attempt_id=attempt_id,
                    run_id=run.id,
                    task_id=task_id,
                    idempotency_key=str(attempt_id),
                    budget_payload=encode_subscription_record(budget),
                )
            )
            await session.flush()
            session.add(
                SubscriptionAttemptConsumption(
                    attempt_id=attempt_id,
                    telemetry_payload=encode_subscription_record(telemetry),
                    observed=charge.observed.values(),
                    charged=charge.charged.values(),
                    unknown_fields=list(charge.unknown_fields),
                    exceeded_fields=[],
                    policy_violations=[],
                    uncertain=False,
                )
            )
            session.add(
                SubscriptionAttemptResult(
                    attempt_id=attempt_id,
                    result_digest=str(index) * 64,
                    result_payload={
                        "schema_version": 4,
                        "failure": "interrupted" if index == 0 else None,
                        "effective_failure": None,
                    },
                    disposition="retained",
                    accepted=False,
                )
            )
        await session.commit()
    return task_id, ids


@pytest.mark.integration
async def test_totals_read_immutable_measurements_and_keep_missing_units_unknown(
    session_factory,
    persisted_run,
):
    await _attempts(session_factory, persisted_run)
    first = SubscriptionUsagePage.model_validate(
        await SubscriptionUsageQuery(session_factory).usage(run_id=persisted_run.id)
    )
    assert len(first.items) == 1 and not first.has_more
    item = first.items[0]
    assert (item.attempts, item.recorded_results, item.failed_results, item.pending_results) == (
        3,
        2,
        1,
        1,
    )
    assert item.input_tokens.model_dump() == {
        "known_total": 10,
        "measured_attempts": 2,
        "unknown_attempts": 1,
    }
    assert item.output_tokens.model_dump() == {
        "known_total": 0,
        "measured_attempts": 1,
        "unknown_attempts": 2,
    }
    assert item.duration_ms.known_total == 100
    assert item.cached_input_tokens.model_dump() == {
        "known_total": 2,
        "measured_attempts": 1,
        "unknown_attempts": 2,
    }
    assert item.currency is None
    # Amounts without an identified currency cannot be added as a monetary total.
    assert item.estimated_api_cost_minor.model_dump() == {
        "known_total": None,
        "measured_attempts": 0,
        "unknown_attempts": 3,
    }
    assert await SubscriptionUsageQuery(session_factory).usage(run_id=uuid4()) is None
    assert (
        SubscriptionUsagePage.model_validate(
            await SubscriptionUsageQuery(session_factory).usage(run_id=persisted_run.id)
        )
        == first
    )


@pytest.mark.integration
@pytest.mark.parametrize("missing_consumption", [False, True])
async def test_final_consumption_is_authoritative_even_when_unknown(
    session_factory,
    persisted_run,
    missing_consumption,
):
    _, ids = await _attempts(session_factory, persisted_run)
    async with session_factory() as session:
        attempt = await session.get(SubscriptionAttempt, ids[0])
        attempt.telemetry_payload = encode_subscription_record(AttemptTelemetry(input_tokens=999))
        consumption = await session.get(SubscriptionAttemptConsumption, ids[0])
        if missing_consumption:
            consumption.telemetry_payload = None
        else:
            # The storage decoder permits field order to change. Page selection
            # must select by field name as well, including nested route fields.
            payload = deepcopy(consumption.telemetry_payload)
            payload["record"]["fields"].reverse()
            consumption.telemetry_payload = payload
            route = deepcopy(attempt.route_payload)
            route["record"]["fields"].reverse()
            for pair in route["record"]["fields"]:
                if pair[0] == "effective":
                    pair[1]["fields"].reverse()
            attempt.route_payload = route
        await session.commit()
    page = SubscriptionUsagePage.model_validate(
        await SubscriptionUsageQuery(session_factory).usage(run_id=persisted_run.id)
    )
    assert len(page.items) == 1
    assert page.items[0].input_tokens.model_dump() == {
        "known_total": 0 if missing_consumption else 10,
        "measured_attempts": 1 if missing_consumption else 2,
        "unknown_attempts": 2 if missing_consumption else 1,
    }


@pytest.mark.integration
async def test_complete_groups_use_each_attempt_route_and_currency_across_pages(
    session_factory,
    persisted_run,
):
    worker = _route("worker")
    fallback = _route("fallback")
    higher_effort = replace(worker, effort=ReasoningEffort.HIGH)
    primary_id, _ = await _attempts(
        session_factory,
        persisted_run,
        worker_fallbacks=(fallback, higher_effort),
    )

    def binding(route):
        return RouteBinding(
            requested=worker,
            effective=route,
            mapping_applied=RouteMapping(
                requested=worker,
                effective=route,
                approved_by="fixture-operator",
                approval_id="fixture-profile",
                reason="approved fixture fallback",
            )
            if route != worker
            else None,
        )

    worker_task_id = uuid4()
    contract = LogicalTaskContract(
        run_id=persisted_run.id,
        task_id=worker_task_id,
        parent_task_id=primary_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=RouteBinding(requested=worker, effective=worker),
        budget=TaskBudget(),
        owned_paths=("apps/worker",),
    )
    # Projections describe persisted attempts, including attempts whose task has
    # since selected a different route. They do not re-run admission.
    values = (
        (worker, "USD", 4),
        (worker, "USD", 5),
        (worker, "GBP", 3),
        (fallback, "USD", 6),
        (higher_effort, "USD", 8),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.create_task(contract, idempotency_key=str(worker_task_id))
        for number, (route, currency, count) in enumerate(values, 1):
            attempt_id = uuid4()
            await work.subscription.create_attempt(
                AttemptIdentity(
                    run_id=persisted_run.id,
                    task_id=worker_task_id,
                    attempt_id=attempt_id,
                    attempt_number=number,
                ),
                route_payload=binding(route),
                idempotency_key=str(attempt_id),
            )
            row = await work.session.get(SubscriptionAttempt, attempt_id)
            row.telemetry_payload = encode_subscription_record(
                AttemptTelemetry(
                    input_tokens=count,
                    estimated_api_cost_minor=count,
                    currency=currency,
                )
            )
        await work.commit()
    async with session_factory() as session:
        row = await session.get(SubscriptionTask, worker_task_id)
        row.payload = encode_subscription_record(
            replace(
                contract,
                route=binding(fallback),
            )
        )
        await session.commit()

    queries = []
    engine = session_factory.kw["bind"].sync_engine

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        full = SubscriptionUsagePage.model_validate(
            await SubscriptionUsageQuery(session_factory).usage(run_id=persisted_run.id)
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    # One scope lookup and one streamed grouping query; no per-attempt reads.
    assert len(queries) == 2
    assert len(full.items) == 5 and not full.has_more
    by_route = {
        (i.effective_route.provider, i.effective_route.effort, i.currency): i for i in full.items
    }
    assert by_route[("worker", "low", "USD")].attempts == 2
    assert by_route[("worker", "low", "USD")].input_tokens.known_total == 9
    assert by_route[("worker", "low", "GBP")].estimated_api_cost_minor.known_total == 3
    assert by_route[("fallback", "low", "USD")].input_tokens.known_total == 6
    assert by_route[("worker", "high", "USD")].input_tokens.known_total == 8
    paged = []
    for offset in range(6):
        page = SubscriptionUsagePage.model_validate(
            await SubscriptionUsageQuery(session_factory).usage(
                run_id=persisted_run.id, offset=offset, limit=1
            )
        )
        assert page.has_more is (offset < 4)
        paged.extend(page.items)
    assert paged == full.items


@pytest.mark.integration
async def test_run_filter_and_fresh_reads_do_not_rewrite_measurements(
    session_factory, persisted_run
):
    query = SubscriptionUsageQuery(session_factory)
    assert await query.usage(run_id=persisted_run.id) == {"items": [], "has_more": False}
    await _attempts(session_factory, persisted_run)
    second = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=persisted_run.policy_version,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second)
        await work.commit()
    await _attempts(session_factory, second)
    async with session_factory() as session:
        before = [
            (row.attempt_id, deepcopy(row.telemetry_payload), deepcopy(row.charged))
            for row in (
                await session.scalars(
                    select(SubscriptionAttemptConsumption).order_by(
                        SubscriptionAttemptConsumption.attempt_id
                    )
                )
            ).all()
        ]
    page = SubscriptionUsagePage.model_validate(await query.usage())
    assert len(page.items) == 2
    assert {item.run_id for item in page.items} == {persisted_run.id, second.id}
    assert all(item.attempts == 3 for item in page.items)
    assert len((await query.usage(run_id=second.id))["items"]) == 1
    async with session_factory() as session:
        after = [
            (row.attempt_id, row.telemetry_payload, row.charged)
            for row in (
                await session.scalars(
                    select(SubscriptionAttemptConsumption).order_by(
                        SubscriptionAttemptConsumption.attempt_id
                    )
                )
            ).all()
        ]
    assert after == before


@pytest.mark.integration
@pytest.mark.parametrize("corruption", ["route", "telemetry", "identity", "result"])
async def test_malformed_stored_usage_fails_closed_without_exposing_payload(
    session_factory,
    persisted_run,
    corruption,
):
    task_id, ids = await _attempts(session_factory, persisted_run)
    async with session_factory() as session:
        if corruption == "route":
            row = await session.get(SubscriptionAttempt, ids[0])
            row.route_payload = {"invalid": "private-payload"}
        elif corruption == "telemetry":
            row = await session.get(SubscriptionAttemptConsumption, ids[0])
            row.telemetry_payload = {"invalid": "private-payload"}
        elif corruption == "identity":
            row = await session.get(SubscriptionTask, task_id)
            payload = deepcopy(row.payload)
            for pair in payload["record"]["fields"]:
                if pair[0] == "task_id":
                    pair[1] = {"$uuid": str(uuid4())}
            row.payload = payload
        else:
            row = await session.get(SubscriptionAttemptResult, ids[0])
            row.result_payload = {"schema_version": True, "failure": "private-payload"}
        await session.commit()
    with pytest.raises(ValueError, match="^invalid stored subscription (usage|result metadata)$"):
        await SubscriptionUsageQuery(session_factory).usage(run_id=persisted_run.id)
