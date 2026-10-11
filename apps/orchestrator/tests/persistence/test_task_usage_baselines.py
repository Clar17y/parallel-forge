import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.agents.adk_gateway import GoogleAdkGateway
from forge.application.services.task_usage_baselines import (
    TaskUsageBaselineService,
    UsageReportFilters,
)
from forge.domain.actor import AgentRole
from forge.domain.run import RunSnapshot
from forge.domain.subscription import LogicalTaskContract, SpecialistPurpose
from forge.domain.task_usage_baselines import BaselineSample, SamplingRules, select_cohort
from forge.domain.task_usage_contract import (
    MonitoringPolicy,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    WorkUnitBinding,
)
from forge.persistence.models.epic_brainstorm import BrainstormConversation, BrainstormJobRow
from forge.persistence.models.epic_brief import Epic
from forge.persistence.models.execution import AgentExecution, EvidenceSet, ModelUsage
from forge.persistence.models.project import Project
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.task_usage import TaskUsagePolicyRevision, TaskUsageWorkUnit
from forge.persistence.queries import task_usage_baselines as baseline_module
from forge.persistence.queries.task_usage_baselines import TaskUsageBaselineQuery
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.task_usage_observations import (
    PostgresTaskUsageObservations,
    _members,
)
from forge.persistence.repositories.task_usage_policy import (
    PostgresTaskUsagePolicyRepository,
    policy_from_row,
)
from forge.persistence.repositories.usage import UsageRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import event, func, select, update

_AGENTS_TEST_DIR = str(Path(__file__).resolve().parents[1] / "agents")
if _AGENTS_TEST_DIR not in sys.path:
    sys.path.insert(0, _AGENTS_TEST_DIR)

from test_adk_gateway import (
    _FakeAdkRuntime,
    _FakeAdkToolProvider,
    _make_invocation_result,
    _make_loader,
    _make_request,
    _pricing_catalog,
    build_developer_output,
)


def _binding(run, execution_id, effort=None):
    configured = RouteCohort.from_members(
        (RouteMember(role="developer", provider="provider-a", model="model-a", ordinal=0, reasoning_effort=effort),)
    )
    effective = RouteCohort.from_members(
        (RouteMember(role="developer", provider="provider-a", model="model-a", ordinal=0, reasoning_effort=effort),)
    )
    return WorkUnitBinding(
        subject=SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id),
        work_unit_id=uuid4(),
        admitted_phase_id="implementation",
        configured_route=configured,
        effective_route=effective,
        source=SourceBinding(kind="agent_execution", source_id=execution_id),
    )


@pytest.mark.integration
async def test_finalized_run_sample_uses_measured_provider_rows_and_role_phase(
    session_factory, persisted_run
):
    execution_ids = (uuid4(), uuid4())
    async with session_factory() as session, session.begin():
        for index, execution_id in enumerate(execution_ids):
            session.add(
                AgentExecution(
                    id=execution_id,
                    run_id=persisted_run.id,
                    role="developer",
                    instruction_version="v1",
                    provider="provider-a",
                    model="model-a",
                    status="SUCCEEDED",
                )
            )
            session.add(
                ModelUsage(
                    id=uuid4(),
                    run_id=persisted_run.id,
                    agent_execution_id=execution_id,
                    provider="provider-a",
                    model="model-a",
                    prompt_version="v1",
                    input_tokens=100 * (index + 1),
                    output_tokens=50 * (index + 1),
                    cached_input_tokens=20,
                    duration_ms=1,
                    tool_call_count=0,
                    pricing_version="test-v1",
                    estimated_cost_minor=0,
                    currency="USD",
                    unknown_price_reason=None,
                )
            )
        await session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED")
        )
        session.add(
            TaskUsagePolicyRevision(
                project_id=persisted_run.project_id,
                revision=1,
                mode="report_only",
                warning_multiplier=1.5,
                checkpoint_multiplier=2.0,
                history_limit=50,
                minimum_comparables=20,
                manual_estimate_tokens=None,
                allow_cross_project_fallback=True,
                actor_id=uuid4(),
            )
        )
    observations = PostgresTaskUsageObservations(session_factory)
    await observations.bind(_binding(persisted_run, execution_ids[0]))
    await observations.bind(_binding(persisted_run, execution_ids[1], effort="medium"))
    query = TaskUsageBaselineQuery(session_factory)
    samples = await query.samples(
        project_ids={persisted_run.project_id},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    assert len(samples) == 1
    assert samples[0].outcome == "completed"
    assert samples[0].finalized and samples[0].measured
    assert samples[0].total_tokens == 490
    assert samples[0].source_count == 2
    assert samples[0].route_members == (
        ("developer", "provider-a", "model-a", None),
        ("developer", "provider-a", "model-a", "medium"),
    )
    assert sorted(samples[0].phase_role_breakdown) == [
        ("implementation", "developer", 170),
        ("implementation", "developer", 320),
    ]


@pytest.mark.integration
async def test_history_sql_bounds_candidates_before_loading_work_units(
    session_factory, persisted_run, monkeypatch
):
    other = RunSnapshot(id=uuid4(), project_id=persisted_run.project_id,
                        task_id=persisted_run.task_id,
                        policy_version=persisted_run.policy_version)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(other)
        await work.commit()
    engine = session_factory.kw["bind"].sync_engine
    statements: list[str] = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", capture)
    try:
        now = datetime.now(UTC) + timedelta(seconds=1)
        full = await TaskUsageBaselineQuery(session_factory).samples(
            project_ids={persisted_run.project_id}, as_of=now, window_days=1,
        )
        with_candidates = tuple(statements)
        monkeypatch.setattr(baseline_module, "_BATCH_SIZE", 1)
        statements.clear()
        split = await TaskUsageBaselineQuery(session_factory).samples(
            project_ids={persisted_run.project_id}, as_of=now, window_days=1,
        )
        assert split == full
        assert len([sql for sql in statements if " from task_usage_work_units "
                    in sql.replace("\n", " ")]) == 2
        statements.clear()
        await TaskUsageBaselineQuery(session_factory).samples(
            project_ids={persisted_run.project_id},
            as_of=datetime(2000, 1, 1, tzinfo=UTC), window_days=1,
        )
        empty_candidates = tuple(statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    run_select = next(sql for sql in with_candidates if " from runs " in sql.replace("\n", " "))
    assert run_select.count("runs.updated_at") >= 3  # selected, upper and lower bounds
    unit_select = next(sql for sql in with_candidates if " from task_usage_work_units " in sql.replace("\n", " "))
    assert "task_usage_work_units.subject_kind" in unit_select
    assert "task_usage_work_units.subject_id" in unit_select
    assert not any(" from task_usage_work_units " in sql.replace("\n", " ") for sql in empty_candidates)


@pytest.mark.integration
async def test_many_candidates_batch_sample_and_exclusion_evidence_queries(
    session_factory, persisted_run, monkeypatch
):
    runs = []
    for _ in range(6):
        run = RunSnapshot(id=uuid4(), project_id=persisted_run.project_id,
                          task_id=persisted_run.task_id,
                          policy_version=persisted_run.policy_version)
        async with PostgresUnitOfWork(session_factory) as work:
            await work.runs.create(run)
            await work.commit()
        runs.append(run)
    execution_ids = {}
    async with session_factory() as session, session.begin():
        legacy_id = uuid4()
        session.add(AgentExecution(id=legacy_id, run_id=persisted_run.id, role="developer",
                                   instruction_version="v1", provider="provider-a",
                                   model="model-a", status="SUCCEEDED"))
        session.add(ModelUsage(id=uuid4(), run_id=persisted_run.id,
                               agent_execution_id=legacy_id, provider="provider-a",
                               model="model-a", prompt_version="v1", input_tokens=4,
                               output_tokens=1, cached_input_tokens=2, duration_ms=1,
                               tool_call_count=0, pricing_version="v1", currency="USD",
                               estimated_cost_minor=0))
        await session.execute(update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED"))
        for index, run in enumerate(runs):
            execution_id = uuid4()
            execution_ids[run.id] = execution_id
            session.add(AgentExecution(id=execution_id, run_id=run.id, role="developer",
                                       instruction_version="v1", provider="provider-a",
                                       model="model-a", status="FAILED" if index == 0 else "SUCCEEDED"))
            session.add(ModelUsage(id=uuid4(), run_id=run.id,
                                   agent_execution_id=execution_id, provider="provider-a",
                                   model="model-a", prompt_version="v1", input_tokens=10 + index,
                                   output_tokens=2, cached_input_tokens=3, duration_ms=1,
                                   tool_call_count=0, pricing_version="v1", currency="USD",
                                   estimated_cost_minor=0))
            await session.execute(update(Run).where(Run.id == run.id).values(state="COMPLETED"))
    observations = PostgresTaskUsageObservations(session_factory)
    for run in runs:
        await observations.bind(_binding(run, execution_ids[run.id]))
    query = TaskUsageBaselineQuery(session_factory)
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    subjects = {(run.project_id, "run", run.id) for run in runs}
    engine = session_factory.kw["bind"].sync_engine
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        samples = await query.samples(project_ids={persisted_run.project_id},
                                      as_of=as_of, window_days=365)
        sample_queries = len(statements)
        statements.clear()
        exclusions = await query.exclusions(project_ids={persisted_run.project_id},
                                            as_of=as_of, window_days=365, subjects=subjects)
        exclusion_queries = len(statements)
        monkeypatch.setattr(baseline_module, "_BATCH_SIZE", 2)
        monkeypatch.setattr(__import__("forge.persistence.queries.task_usage_totals", fromlist=["_BATCH_SIZE"]),
                            "_BATCH_SIZE", 2)
        split = await query.samples(project_ids={persisted_run.project_id},
                                    as_of=as_of, window_days=365)
        split_exclusions = await query.exclusions(project_ids={persisted_run.project_id},
                                                  as_of=as_of, window_days=365, subjects=subjects)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert sample_queries <= 20
    assert exclusion_queries <= 20
    assert split == samples and split_exclusions == exclusions
    assert {row.subject_id: row.total_tokens for row in samples if row.subject_id in {r.id for r in runs}} == {
        run.id: 15 + index for index, run in enumerate(runs)
    }
    assert next(row for row in samples if row.subject_id == runs[0].id).failed_attempt_count == 1
    legacy = next(row for row in samples if row.subject_id == persisted_run.id)
    assert legacy.total_tokens == 7 and legacy.finalized and legacy.source_count == 1
    async with session_factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        units = (await session.scalars(select(TaskUsageWorkUnit).where(
            TaskUsageWorkUnit.subject_id.in_([run.id for run in runs])
        ))).all()
        keyed = {
            SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id):
            [unit for unit in units if unit.subject_id == run.id]
            for run in runs
        }
        batched = await TaskUsageTotalsQuery(session_factory).totals_for_units_in(
            session, keyed, as_of=as_of,
        )
        for subject in keyed:
            assert batched[subject] == await TaskUsageTotalsQuery(session_factory).totals_in(
                session, subject, as_of=as_of,
            )
    assert exclusions == {}


@pytest.mark.integration
async def test_exact_window_boundary_retains_older_admitted_unit_and_huge_window(
    session_factory, persisted_run
):
    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(id=execution_id, run_id=persisted_run.id,
                                   role="developer", instruction_version="v1",
                                   provider="provider-a", model="model-a", status="SUCCEEDED"))
        session.add(ModelUsage(id=uuid4(), run_id=persisted_run.id,
                               agent_execution_id=execution_id, provider="provider-a",
                               model="model-a", prompt_version="v1", input_tokens=12,
                               output_tokens=3, cached_input_tokens=5, duration_ms=1,
                               tool_call_count=0, pricing_version="v1", currency="USD",
                               estimated_cost_minor=0))
    binding = _binding(persisted_run, execution_id)
    at = datetime.now(UTC) + timedelta(seconds=1)
    lower = at - timedelta(days=1)
    async with session_factory() as session, session.begin():
        session.add(TaskUsageWorkUnit(
            id=binding.work_unit_id, project_id=persisted_run.project_id,
            subject_kind="run", subject_id=persisted_run.id, run_id=persisted_run.id,
            admitted_phase_id=binding.admitted_phase_id,
            configured_route_digest=binding.configured_route.fingerprint,
            configured_route_members=_members(binding.configured_route),
            effective_route_digest=binding.effective_route.fingerprint,
            effective_route_members=_members(binding.effective_route),
            source_kind="agent_execution", source_id=execution_id,
            agent_execution_id=execution_id, schema_version=1,
            created_at=lower - timedelta(days=1),
        ))
        await session.execute(update(Run).where(Run.id == persisted_run.id)
                              .values(state="COMPLETED", updated_at=lower))
    query = TaskUsageBaselineQuery(session_factory)
    rows = await query.samples(project_ids={persisted_run.project_id}, as_of=at, window_days=1)
    assert len(rows) == 1 and rows[0].total_tokens == 20
    rows = await query.samples(project_ids={persisted_run.project_id},
                               as_of=at, window_days=2**63 - 1)
    assert len(rows) == 1 and rows[0].total_tokens == 20
    offset_at = at.astimezone(timezone(timedelta(hours=14)))
    rows = await query.samples(project_ids={persisted_run.project_id},
                               as_of=offset_at, window_days=2**63 - 1)
    assert len(rows) == 1 and rows[0].total_tokens == 20
    exclusions = await query.exclusions(
        project_ids={persisted_run.project_id}, as_of=offset_at,
        window_days=2**63 - 1,
        subjects={(persisted_run.project_id, "run", persisted_run.id)},
    )
    assert exclusions == {}


def test_persisted_policy_decoder_preserves_optional_limits_and_modes():
    project = uuid4()
    common = {"project_id": project, "revision": 7, "mode": "checkpoint",
              "warning_multiplier": 1.25, "checkpoint_multiplier": 2.5,
              "history_limit": 50, "minimum_comparables": 2,
              "allow_cross_project_fallback": False, "actor_id": uuid4()}
    empty = policy_from_row(TaskUsagePolicyRevision(**common, window_days=None,
        hard_token_cap=None, hard_context_cap=None, manual_estimate_tokens=None))
    assert empty.project_id == project and empty.revision == 7
    assert empty.mode.value == "checkpoint" and empty.window_days is None
    assert empty.hard_token_cap is None and empty.hard_context_cap is None
    assert empty.manual_estimate_tokens is None and not empty.allow_cross_project_fallback
    maximum = policy_from_row(TaskUsagePolicyRevision(**common, window_days=2**63 - 1,
        hard_token_cap=2**63 - 1, hard_context_cap=2**63 - 1,
        manual_estimate_tokens=2**63 - 1))
    assert (maximum.window_days, maximum.hard_token_cap,
            maximum.hard_context_cap, maximum.manual_estimate_tokens) == (2**63 - 1,) * 4
    assert (maximum.warning_multiplier, maximum.checkpoint_multiplier) == (1.25, 2.5)


@pytest.mark.integration
async def test_adk_gateway_cached_usage_persists_unchanged_and_normalizes_bound_totals(
    session_factory, persisted_run
):
    output = build_developer_output().model_dump_json()
    runtime = _FakeAdkRuntime([
        _make_invocation_result(output, input_tokens=100, output_tokens=5, cached_tokens=80),
        _make_invocation_result(output, input_tokens=100, output_tokens=5, cached_tokens=10),
    ])
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())
    store = UsageRepository(session_factory)
    observations = PostgresTaskUsageObservations(session_factory)
    route = RouteCohort.from_members((
        RouteMember(role="developer", provider="google", model="gemini-2.5-flash", ordinal=0),
    ))
    for uncached, cached in ((20, 80), (90, 10)):
        execution_id = uuid4()
        async with session_factory() as session, session.begin():
            session.add(AgentExecution(id=execution_id, run_id=persisted_run.id,
                                       role="developer", instruction_version="1",
                                       provider="google", model="gemini-2.5-flash", status="SUCCEEDED"))
        request = _make_request(AgentRole.DEVELOPER).model_copy(
            update={"run_id": persisted_run.id, "execution_id": execution_id}
        )
        result = await gateway.execute(request)
        assert result.usage is not None
        assert (result.usage.input_tokens, result.usage.cached_input_tokens) == (uncached, cached)
        await store.add_priced(persisted_run.id, execution_id, result.usage)
        await observations.bind(WorkUnitBinding(
            subject=SubjectKey(project_id=persisted_run.project_id, kind="run",
                               subject_id=persisted_run.id),
            work_unit_id=uuid4(), admitted_phase_id="implementation",
            configured_route=route, effective_route=route,
            source=SourceBinding(kind="agent_execution", source_id=execution_id),
        ))
    async with session_factory() as session, session.begin():
        rows = (await session.scalars(select(ModelUsage).where(ModelUsage.run_id == persisted_run.id))).all()
        assert sorted((row.input_tokens, row.cached_input_tokens) for row in rows) == [(20, 80), (90, 10)]
        await session.execute(update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED"))
    totals = await TaskUsageTotalsQuery(session_factory).totals(
        SubjectKey(project_id=persisted_run.project_id, kind="run", subject_id=persisted_run.id)
    )
    assert (totals.input_tokens, totals.output_tokens, totals.cached_input_tokens) == (200, 10, 90)
    assert totals.final_source_count == 2
    sample, = await TaskUsageBaselineQuery(session_factory).samples(
        project_ids={persisted_run.project_id}, as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    assert sample.total_tokens == 210
    assert sorted(sample.phase_role_breakdown) == [
        ("implementation", "developer", 105),
        ("implementation", "developer", 105),
    ]


@pytest.mark.integration
@pytest.mark.parametrize("unbound_has_final_usage", [True, False])
async def test_partial_source_bindings_never_train_whole_subject_baseline(
    session_factory, persisted_run, unbound_has_final_usage
):
    bound_id, unbound_id = uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        for execution_id in (bound_id, unbound_id):
            session.add(
                AgentExecution(
                    id=execution_id,
                    run_id=persisted_run.id,
                    role="developer",
                    instruction_version="v1",
                    provider="provider-a",
                    model="model-a",
                    status="SUCCEEDED",
                )
            )
        for execution_id in (bound_id, *([unbound_id] if unbound_has_final_usage else [])):
            session.add(
                ModelUsage(
                    id=uuid4(),
                    run_id=persisted_run.id,
                    agent_execution_id=execution_id,
                    provider="provider-a",
                    model="model-a",
                    prompt_version="v1",
                    input_tokens=100,
                    output_tokens=50,
                    cached_input_tokens=0,
                    duration_ms=1,
                    tool_call_count=0,
                    pricing_version="test-v1",
                    estimated_cost_minor=0,
                    currency="USD",
                    unknown_price_reason=None,
                )
            )
        await session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED")
        )
        session.add(
            TaskUsagePolicyRevision(
                project_id=persisted_run.project_id,
                revision=1,
                mode="report_only",
                warning_multiplier=1.5,
                checkpoint_multiplier=2.0,
                history_limit=50,
                minimum_comparables=20,
                manual_estimate_tokens=None,
                allow_cross_project_fallback=True,
                actor_id=uuid4(),
            )
        )
    await PostgresTaskUsageObservations(session_factory).bind(
        _binding(persisted_run, bound_id)
    )
    query = TaskUsageBaselineQuery(session_factory)
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    samples = await query.samples(
        project_ids={persisted_run.project_id}, as_of=as_of, window_days=365
    )
    sample, = [row for row in samples if row.subject_id == persisted_run.id]
    assert not sample.measured
    assert not sample.finalized
    assert sample.total_tokens is None
    assert sample.source_count == 2
    assert sample.route_members == ()
    exclusions = await query.exclusions(
        project_ids={persisted_run.project_id},
        as_of=as_of,
        window_days=365,
        subjects={(persisted_run.project_id, "run", persisted_run.id)},
    )
    assert exclusions["incomplete_usage"] == 1


@pytest.mark.integration
async def test_historical_exclusions_ignore_later_admitted_incomplete_source(
    session_factory, persisted_run
):
    early_id, late_id = uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(id=early_id, run_id=persisted_run.id,
                                   role="developer", instruction_version="v1",
                                   provider="provider-a", model="model-a", status="SUCCEEDED"))
        session.add(ModelUsage(id=uuid4(), run_id=persisted_run.id,
                               agent_execution_id=early_id, provider="provider-a",
                               model="model-a", prompt_version="v1", input_tokens=20,
                               output_tokens=5, cached_input_tokens=10, duration_ms=1,
                               tool_call_count=0, pricing_version="v1", currency="USD",
                               estimated_cost_minor=0))
        await session.execute(update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED"))
    writer = PostgresTaskUsageObservations(session_factory)
    await writer.bind(_binding(persisted_run, early_id))
    async with session_factory() as session:
        as_of = await session.scalar(select(func.clock_timestamp()))
    assert as_of is not None
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(id=late_id, run_id=persisted_run.id,
                                   role="developer", instruction_version="v1",
                                   provider="provider-a", model="model-a", status="RUNNING"))
    await writer.bind(_binding(persisted_run, late_id))
    query = TaskUsageBaselineQuery(session_factory)
    subjects = {(persisted_run.project_id, "run", persisted_run.id)}
    history = await query.samples(project_ids={persisted_run.project_id},
                                  as_of=as_of, window_days=365)
    assert len(history) == 1 and history[0].total_tokens == 35
    assert history[0].finalized and history[0].source_count == 1
    historical_exclusions = await query.exclusions(
        project_ids={persisted_run.project_id}, as_of=as_of,
        window_days=365, subjects=subjects,
    )
    assert "incomplete_usage" not in historical_exclusions
    async with session_factory() as session:
        now = await session.scalar(select(func.clock_timestamp()))
    assert now is not None
    current = await query.samples(project_ids={persisted_run.project_id},
                                  as_of=now, window_days=365)
    assert len(current) == 1 and current[0].total_tokens is None
    assert not current[0].finalized and current[0].source_count == 2
    assert (await query.exclusions(project_ids={persisted_run.project_id},
                                   as_of=now, window_days=365,
                                   subjects=subjects))["incomplete_usage"] == 1


@pytest.mark.integration
async def test_completed_but_unknown_usage_is_not_zero_and_is_reported_as_excluded(
    session_factory, persisted_run
):
    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=persisted_run.id,
                role="developer",
                instruction_version="v1",
                provider="provider-a",
                model="model-a",
                status="SUCCEEDED",
            )
        )
        await session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED")
        )
    await PostgresTaskUsageObservations(session_factory).bind(
        _binding(persisted_run, execution_id)
    )
    query = TaskUsageBaselineQuery(session_factory)
    sample, = await query.samples(
        project_ids={persisted_run.project_id},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    assert sample.outcome == "completed"
    assert sample.total_tokens is None
    assert not sample.finalized and not sample.measured
    assert (
        await query.exclusions(
            project_ids={persisted_run.project_id},
            as_of=datetime.now(UTC) + timedelta(seconds=1),
            window_days=365,
            subjects={(persisted_run.project_id, "run", persisted_run.id)},
        )
    )["incomplete_usage"] == 1


@pytest.mark.integration
async def test_concurrent_freeze_returns_one_immutable_snapshot(session_factory, persisted_run):
    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=persisted_run.id,
                role="developer",
                instruction_version="v1",
                provider="provider-a",
                model="model-a",
                status="SUCCEEDED",
            )
        )
        await session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED")
        )
        session.add(
            TaskUsagePolicyRevision(
                project_id=persisted_run.project_id,
                revision=1,
                mode="report_only",
                warning_multiplier=1.5,
                checkpoint_multiplier=2.0,
                history_limit=50,
                minimum_comparables=20,
                manual_estimate_tokens=None,
                allow_cross_project_fallback=True,
                actor_id=uuid4(),
            )
        )
    binding = _binding(persisted_run, execution_id)
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    query = TaskUsageBaselineQuery(session_factory)
    frozen_at = datetime.now(UTC) + timedelta(seconds=1)
    first, second = await asyncio.gather(
        query.freeze_baseline(binding, frozen_at=frozen_at),
        query.freeze_baseline(binding, frozen_at=frozen_at),
    )
    assert first.snapshot_digest == second.snapshot_digest
    assert first.history_digest == second.history_digest
    assert first.status == second.status == "insufficient_history"

    history_run = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=persisted_run.policy_version,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(history_run)
        await work.commit()
    history_execution = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=history_execution,
                run_id=history_run.id,
                role="developer",
                instruction_version="v1",
                provider="provider-a",
                model="model-a",
                status="SUCCEEDED",
            )
        )
        session.add(
            ModelUsage(
                id=uuid4(),
                run_id=history_run.id,
                agent_execution_id=history_execution,
                provider="provider-a",
                model="model-a",
                prompt_version="v1",
                input_tokens=25,
                output_tokens=10,
                cached_input_tokens=0,
                duration_ms=1,
                tool_call_count=0,
                pricing_version="test-v1",
                estimated_cost_minor=0,
                currency="USD",
                unknown_price_reason=None,
            )
        )
        await session.execute(update(Run).where(Run.id == history_run.id).values(state="COMPLETED"))
    history_binding = _binding(history_run, history_execution)
    await PostgresTaskUsageObservations(session_factory).bind(history_binding)
    after = await query.samples(
        project_ids={persisted_run.project_id},
        as_of=frozen_at + timedelta(seconds=1),
        window_days=365,
    )
    assert any(sample.subject_id == history_run.id and sample.total_tokens == 35 for sample in after)
    still_frozen = await query.freeze_baseline(binding, frozen_at=frozen_at + timedelta(seconds=2))
    assert still_frozen.snapshot_digest == first.snapshot_digest
    assert still_frozen.history_digest == first.history_digest


@pytest.mark.integration
async def test_unconfigured_project_freeze_is_inert_and_replay_is_none(session_factory, persisted_run):
    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(id=execution_id, run_id=persisted_run.id,
                                   role="developer", instruction_version="v1",
                                   provider="provider-a", model="model-a", status="SUCCEEDED"))
    binding = _binding(persisted_run, execution_id)
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    query = TaskUsageBaselineQuery(session_factory)
    assert await asyncio.gather(query.freeze_baseline(binding), query.freeze_baseline(binding)) == [None, None]
    assert await query.freeze_baseline(binding) is None
    async with session_factory() as session, session.begin():
        await PostgresTaskUsagePolicyRepository(session).append_policy_revision(
            MonitoringPolicy(project_id=persisted_run.project_id, revision=1),
            expected_revision=0, actor_id=uuid4(),
        )
    configured = await query.freeze_baseline(binding)
    assert configured is not None and configured.policy_revision == 1
    assert configured.status == "insufficient_history"
    async with session_factory() as session, session.begin():
        await PostgresTaskUsagePolicyRepository(session).append_policy_revision(
            MonitoringPolicy(project_id=persisted_run.project_id, revision=2),
            expected_revision=1, actor_id=uuid4(),
        )
    replay = await query.freeze_baseline(binding)
    assert replay is not None and replay.snapshot_digest == configured.snapshot_digest
    next_run = RunSnapshot(id=uuid4(), project_id=persisted_run.project_id,
                           task_id=persisted_run.task_id,
                           policy_version=persisted_run.policy_version)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(next_run)
        await work.commit()
    next_execution = uuid4()
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(id=next_execution, run_id=next_run.id,
                                   role="developer", instruction_version="v1",
                                   provider="provider-a", model="model-a", status="SUCCEEDED"))
    next_binding = _binding(next_run, next_execution)
    await PostgresTaskUsageObservations(session_factory).bind(next_binding)
    async def revise():
        async with session_factory() as session, session.begin():
            await PostgresTaskUsagePolicyRepository(session).append_policy_revision(
                MonitoringPolicy(project_id=persisted_run.project_id, revision=3),
                expected_revision=2, actor_id=uuid4(),
            )
    raced, _ = await asyncio.gather(query.freeze_baseline(next_binding), revise())
    assert raced is not None and raced.policy_revision in {2, 3}
    after_race = await query.freeze_baseline(next_binding)
    assert after_race is not None and after_race.snapshot_digest == raced.snapshot_digest


async def test_each_subscription_attempt_inherits_its_admitted_task_purpose(monkeypatch):
    task_a, task_b = uuid4(), uuid4()
    attempts = (uuid4(), uuid4(), uuid4())
    units = [SimpleNamespace(source_kind="subscription_attempt", source_id=attempts[index],
                             subscription_task_id=task_a if index < 2 else task_b)
             for index in range(3)]
    units.append(SimpleNamespace(source_kind="authoring_attempt", source_id=attempts[0],
                                 subscription_task_id=None))
    class Result:
        def all(self):
            return [(task_a, {"purpose": "independent_review"}),
                    (task_b, {"purpose": "routine_implementation"})]
    class Session:
        async def execute(self, statement):
            return Result()
    def decode(payload):
        contract = object.__new__(LogicalTaskContract)
        object.__setattr__(contract, "purpose", SpecialistPurpose(payload["purpose"]))
        return contract
    monkeypatch.setattr(baseline_module, "decode_subscription_record", decode)
    roles = await baseline_module._source_roles(Session(), units)
    assert [roles[("subscription_attempt", attempt)] for attempt in attempts] == [
        "independent_review", "independent_review", "routine_implementation"
    ]
    assert roles[("authoring_attempt", attempts[0])] == "authoring"


@pytest.mark.integration
async def test_frozen_evidence_uses_same_rolling_fallback_cohort_as_statistics(
    session_factory, persisted_run, monkeypatch
):
    execution_id, foreign_id = uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        session.add(Project(id=foreign_id, name="Foreign", canonical_path="/foreign-cohort",
                            canonical_path_key="/foreign-cohort", github_repository="owner/foreign-cohort",
                            default_branch="main"))
        session.add(AgentExecution(id=execution_id, run_id=persisted_run.id,
                                   role="developer", instruction_version="v1",
                                   provider="provider-a", model="model-a", status="SUCCEEDED"))
        session.add(TaskUsagePolicyRevision(project_id=persisted_run.project_id,
                                           revision=1, mode="report_only", warning_multiplier=1.5,
                                           checkpoint_multiplier=2.0, history_limit=2,
                                           minimum_comparables=1, allow_cross_project_fallback=True,
                                           actor_id=uuid4()))
    binding = _binding(persisted_run, execution_id)
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    at = datetime.now(UTC)
    def history(project, tokens, age):
        return BaselineSample(project_id=project, subject_kind="run", subject_id=uuid4(),
                              completed_at=at-timedelta(days=age), total_tokens=tokens,
                              outcome="completed", finalized=True, measured=tokens is not None,
                              configured_route=binding.configured_route.fingerprint,
                              effective_route=binding.effective_route.fingerprint)
    rows = (history(persisted_run.project_id, 10, 4),
            history(persisted_run.project_id, None, 3),
            history(persisted_run.project_id, None, 2), history(foreign_id, 40, 1),
            replace(history(persisted_run.project_id, None, 5),
                    outcome="failed", finalized=False),
            replace(history(foreign_id, None, 6), outcome="failed", finalized=False),
            replace(history(foreign_id, None, 7), outcome="failed", finalized=False,
                    configured_route="other"))
    selected = select_cohort(
        rows, rules=SamplingRules(history_limit=2, minimum_comparables=1),
        as_of=at, project_ids={persisted_run.project_id, foreign_id},
        target_project_id=persisted_run.project_id, subject_kind="run",
        configured_route=binding.configured_route.fingerprint,
        effective_route=binding.effective_route.fingerprint,
    )
    assert selected.cohort_source == "accessible_cross_project"
    assert selected.excluded_count == 2
    query = TaskUsageBaselineQuery(session_factory)
    async def fake_samples(session, *, project_ids, as_of, window_days):
        return rows
    monkeypatch.setattr(query, "_samples", fake_samples)
    frozen = await query.freeze_baseline(binding, frozen_at=at)
    assert frozen is not None
    assert frozen.status == "ready"
    assert (frozen.sample_count, frozen.outcome_count, frozen.source_count) == (1, 2, 2)
    assert frozen.mean_tokens == 40


@pytest.mark.integration
async def test_colliding_run_and_authoring_ids_keep_failed_and_legacy_facts_separate(
    session_factory, persisted_run, monkeypatch,
):
    shared_id = persisted_run.id
    epic_id, conversation_id = uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        session.add(Epic(id=epic_id, project_id=persisted_run.project_id,
                         title="collision", draft={}))
        session.add(BrainstormConversation(id=conversation_id, epic_id=epic_id,
                                          project_id=persisted_run.project_id, version=1))
        session.add(BrainstormJobRow(
            id=shared_id, epic_id=epic_id, project_id=persisted_run.project_id,
            conversation_id=conversation_id, version=1, state="proposed",
            snapshot={}, proposal={"title": "collision"}, proposal_digest="c" * 64,
            current_attempt_id=None, adopted_revision_id=None,
            wait_pool_revision=None, retry_authorized_until=None,
            override_unknown_usage=False,
        ))
        for status, input_tokens in (("SUCCEEDED", 4), ("FAILED", 2)):
            execution_id = uuid4()
            session.add(AgentExecution(
                id=execution_id, run_id=shared_id, role="developer",
                instruction_version="v1", provider="provider-a", model="model-a",
                status=status,
            ))
            session.add(ModelUsage(
                id=uuid4(), run_id=shared_id, agent_execution_id=execution_id,
                provider="provider-a", model="model-a", prompt_version="v1",
                input_tokens=input_tokens, output_tokens=1, cached_input_tokens=1,
                duration_ms=1, tool_call_count=0, pricing_version="v1",
                currency="USD", estimated_cost_minor=0,
            ))
        await session.execute(update(Run).where(Run.id == shared_id).values(state="COMPLETED"))
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    query = TaskUsageBaselineQuery(session_factory)
    rows = await query.samples(project_ids={persisted_run.project_id},
                               as_of=as_of, window_days=365)
    by_kind = {(row.subject_kind, row.subject_id): row for row in rows}
    measured = by_kind[("run", shared_id)]
    unknown = by_kind[("authoring_job", shared_id)]
    assert measured.total_tokens == 10 and measured.finalized
    assert measured.failed_attempt_count == 1
    assert unknown.total_tokens is None and not unknown.finalized
    assert unknown.failed_attempt_count == 0 and unknown.source_count == 0
    exclusions = await query.exclusions(
        project_ids={persisted_run.project_id}, as_of=as_of, window_days=365,
        subjects={(persisted_run.project_id, "run", shared_id),
                  (persisted_run.project_id, "authoring_job", shared_id)},
    )
    assert exclusions["unclassified_legacy"] == 1
    monkeypatch.setattr(baseline_module, "_BATCH_SIZE", 1)
    assert await query.samples(project_ids={persisted_run.project_id},
                               as_of=as_of, window_days=365) == rows
    assert await query.exclusions(
        project_ids={persisted_run.project_id}, as_of=as_of, window_days=365,
        subjects={(persisted_run.project_id, "run", shared_id),
                  (persisted_run.project_id, "authoring_job", shared_id)},
    ) == exclusions


@pytest.mark.integration
async def test_legacy_source_inventory_honors_historical_cutoff_and_inclusive_boundary(
    session_factory, persisted_run,
):
    second = RunSnapshot(id=uuid4(), project_id=persisted_run.project_id,
                         task_id=persisted_run.task_id,
                         policy_version=persisted_run.policy_version)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second)
        await work.commit()
    for run in (persisted_run, second):
        execution_id = uuid4()
        async with session_factory() as session, session.begin():
            session.add(AgentExecution(id=execution_id, run_id=run.id, role="developer",
                                       instruction_version="v1", provider="provider-a",
                                       model="model-a", status="SUCCEEDED"))
            session.add(ModelUsage(id=uuid4(), run_id=run.id,
                                   agent_execution_id=execution_id, provider="provider-a",
                                   model="model-a", prompt_version="v1", input_tokens=4,
                                   output_tokens=1, cached_input_tokens=2, duration_ms=1,
                                   tool_call_count=0, pricing_version="v1", currency="USD",
                                   estimated_cost_minor=0))
            await session.execute(update(Run).where(Run.id == run.id).values(state="COMPLETED"))
    historical_at = datetime.now(UTC) + timedelta(seconds=1)
    boundary = historical_at + timedelta(days=1)
    async with session_factory() as session, session.begin():
        future_execution_id = uuid4()
        session.add(AgentExecution(id=future_execution_id, run_id=persisted_run.id,
                                   role="reviewer", instruction_version="v1",
                                   provider="provider-a", model="model-a",
                                   status="FAILED", created_at=boundary))
        session.add(ModelUsage(id=uuid4(), run_id=persisted_run.id,
                               agent_execution_id=future_execution_id,
                               provider="provider-a", model="model-a", prompt_version="v1",
                               input_tokens=20, output_tokens=1, cached_input_tokens=0,
                               duration_ms=1, tool_call_count=0, pricing_version="v1",
                               currency="USD", estimated_cost_minor=0,
                               created_at=historical_at))
        task_id = uuid4()
        session.add(SubscriptionTask(id=task_id, run_id=second.id, task_id=uuid4(),
                                     idempotency_key="future-task", payload={},
                                     created_at=boundary))
        await session.flush()
        session.add(SubscriptionAttempt(id=uuid4(), run_id=second.id,
                                        task_row_id=task_id, attempt_number=1,
                                        idempotency_key="future-attempt", route_payload={},
                                        created_at=boundary))
    query = TaskUsageBaselineQuery(session_factory)
    subjects = {(run.project_id, "run", run.id) for run in (persisted_run, second)}
    before = await query.samples(project_ids={persisted_run.project_id},
                                 as_of=historical_at, window_days=365)
    assert {row.subject_id: (row.total_tokens, row.finalized, row.source_count)
            for row in before} == {run.id: (7, True, 1) for run in (persisted_run, second)}
    assert await query.exclusions(project_ids={persisted_run.project_id},
                                  as_of=historical_at, window_days=365,
                                  subjects=subjects) == {}
    at_boundary = await query.samples(project_ids={persisted_run.project_id},
                                      as_of=boundary, window_days=365)
    assert {row.subject_id: row.total_tokens for row in at_boundary} == {
        persisted_run.id: 28, second.id: None,
    }
    assert (await query.exclusions(project_ids={persisted_run.project_id},
                                   as_of=boundary, window_days=365,
                                   subjects=subjects))["unclassified_legacy"] == 1


@pytest.mark.integration
async def test_report_keeps_one_snapshot_when_final_source_commits_between_projections(
    session_factory, persisted_run, monkeypatch,
):
    first_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(id=first_id, run_id=persisted_run.id,
                                   role="developer", instruction_version="v1",
                                   provider="provider-a", model="model-a", status="SUCCEEDED"))
        session.add(ModelUsage(id=uuid4(), run_id=persisted_run.id,
                               agent_execution_id=first_id, provider="provider-a",
                               model="model-a", prompt_version="v1", input_tokens=4,
                               output_tokens=1, cached_input_tokens=2, duration_ms=1,
                               tool_call_count=0, pricing_version="v1", currency="USD",
                               estimated_cost_minor=0))
        await session.execute(update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED"))
    await PostgresTaskUsageObservations(session_factory).bind(_binding(persisted_run, first_id))
    query = TaskUsageBaselineQuery(session_factory)
    original = query._samples
    sampled = asyncio.Event()
    resume = asyncio.Event()
    async def paused_samples(session, *, project_ids, as_of, window_days):
        rows = await original(session, project_ids=project_ids, as_of=as_of,
                              window_days=window_days)
        sampled.set()
        await resume.wait()
        return rows
    monkeypatch.setattr(query, "_samples", paused_samples)
    as_of = datetime.now(UTC) + timedelta(days=1)
    service = TaskUsageBaselineService(query)
    filters = UsageReportFilters(as_of=as_of, rules=SamplingRules(
        history_limit=10, minimum_comparables=1,
    ))
    report_task = asyncio.create_task(service.report(filters))
    try:
        await asyncio.wait_for(sampled.wait(), 10)
        second_id = uuid4()
        async with session_factory() as session, session.begin():
            session.add(AgentExecution(id=second_id, run_id=persisted_run.id,
                                       role="reviewer", instruction_version="v1",
                                       provider="provider-a", model="model-a", status="SUCCEEDED"))
            session.add(ModelUsage(id=uuid4(), run_id=persisted_run.id,
                                   agent_execution_id=second_id, provider="provider-a",
                                   model="model-a", prompt_version="v1", input_tokens=8,
                                   output_tokens=1, cached_input_tokens=0, duration_ms=1,
                                   tool_call_count=0, pricing_version="v1", currency="USD",
                                   estimated_cost_minor=0))
    finally:
        resume.set()
    report = await asyncio.wait_for(report_task, 20)
    assert report["overview"]["sample_count"] == 1
    assert report["overview"]["mean_tokens"] == 7
    assert report["excluded_outcomes"] == {}
    assert report["failed_attempt_count"] == 0
    monkeypatch.setattr(query, "_samples", original)
    later = await service.report(filters)
    assert later["overview"]["sample_count"] == 0
    assert later["excluded_outcomes"]["incomplete_usage"] == 1


@pytest.mark.integration
async def test_authoring_and_run_subjects_are_separate_history_rows(session_factory, persisted_run):
    epic_id, conversation_id, job_id = uuid4(), uuid4(), uuid4()
    foreign_project, foreign_epic, foreign_conversation, foreign_job = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    async with session_factory() as session, session.begin():
        session.add(Project(id=foreign_project, name="Other history project",
                            canonical_path="/other-history-project",
                            canonical_path_key="/other-history-project",
                            github_repository="owner/other-history-project",
                            default_branch="main"))
        await session.flush()
        session.add(Epic(id=epic_id, project_id=persisted_run.project_id, title="test", draft={}))
        session.add(Epic(id=foreign_epic, project_id=foreign_project, title="foreign", draft={}))
        session.add(
            BrainstormConversation(
                id=conversation_id,
                epic_id=epic_id,
                project_id=persisted_run.project_id,
                version=1,
            )
        )
        session.add(BrainstormConversation(id=foreign_conversation, epic_id=foreign_epic,
                                          project_id=foreign_project, version=1))
        session.add(
            BrainstormJobRow(
                id=job_id,
                epic_id=epic_id,
                project_id=persisted_run.project_id,
                conversation_id=conversation_id,
                version=1,
                state="proposed",
                snapshot={},
                proposal={"title": "test"},
                proposal_digest="c" * 64,
                current_attempt_id=None,
                adopted_revision_id=None,
                wait_pool_revision=None,
                retry_authorized_until=None,
                override_unknown_usage=False,
            )
        )
        session.add(BrainstormJobRow(id=foreign_job, epic_id=foreign_epic,
                                    project_id=foreign_project,
                                    conversation_id=foreign_conversation, version=1,
                                    state="proposed", snapshot={}, proposal={"title": "foreign"},
                                    proposal_digest="d" * 64, current_attempt_id=None,
                                    adopted_revision_id=None, wait_pool_revision=None,
                                    retry_authorized_until=None, override_unknown_usage=False))
        await session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED")
        )
    rows = await TaskUsageBaselineQuery(session_factory).samples(
        project_ids={persisted_run.project_id, foreign_project},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    assert len(rows) == 3
    assert {(row.subject_kind, row.task_kind) for row in rows} == {
        ("run", "implementation"),
        ("authoring_job", "authoring"),
    }
    assert {row.outcome for row in rows} == {"completed"}
    assert {(row.project_id, row.subject_kind, row.subject_id) for row in rows} == {
        (persisted_run.project_id, "run", persisted_run.id),
        (persisted_run.project_id, "authoring_job", job_id),
        (foreign_project, "authoring_job", foreign_job),
    }
    assert all(row.total_tokens is None and not row.measured for row in rows)


@pytest.mark.integration
async def test_manual_frozen_baseline_is_labelled_and_has_no_measured_samples(
    session_factory, persisted_run
):
    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=persisted_run.id,
                role="developer",
                instruction_version="v1",
                provider="provider-a",
                model="model-a",
                status="SUCCEEDED",
            )
        )
        session.add(
            TaskUsagePolicyRevision(
                project_id=persisted_run.project_id,
                revision=1,
                mode="report_only",
                warning_multiplier=1.5,
                checkpoint_multiplier=2.0,
                history_limit=50,
                minimum_comparables=20,
                manual_estimate_tokens=75,
                allow_cross_project_fallback=True,
                actor_id=uuid4(),
            )
        )
        await session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED")
        )
    binding = _binding(persisted_run, execution_id)
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    reference = await TaskUsageBaselineQuery(session_factory).freeze_baseline(binding)
    assert reference.status == "manual"
    assert reference.reference_tokens == 75
    assert reference.sample_count == reference.coverage_count == reference.outcome_count == 0


@pytest.mark.integration
async def test_extreme_manual_threshold_freezes_and_replays_after_policy_revision(
    session_factory, persisted_run,
):
    binding = _binding(persisted_run, uuid4())
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(id=binding.source.source_id, run_id=persisted_run.id,
                                   role="developer", instruction_version="v1",
                                   provider="provider-a", model="model-a", status="RUNNING"))
        session.add(TaskUsagePolicyRevision(
            project_id=persisted_run.project_id, revision=1, mode="report_only",
            warning_multiplier=1e308, checkpoint_multiplier=1e308,
            history_limit=50, minimum_comparables=20,
            manual_estimate_tokens=2**63 - 1,
            allow_cross_project_fallback=True, actor_id=uuid4(),
        ))
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    query = TaskUsageBaselineQuery(session_factory)
    frozen = await query.freeze_baseline(binding)
    assert frozen is not None and frozen.status == "manual"
    assert frozen.warning_threshold_tokens == sys.float_info.max
    assert frozen.checkpoint_threshold_tokens == sys.float_info.max
    async with session_factory() as session, session.begin():
        session.add(TaskUsagePolicyRevision(
            project_id=persisted_run.project_id, revision=2, mode="report_only",
            warning_multiplier=1.5, checkpoint_multiplier=2.0,
            history_limit=50, minimum_comparables=20, manual_estimate_tokens=75,
            allow_cross_project_fallback=True, actor_id=uuid4(),
        ))
    assert await query.freeze_baseline(binding) == frozen


@pytest.mark.integration
async def test_extreme_ready_history_threshold_freezes_from_large_measured_value(
    session_factory, persisted_run,
):
    target = RunSnapshot(id=uuid4(), project_id=persisted_run.project_id,
                         task_id=persisted_run.task_id,
                         policy_version=persisted_run.policy_version)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(target)
        await work.commit()
    history_binding = _binding(persisted_run, uuid4())
    target_binding = _binding(target, uuid4())
    async with session_factory() as session, session.begin():
        for binding, run_id in ((history_binding, persisted_run.id),
                                (target_binding, target.id)):
            session.add(AgentExecution(id=binding.source.source_id, run_id=run_id,
                                       role="developer", instruction_version="v1",
                                       provider="provider-a", model="model-a", status="SUCCEEDED"))
        session.add(ModelUsage(id=uuid4(), run_id=persisted_run.id,
                               agent_execution_id=history_binding.source.source_id,
                               provider="provider-a", model="model-a", prompt_version="v1",
                               input_tokens=2_000_000_000, output_tokens=0,
                               cached_input_tokens=0, duration_ms=1, tool_call_count=0,
                               pricing_version="v1", currency="USD", estimated_cost_minor=0))
        await session.execute(update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED"))
        session.add(TaskUsagePolicyRevision(
            project_id=persisted_run.project_id, revision=1, mode="report_only",
            warning_multiplier=1e308, checkpoint_multiplier=1e308,
            history_limit=50, minimum_comparables=1, manual_estimate_tokens=None,
            allow_cross_project_fallback=False, actor_id=uuid4(),
        ))
    writer = PostgresTaskUsageObservations(session_factory)
    await writer.bind(history_binding)
    await writer.bind(target_binding)
    frozen = await TaskUsageBaselineQuery(session_factory).freeze_baseline(target_binding)
    assert frozen is not None and frozen.status == "ready" and frozen.sample_count == 1
    assert frozen.warning_threshold_tokens == sys.float_info.max
    assert frozen.checkpoint_threshold_tokens == sys.float_info.max


@pytest.mark.integration
async def test_durable_current_acceptance_is_visible_before_merge(
    session_factory, tmp_path
):
    from forge.application.ports.evidence import SubscriptionAcceptanceEvidenceDraft
    from test_subscription_acceptance_evidence_persistence import acceptance_evidence_case

    factory, manifest, artifact = await acceptance_evidence_case(session_factory, tmp_path)
    async with factory() as work:
        await work.evidence.record_set(
            SubscriptionAcceptanceEvidenceDraft(manifest), artifact
        )
        run = await work.session.get(Run, manifest.run_id)
        assert run.state == "VALIDATING"
        project_id = run.project_id
        await work.commit()
    rows = await TaskUsageBaselineQuery(session_factory).samples(
        project_ids={project_id},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    sample, = [row for row in rows if row.subject_id == manifest.run_id]
    assert sample.outcome == "accepted"
    assert sample.subject_kind == "run"

    async with factory() as work:
        run = await work.session.get(Run, manifest.run_id)
        run.candidate_commit = "a" * 40
        await work.commit()
    rows = await TaskUsageBaselineQuery(session_factory).samples(
        project_ids={project_id},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    sample, = [row for row in rows if row.subject_id == manifest.run_id]
    assert sample.outcome == "unknown"

    async with factory() as work:
        run = await work.session.get(Run, manifest.run_id)
        run.candidate_commit = None
        task = await work.session.scalar(
            select(SubscriptionTask).where(
                SubscriptionTask.run_id == manifest.run_id,
                SubscriptionTask.task_id == manifest.producer_task_id,
            )
        )
        assert task is not None
        task.parent_task_id = task.id
        await work.commit()
    rows = await TaskUsageBaselineQuery(session_factory).samples(
        project_ids={project_id},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    sample, = [row for row in rows if row.subject_id == manifest.run_id]
    assert sample.outcome == "unknown"


@pytest.mark.integration
async def test_acceptance_uses_producer_row_identity_and_exact_root_attempt(
    session_factory, tmp_path,
):
    from forge.application.ports.evidence import SubscriptionAcceptanceEvidenceDraft
    from test_subscription_acceptance_evidence_persistence import acceptance_evidence_case

    factory, manifest, artifact = await acceptance_evidence_case(session_factory, tmp_path)
    async with factory() as work:
        await work.evidence.record_set(SubscriptionAcceptanceEvidenceDraft(manifest), artifact)
        run = await work.session.get(Run, manifest.run_id)
        project_id = run.project_id
        await work.commit()
    async with session_factory() as session, session.begin():
        task = await session.get(SubscriptionTask, manifest.producer_task_id)
        assert task is not None and task.parent_task_id is None
        assert task.id == task.task_id  # Normal writer identity is equal; schema permits distinct IDs.
        task.task_id = uuid4()
        decoy_id = uuid4()
        session.add(SubscriptionAttempt(
            id=decoy_id, run_id=manifest.run_id, task_row_id=task.id,
            attempt_number=999, idempotency_key="same-task-decoy", route_payload={},
        ))
    assert task.id != task.task_id
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    query = TaskUsageBaselineQuery(session_factory)

    async def outcome():
        rows = await query.samples(project_ids={project_id}, as_of=as_of, window_days=365)
        sample, = (row for row in rows if row.subject_id == manifest.run_id)
        exclusions = await query.exclusions(
            project_ids={project_id}, as_of=as_of, window_days=365,
            subjects={(project_id, "run", manifest.run_id)},
        )
        return sample.outcome, exclusions

    accepted, excluded = await outcome()
    assert accepted == "accepted" and excluded["unclassified_legacy"] == 1
    async with session_factory() as session, session.begin():
        await session.execute(update(SubscriptionAttempt).where(
            SubscriptionAttempt.id == manifest.producer_attempt_id,
        ).values(created_at=as_of + timedelta(days=1)))
    blocked, excluded = await outcome()
    assert blocked == "unknown" and excluded["in_progress"] == 1
    async with session_factory() as session, session.begin():
        await session.execute(update(SubscriptionAttempt).where(
            SubscriptionAttempt.id == manifest.producer_attempt_id,
        ).values(created_at=as_of))
        parent_id = uuid4()
        session.add(SubscriptionTask(id=parent_id, run_id=manifest.run_id,
                                     task_id=uuid4(), idempotency_key="parent-task",
                                     payload={}))
        await session.flush()
        await session.execute(update(SubscriptionTask).where(
            SubscriptionTask.id == manifest.producer_task_id,
        ).values(parent_task_id=parent_id))
    child, excluded = await outcome()
    assert child == "unknown" and excluded["in_progress"] == 1


@pytest.mark.integration
async def test_historical_acceptance_requires_evidence_and_producer_existing_by_cutoff(
    session_factory, tmp_path,
):
    from forge.application.ports.evidence import SubscriptionAcceptanceEvidenceDraft
    from test_subscription_acceptance_evidence_persistence import acceptance_evidence_case

    factory, manifest, artifact = await acceptance_evidence_case(session_factory, tmp_path)
    query = TaskUsageBaselineQuery(session_factory)
    service = TaskUsageBaselineService(query)
    async with factory() as work:
        run = await work.session.get(Run, manifest.run_id)
        assert run.state == "VALIDATING"
        project_id = run.project_id

    async with session_factory() as session:
        before_at = await session.scalar(select(func.clock_timestamp()))
    assert before_at is not None
    subjects = {(project_id, "run", manifest.run_id)}

    async def projection(at):
        rows = await query.samples(project_ids={project_id}, as_of=at, window_days=365)
        sample, = (row for row in rows if row.subject_id == manifest.run_id)
        exclusions = await query.exclusions(project_ids={project_id}, as_of=at,
                                            window_days=365, subjects=subjects)
        report = await service.report(UsageReportFilters(
            project_ids=frozenset({project_id}), as_of=at,
            rules=SamplingRules(window_days=365),
        ))
        return sample, exclusions, report

    before = await projection(before_at)
    assert before[0].outcome == "unknown" and before[0].total_tokens is None
    assert before[1]["in_progress"] == 1
    assert before[2]["excluded_outcomes"]["in_progress"] == 1

    async with factory() as work:
        await work.evidence.record_set(SubscriptionAcceptanceEvidenceDraft(manifest), artifact)
        await work.commit()
    async with session_factory() as session:
        accepted_at = await session.scalar(select(EvidenceSet.created_at).where(
            EvidenceSet.id == manifest.evidence_set_id,
        ))
    assert accepted_at is not None and accepted_at > before_at
    later_at = accepted_at + timedelta(days=1)

    historical = await projection(before_at)
    assert historical[0].outcome == before[0].outcome
    assert historical[0].total_tokens is None and not historical[0].measured
    assert historical[1] == before[1]
    assert historical[2]["overview"] == before[2]["overview"]
    assert historical[2]["comparable_references"] == before[2]["comparable_references"]
    assert historical[2]["excluded_outcomes"] == before[2]["excluded_outcomes"]
    inclusive = await projection(accepted_at)
    assert inclusive[0].outcome == "accepted" and inclusive[0].total_tokens is None
    assert inclusive[1]["unclassified_legacy"] == 1
    assert inclusive[2]["excluded_outcomes"]["unclassified_legacy"] == 1
    assert (await projection(later_at))[0].outcome == "accepted"

    for model, row_id in (
        (SubscriptionTask, manifest.producer_task_id),
        (SubscriptionAttempt, manifest.producer_attempt_id),
    ):
        async with session_factory() as session, session.begin():
            await session.execute(update(model).where(model.id == row_id)
                                  .values(created_at=later_at))
        blocked = await projection(accepted_at)
        assert blocked[0].outcome == "unknown" and blocked[1]["in_progress"] == 1
        assert blocked[2]["excluded_outcomes"]["in_progress"] == 1
        assert (await projection(later_at))[0].outcome == "accepted"
        async with session_factory() as session, session.begin():
            await session.execute(update(model).where(model.id == row_id)
                                  .values(created_at=before_at))


@pytest.mark.integration
async def test_legacy_model_usage_uses_defensible_actual_routes_and_keeps_failed_attempts(
    session_factory, persisted_run
):
    execution_ids = (uuid4(), uuid4())
    async with session_factory() as session, session.begin():
        for index, (execution_id, status) in enumerate(
            zip(execution_ids, ("FAILED", "SUCCEEDED"), strict=True)
        ):
            session.add(
                AgentExecution(
                    id=execution_id,
                    run_id=persisted_run.id,
                    role="developer" if index == 0 else "reviewer",
                    instruction_version="v1",
                    provider="provider-a",
                    model=f"model-{index}",
                    status=status,
                )
            )
            session.add(
                ModelUsage(
                    id=uuid4(),
                    run_id=persisted_run.id,
                    agent_execution_id=execution_id,
                    provider="provider-a",
                    model=f"model-{index}",
                    prompt_version="v1",
                    input_tokens=40,
                    output_tokens=10,
                    cached_input_tokens=20 if index == 0 else 5,
                    duration_ms=1,
                    tool_call_count=0,
                    pricing_version="test-v1",
                    estimated_cost_minor=0,
                    currency="USD",
                    unknown_price_reason=None,
                )
            )
        await session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED")
        )
    query = TaskUsageBaselineQuery(session_factory)
    sample, = await query.samples(
        project_ids={persisted_run.project_id},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    assert sample.total_tokens == 125
    assert sorted(amount for _, _, amount in sample.phase_role_breakdown) == [55, 70]
    assert sample.measured and sample.finalized
    assert sample.configured_route is None
    assert {member[2] for member in sample.route_members} == {"model-0", "model-1"}
    assert sample.failed_attempt_count == 1
    assert "unclassified_legacy" not in await query.exclusions(
        project_ids={persisted_run.project_id},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
        subjects={(persisted_run.project_id, "run", persisted_run.id)},
    )
