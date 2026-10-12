"""Real PostgreSQL binding, replay, final replacement and snapshot tests."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.api.schemas.task_usage import UsageReportWire
from forge.application.services.task_usage_reports import build_usage_report
from forge.domain.task_usage_contract import (
    CounterKind,
    Observation,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    TokenCounts,
    WorkUnitBinding,
)
from forge.persistence.models import AgentExecution, ModelUsage
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.queries import task_usage_totals as totals_module
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.task_usage_observations import (
    PostgresTaskUsageObservations,
    UsageBindingConflict,
)
from sqlalchemy import event


async def _binding(session_factory, run, *, source_id=None) -> WorkUnitBinding:
    source_id = source_id or uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=source_id,
                run_id=run.id,
                role="developer",
                instruction_version="v1",
                provider="test",
                model="test",
                status="RUNNING",
            )
        )
    route = RouteCohort.from_members(
        (RouteMember(role="developer", provider="test", model="test", ordinal=0),)
    )
    return WorkUnitBinding(
        subject=SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id),
        work_unit_id=uuid4(),
        admitted_phase_id="implement",
        configured_route=route,
        effective_route=route,
        source=SourceBinding(kind="agent_execution", source_id=source_id),
    )


@pytest.mark.asyncio
async def test_run_report_uses_durable_task_ancestry_without_changing_totals(
    session_factory, persisted_run,
) -> None:
    source_rows = []
    async with session_factory() as session, session.begin():
        for index, (parent, input_tokens, output_tokens) in enumerate(
            ((None, 1, 1), (0, 60, 40), (None, 1, 1), (2, 60, 40))
        ):
            task_id, attempt_id = uuid4(), uuid4()
            parent_id = source_rows[parent][0] if parent is not None else None
            session.add(SubscriptionTask(
                id=task_id, run_id=persisted_run.id, task_id=task_id,
                parent_task_id=parent_id, idempotency_key=f"ancestry-task-{index}", payload={},
            ))
            await session.flush()
            session.add(SubscriptionAttempt(
                id=attempt_id, run_id=persisted_run.id, task_row_id=task_id,
                attempt_number=1, idempotency_key=f"ancestry-attempt-{index}",
                route_payload={},
            ))
            await session.flush()
            source_rows.append((task_id, attempt_id, input_tokens, output_tokens))
    route = RouteCohort.from_members(
        (RouteMember(role="primary", provider="test", model="test", ordinal=0),)
    )
    writer = PostgresTaskUsageObservations(session_factory)
    for index, (task_id, attempt_id, input_tokens, output_tokens) in enumerate(source_rows):
        binding = WorkUnitBinding(
            subject=SubjectKey(project_id=persisted_run.project_id, kind="run", subject_id=persisted_run.id),
            work_unit_id=uuid4(), admitted_phase_id=f"phase-{index}",
            configured_route=route, effective_route=route,
            source=SourceBinding(kind="subscription_attempt", source_id=attempt_id, parent_id=task_id),
        )
        await writer.bind(binding)
        assert await writer.append_observation(Observation(
            binding=binding, event_id=f"ancestry-{index}", sequence=1,
            counter_kind=CounterKind.CUMULATIVE,
            tokens=TokenCounts(input_tokens=input_tokens, output_tokens=output_tokens),
        ))
    async with session_factory() as session:
        totals = await TaskUsageTotalsQuery(session_factory).totals_in(session, binding.subject)
        report = await build_usage_report(
            session, totals=totals, policy_revision=None, baseline=None, checkpoint=None,
        )
    assert report is not None
    assert (report.input_tokens, report.output_tokens) == (122, 82)
    assert [source.binding.work_unit_id for source in report.sources] == [
        fact.binding.work_unit_id for fact in totals.source_facts
    ]
    assert {source.binding.source.source_id for source in report.sources} == {
        row[1] for row in source_rows
    }
    by_id = {source.binding.source.source_id: source for source in report.sources}
    assert [by_id[row[1]].delegated for row in source_rows] == [False, True, False, True]
    wire = UsageReportWire.dump_python(report, mode="json")
    assert [source["delegated"] for source in wire["sources"]] == [
        source.delegated for source in report.sources
    ]


@pytest.mark.asyncio
async def test_binding_replay_and_source_project_authority(session_factory, persisted_run) -> None:
    binding = await _binding(session_factory, persisted_run)
    writer = PostgresTaskUsageObservations(session_factory)
    await writer.bind(binding)
    await writer.bind(binding)
    assert (
        await writer.binding_for_source(
            project_id=binding.subject.project_id,
            source_kind="agent_execution",
            source_id=binding.source.source_id,
        )
        == binding
    )
    with pytest.raises(UsageBindingConflict):
        await writer.bind(replace(binding, admitted_phase_id="review", work_unit_id=uuid4()))
    with pytest.raises(UsageBindingConflict, match="lineage"):
        await writer.bind(
            replace(
                binding,
                work_unit_id=uuid4(),
                subject=replace(binding.subject, project_id=uuid4()),
            )
        )
    with pytest.raises(UsageBindingConflict):
        await writer.append_observation(
            Observation(
                binding=replace(binding, admitted_phase_id="review"),
                event_id="spoof",
                sequence=1,
                counter_kind=CounterKind.CUMULATIVE,
                tokens=TokenCounts(input_tokens=1),
            )
        )


@pytest.mark.asyncio
async def test_reorder_replay_epoch_and_final_replacement(session_factory, persisted_run) -> None:
    binding = await _binding(session_factory, persisted_run)
    writer = PostgresTaskUsageObservations(session_factory)
    query = TaskUsageTotalsQuery(session_factory)
    await writer.bind(binding)
    second = Observation(
        binding=binding,
        event_id="second",
        sequence=2,
        counter_kind=CounterKind.CUMULATIVE,
        counter_epoch="turn-1",
        tokens=TokenCounts(input_tokens=12, output_tokens=3, cached_input_tokens=2),
    )
    first = replace(
        second,
        event_id="first",
        sequence=1,
        tokens=TokenCounts(input_tokens=10, output_tokens=2, cached_input_tokens=1),
    )
    assert await writer.append_observation(second)
    assert await writer.append_observation(first)
    assert not await writer.append_observation(second)
    with pytest.raises(UsageBindingConflict):
        await writer.append_observation(replace(second, tokens=TokenCounts(input_tokens=13)))
    third = replace(
        second,
        event_id="turn-2",
        sequence=3,
        counter_epoch="turn-2",
        tokens=TokenCounts(input_tokens=5, output_tokens=1),
    )
    await writer.append_observation(third)
    before = await query.totals(binding.subject)
    assert (before.input_tokens, before.output_tokens, before.cached_input_tokens) == (17, 4, 2)
    assert before.completeness == "complete"
    with pytest.raises(ValueError, match="decrement"):
        await writer.append_observation(
            replace(
                first,
                event_id="decrement",
                sequence=5,
                tokens=TokenCounts(input_tokens=9, output_tokens=2),
            )
        )
    final = replace(
        third,
        event_id="final",
        sequence=4,
        tokens=TokenCounts(input_tokens=15, output_tokens=3),
        final=True,
        final_status="cancelled",
        reconciles_event_id="second",
    )
    await writer.append_observation(final)
    after = await query.totals(binding.subject)
    assert (after.input_tokens, after.output_tokens) == (15, 3)
    assert after.final_source_count == 0
    assert await query.finalized_sources(binding.subject) == ()
    assert after.snapshot_digest != before.snapshot_digest
    assert "final_below_live" in after.source_facts[0].disagreements


@pytest.mark.asyncio
async def test_authoritative_model_usage_replaces_late_live(session_factory, persisted_run) -> None:
    binding = await _binding(session_factory, persisted_run)
    writer = PostgresTaskUsageObservations(session_factory)
    query = TaskUsageTotalsQuery(session_factory)
    await writer.bind(binding)
    await writer.append_observation(
        Observation(
            binding=binding,
            event_id="live",
            sequence=1,
            counter_kind=CounterKind.CUMULATIVE,
            tokens=TokenCounts(input_tokens=30, output_tokens=5),
        )
    )
    before_model_usage = datetime.now(UTC) - timedelta(microseconds=1)
    async with session_factory() as session, session.begin():
        session.add(
            ModelUsage(
                id=uuid4(),
                run_id=persisted_run.id,
                agent_execution_id=binding.source.source_id,
                provider="test",
                model="test",
                prompt_version="v1",
                input_tokens=20,
                output_tokens=4,
                cached_input_tokens=30,
                input_tokens_complete=True,
                output_tokens_complete=True,
                cached_input_tokens_complete=True,
                duration_ms=1,
                tool_call_count=0,
                pricing_version="v1",
                currency="USD",
                estimated_cost_minor=0,
            )
        )
    totals = await query.totals(binding.subject)
    assert (totals.input_tokens, totals.output_tokens, totals.cached_input_tokens) == (50, 4, 30)
    assert totals.final_source_count == 1
    assert len(await query.finalized_sources(binding.subject)) == 1
    assert totals.source_facts[0].evidence_kind == "model_usage"
    assert "final_below_live" in totals.source_facts[0].disagreements
    historical = await query.totals(binding.subject, as_of=before_model_usage)
    assert historical.final_source_count == 0
    assert historical.source_facts[0].evidence_kind == "live"


@pytest.mark.asyncio
async def test_model_usage_missing_metadata_does_not_project_as_measured_zero(session_factory, persisted_run) -> None:
    binding = await _binding(session_factory, persisted_run)
    writer = PostgresTaskUsageObservations(session_factory)
    await writer.bind(binding)
    async with session_factory() as session, session.begin():
        session.add(ModelUsage(
            id=uuid4(), run_id=persisted_run.id,
            agent_execution_id=binding.source.source_id,
            provider="test", model="test", prompt_version="v1",
            input_tokens=0, output_tokens=0, cached_input_tokens=0,
            input_tokens_complete=False, output_tokens_complete=True,
            cached_input_tokens_complete=False,
            duration_ms=1, tool_call_count=0, pricing_version="v1",
            currency="USD", estimated_cost_minor=0,
        ))
    totals = await TaskUsageTotalsQuery(session_factory).totals(binding.subject)
    assert totals.input_tokens is None
    assert totals.output_tokens == 0
    assert totals.cached_input_tokens is None
    assert totals.completeness == "lower_bound"


@pytest.mark.asyncio
async def test_model_usage_retains_full_prompt_when_cache_subset_is_unknown(session_factory, persisted_run) -> None:
    binding = await _binding(session_factory, persisted_run)
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    async with session_factory() as session, session.begin():
        session.add(ModelUsage(
            id=uuid4(), run_id=persisted_run.id,
            agent_execution_id=binding.source.source_id,
            provider="test", model="test", prompt_version="v1",
            input_tokens=9, output_tokens=0, cached_input_tokens=0,
            input_tokens_complete=True, output_tokens_complete=True,
            cached_input_tokens_complete=False,
            duration_ms=1, tool_call_count=0, pricing_version="v1",
            currency="USD", estimated_cost_minor=0,
        ))
    totals = await TaskUsageTotalsQuery(session_factory).totals(binding.subject)
    assert totals.input_tokens == 9
    assert totals.output_tokens == 0
    assert totals.cached_input_tokens is None
    assert totals.completeness == "complete"


@pytest.mark.asyncio
async def test_model_usage_explicit_zero_is_complete(session_factory, persisted_run) -> None:
    binding = await _binding(session_factory, persisted_run)
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    async with session_factory() as session, session.begin():
        session.add(ModelUsage(
            id=uuid4(), run_id=persisted_run.id,
            agent_execution_id=binding.source.source_id,
            provider="test", model="test", prompt_version="v1",
            input_tokens=0, output_tokens=0, cached_input_tokens=0,
            input_tokens_complete=True, output_tokens_complete=True,
            cached_input_tokens_complete=True,
            duration_ms=1, tool_call_count=0, pricing_version="v1",
            currency="USD", estimated_cost_minor=0,
        ))
    totals = await TaskUsageTotalsQuery(session_factory).totals(binding.subject)
    assert (totals.input_tokens, totals.output_tokens, totals.cached_input_tokens) == (0, 0, 0)
    assert totals.completeness == "complete"


@pytest.mark.asyncio
async def test_mixed_model_rows_retain_positive_partial_as_lower_bound(session_factory, persisted_run) -> None:
    binding = await _binding(session_factory, persisted_run)
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    async with session_factory() as session, session.begin():
        for tokens, complete in ((11, True), (7, False)):
            session.add(ModelUsage(
                id=uuid4(), run_id=persisted_run.id,
                agent_execution_id=binding.source.source_id,
                provider="test", model="test", prompt_version="v1",
                input_tokens=tokens, output_tokens=0, cached_input_tokens=0,
                input_tokens_complete=complete, output_tokens_complete=True,
                cached_input_tokens_complete=True,
                duration_ms=1, tool_call_count=0, pricing_version="v1",
                currency="USD", estimated_cost_minor=0,
            ))
    totals = await TaskUsageTotalsQuery(session_factory).totals(binding.subject)
    assert totals.input_tokens == 18
    assert totals.output_tokens == 0
    assert totals.completeness == "lower_bound"


@pytest.mark.asyncio
async def test_totals_in_reuses_callers_repeatable_read_snapshot(
    session_factory, persisted_run
) -> None:
    binding = await _binding(session_factory, persisted_run)
    writer = PostgresTaskUsageObservations(session_factory)
    query = TaskUsageTotalsQuery(session_factory)
    await writer.bind(binding)
    first = Observation(
        binding=binding,
        event_id="first",
        sequence=1,
        counter_kind=CounterKind.CUMULATIVE,
        tokens=TokenCounts(input_tokens=5, output_tokens=1),
    )
    await writer.append_observation(first)
    async with session_factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        before = await query.totals_in(session, binding.subject)
        await writer.append_observation(
            replace(
                first,
                event_id="second",
                sequence=2,
                tokens=TokenCounts(input_tokens=7, output_tokens=2),
            )
        )
        retained = await query.totals_in(session, binding.subject)
    fresh = await query.totals(binding.subject)
    assert (before.input_tokens, retained.input_tokens, fresh.input_tokens) == (5, 5, 7)
    assert before.snapshot_digest == retained.snapshot_digest
    assert before.snapshot_digest != fresh.snapshot_digest


@pytest.mark.asyncio
async def test_parallel_sources_and_duplicate_writers_aggregate_once(
    session_factory, persisted_run
) -> None:
    first = await _binding(session_factory, persisted_run)
    second = await _binding(session_factory, persisted_run)
    writer = PostgresTaskUsageObservations(session_factory)
    query = TaskUsageTotalsQuery(session_factory)
    await asyncio.gather(writer.bind(first), writer.bind(second))
    one = Observation(
        binding=first,
        event_id="one",
        sequence=1,
        counter_kind=CounterKind.DELTA,
        tokens=TokenCounts(input_tokens=10, output_tokens=2),
    )
    two = replace(
        one,
        binding=second,
        event_id="two",
        tokens=TokenCounts(input_tokens=None, output_tokens=3),
        unknown_dimensions=("input_tokens",),
    )
    first_results = await asyncio.gather(
        writer.append_observation(one), writer.append_observation(one)
    )
    assert sorted(first_results) == [False, True]
    await writer.append_observation(two)
    totals = await query.totals(first.subject)
    assert (totals.input_tokens, totals.output_tokens) == (10, 5)
    assert totals.source_count == 2
    assert totals.completeness == "lower_bound"
    assert totals.final_source_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("second, expected", [(0, 2**63 - 1), (1, None)])
@pytest.mark.parametrize("channel", ["input", "output", "both"])
async def test_valid_source_channels_can_exceed_aggregate_signed64_without_losing_facts(
    session_factory, persisted_run, second, expected, channel,
) -> None:
    bindings = [await _binding(session_factory, persisted_run) for _ in range(2)]
    writer = PostgresTaskUsageObservations(session_factory)
    for index, (binding, amount) in enumerate(zip(bindings, (2**63 - 1, second), strict=True)):
        await writer.bind(binding)
        await writer.append_observation(Observation(
            binding=binding, event_id=f"large-{index}", sequence=1,
            counter_kind=CounterKind.CUMULATIVE,
            tokens=TokenCounts(input_tokens=amount if channel != "output" else 0,
                               output_tokens=amount if channel != "input" else 0,
                               cached_input_tokens=amount if channel != "output" else 0,
                               reasoning_output_tokens=amount if channel != "input" else 0),
            final=True, final_status="settled",
        ))
    totals = await TaskUsageTotalsQuery(session_factory).totals(bindings[0].subject)
    if channel == "input":
        assert (totals.input_tokens, totals.cached_input_tokens) == (expected, expected)
        assert (totals.output_tokens, totals.reasoning_output_tokens) == (0, 0)
    elif channel == "output":
        assert (totals.input_tokens, totals.cached_input_tokens) == (0, 0)
        assert (totals.output_tokens, totals.reasoning_output_tokens) == (expected, expected)
    else:
        assert (totals.input_tokens, totals.output_tokens) == (expected, expected)
        assert (totals.cached_input_tokens, totals.reasoning_output_tokens) == (
            expected, expected,
        )
    assert totals.completeness == (
        "complete" if expected is not None else "unknown" if channel == "both" else "lower_bound"
    )
    source_channel = "output" if channel == "output" else "input"
    assert sorted(getattr(fact, f"{source_channel}_tokens") for fact in totals.source_facts) == [
        second, 2**63 - 1,
    ]
    assert all(fact.completeness == "complete" for fact in totals.source_facts)


@pytest.mark.parametrize("stored", [-1, 2**63, 1.5, True])
def test_unsupported_aggregate_does_not_relax_malformed_stored_value_rejection(stored):
    with pytest.raises(ValueError, match="stored measured usage is malformed"):
        totals_module._measured(stored)


@pytest.mark.asyncio
async def test_totals_batch_source_reads_with_stable_order_and_digest(
    session_factory, persisted_run, monkeypatch
) -> None:
    bindings = [await _binding(session_factory, persisted_run) for _ in range(3)]
    writer = PostgresTaskUsageObservations(session_factory)
    model_ids: dict[object, list[object]] = {}
    for binding in bindings:
        await writer.bind(binding)
    for index, binding in enumerate(bindings):
        await writer.append_observation(Observation(
            binding=binding, event_id=f"live-{index}", sequence=1,
            counter_kind=CounterKind.CUMULATIVE,
            tokens=TokenCounts(input_tokens=100 + index, output_tokens=2),
        ))
        async with session_factory() as session, session.begin():
            record_id = uuid4()
            session.add(ModelUsage(
                id=record_id, run_id=persisted_run.id,
                agent_execution_id=binding.source.source_id,
                provider="test", model="test", prompt_version="v1",
                input_tokens=10 + index, output_tokens=3,
                    cached_input_tokens=20 - index, duration_ms=1,
                    input_tokens_complete=True, output_tokens_complete=True,
                    cached_input_tokens_complete=True,
                tool_call_count=0, pricing_version="v1", currency="USD",
                estimated_cost_minor=0,
            ))
            model_ids.setdefault(binding.source.source_id, []).append(record_id)
            if index == 0:
                extra_id = uuid4()
                session.add(ModelUsage(
                    id=extra_id, run_id=persisted_run.id,
                    agent_execution_id=binding.source.source_id,
                    provider="test", model="test", prompt_version="v1",
                        input_tokens=5, output_tokens=2, cached_input_tokens=0,
                        input_tokens_complete=True, output_tokens_complete=True,
                        cached_input_tokens_complete=True,
                    duration_ms=1, tool_call_count=0, pricing_version="v1",
                    currency="USD", estimated_cost_minor=0,
                ))
                model_ids[binding.source.source_id].append(extra_id)
    engine = session_factory.kw["bind"].sync_engine
    statements: list[str] = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "task_usage_observations" in lowered or "model_usage" in lowered:
            statements.append(lowered)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        totals = await TaskUsageTotalsQuery(session_factory).totals(bindings[0].subject)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len([sql for sql in statements if "task_usage_observations" in sql]) == 1
    assert len([sql for sql in statements if "model_usage" in sql]) == 1
    assert (totals.source_count, totals.final_source_count) == (3, 3)
    assert (totals.input_tokens, totals.output_tokens, totals.cached_input_tokens) == (95, 11, 57)
    fingerprint = []
    for binding in sorted(bindings, key=lambda item: str(item.work_unit_id)):
        index = bindings.index(binding)
        fingerprint.append((str(binding.work_unit_id), [(f"live-{index}", 1)]))
        fingerprint.append(("model_usage", sorted(str(value) for value in model_ids[binding.source.source_id])))
    expected = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert totals.snapshot_digest == expected
    monkeypatch.setattr(totals_module, "_BATCH_SIZE", 2)
    statements.clear()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        split = await TaskUsageTotalsQuery(session_factory).totals(bindings[0].subject)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert split == totals
    assert len([sql for sql in statements if "task_usage_observations" in sql]) == 2
    assert len([sql for sql in statements if "model_usage" in sql]) == 2


@pytest.mark.asyncio
async def test_final_can_arrive_before_reconciled_live_event(
    session_factory, persisted_run
) -> None:
    binding = await _binding(session_factory, persisted_run)
    writer = PostgresTaskUsageObservations(session_factory)
    query = TaskUsageTotalsQuery(session_factory)
    await writer.bind(binding)
    final = Observation(
        binding=binding,
        event_id="final",
        sequence=2,
        counter_kind=CounterKind.CUMULATIVE,
        tokens=TokenCounts(input_tokens=8, output_tokens=1),
        final=True,
        final_status="succeeded",
        reconciles_event_id="early",
    )
    await writer.append_observation(final)
    await writer.append_observation(
        replace(
            final,
            event_id="early",
            sequence=1,
            final=False,
            final_status=None,
            reconciles_event_id=None,
            tokens=TokenCounts(input_tokens=10, output_tokens=2),
        )
    )
    totals = await query.totals(binding.subject)
    assert (totals.input_tokens, totals.output_tokens) == (8, 1)
    assert totals.final_source_count == 0
    assert await query.finalized_sources(binding.subject) == ()
    assert "final_below_live" in totals.source_facts[0].disagreements
