"""Real PostgreSQL binding, replay, final replacement and snapshot tests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
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
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.task_usage_observations import (
    PostgresTaskUsageObservations,
    UsageBindingConflict,
)


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
