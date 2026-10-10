import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.agents.adk_gateway import GoogleAdkGateway
from forge.domain.actor import AgentRole
from forge.domain.run import RunSnapshot
from forge.domain.subscription import LogicalTaskContract, SpecialistPurpose
from forge.domain.task_usage_baselines import BaselineSample
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
from forge.persistence.models.execution import AgentExecution, ModelUsage
from forge.persistence.models.project import Project
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.task_usage import TaskUsagePolicyRevision
from forge.persistence.queries import task_usage_baselines as baseline_module
from forge.persistence.queries.task_usage_baselines import TaskUsageBaselineQuery
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.task_usage_observations import PostgresTaskUsageObservations
from forge.persistence.repositories.task_usage_policy import PostgresTaskUsagePolicyRepository
from forge.persistence.repositories.usage import UsageRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select, update

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
    assert [roles[attempt] for attempt in attempts] == [
        "independent_review", "independent_review", "routine_implementation"
    ]


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
            history(persisted_run.project_id, None, 2), history(foreign_id, 40, 1))
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
async def test_authoring_and_run_subjects_are_separate_history_rows(session_factory, persisted_run):
    epic_id, conversation_id, job_id = uuid4(), uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        session.add(Epic(id=epic_id, project_id=persisted_run.project_id, title="test", draft={}))
        session.add(
            BrainstormConversation(
                id=conversation_id,
                epic_id=epic_id,
                project_id=persisted_run.project_id,
                version=1,
            )
        )
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
        await session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="COMPLETED")
        )
    rows = await TaskUsageBaselineQuery(session_factory).samples(
        project_ids={persisted_run.project_id},
        as_of=datetime.now(UTC) + timedelta(seconds=1),
        window_days=365,
    )
    assert {(row.subject_kind, row.task_kind) for row in rows} == {
        ("run", "implementation"),
        ("authoring_job", "authoring"),
    }
    assert {row.outcome for row in rows} == {"completed"}
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
