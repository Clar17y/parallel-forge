"""PostgreSQL checkpoint transitions and command replay at the durable boundary."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.services.task_usage_controls import RunTaskUsageControls
from forge.domain.task_usage_contract import (
    CheckpointRecord,
    CheckpointState,
    MonitoringMode,
    MonitoringPolicy,
    OwnerCommand,
    RouteCohort,
    RouteMember,
)
from forge.persistence.database import create_engine
from forge.persistence.models.task_usage import TaskUsageWorkUnit
from forge.persistence.queries.task_usage_baselines import TaskUsageBaselineQuery
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.task_usage_admissions import (
    PostgresTaskUsageAdmissionRepository,
)
from forge.persistence.repositories.task_usage_checkpoints import (
    CheckpointConflict,
    PostgresTaskUsageCheckpointRepository,
)
from forge.persistence.repositories.task_usage_observations import decode_binding
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.integration
async def test_checkpoint_cas_replay_and_new_generation(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    project, task, run, execution, unit, observation = (uuid4() for _ in range(6))
    checkpoint_id, actor = uuid4(), uuid4()
    cohort = RouteCohort.from_members((RouteMember(role="developer", provider="test", model="test", ordinal=0),)).fingerprint
    try:
        async with engine.begin() as db:
            await db.execute(
                text("INSERT INTO projects (id, canonical_path, github_repository, default_branch) VALUES (:id, :path, :repo, 'main')"),
                {"id": project, "path": f"/tmp/{project}", "repo": f"owner/{project}"},
            )
            await db.execute(text("INSERT INTO project_policy_versions (project_id, version, policy_digest, document_schema_version, document) VALUES (:id, 1, :digest, 1, '{}'::jsonb)"), {"id": project, "digest": "a" * 64})
            await db.execute(text("INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'checkpoint', :digest)"), {"task": task, "project": project, "digest": "b" * 64})
            await db.execute(text("INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, 'CREATED', 0, 0, 0, 0, 0, 0, 'DISABLED')"), {"run": run, "project": project, "task": task})
            await db.execute(text("INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'test', 'RUNNING')"), {"execution": execution, "run": run})
            await db.execute(text("INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'agent_execution', :execution, :execution, 1)"), {"unit": unit, "project": project, "run": run, "execution": execution, "digest": cohort, "members": '[{"role":"developer","provider":"test","model":"test","ordinal":0}]'})
            await db.execute(text("INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, :actor)"), {"project": project, "actor": actor})
            await db.execute(text("INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, observed_at, schema_version) VALUES (:id, :project, :unit, 'event', 1, 'cumulative', 'source', 100, false, now(), 1)"), {"id": observation, "project": project, "unit": unit})
            await db.execute(text("INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, reference_tokens, warning_threshold_tokens, checkpoint_threshold_tokens, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :history, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'manual', 25, 37.5, 50, '{}'::jsonb)"), {"unit": unit, "project": project, "run": run, "history": "d" * 64, "snapshot": "e" * 64, "cohort": cohort})

        async with AsyncSession(engine) as session:
            binding = decode_binding(await session.get(TaskUsageWorkUnit, unit))
            checkpoint = CheckpointRecord(checkpoint_id=checkpoint_id, binding=binding, baseline_binding=binding, state=CheckpointState.REQUESTED, policy_revision=1, reference_digest="e" * 64, trigger_event_id="event")
            repo = PostgresTaskUsageCheckpointRepository(session)
            assert await repo.request(checkpoint) == checkpoint
            assert await repo.request(checkpoint) == checkpoint
            with pytest.raises(CheckpointConflict):
                await repo.request(replace(checkpoint, checkpoint_id=uuid4()))
            await session.execute(text("INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, observed_at, schema_version) VALUES (:id, :project, :unit, 'event2', 2, 'cumulative', 'source', 101, false, now(), 1)"), {"id": uuid4(), "project": project, "unit": unit})
            second = replace(checkpoint, checkpoint_id=uuid4(), trigger_event_id="event2")
            with pytest.raises(CheckpointConflict):
                await repo.request(second)
            requested = await repo.get(checkpoint_id)
            assert requested == checkpoint
            pausing = await repo.transition(checkpoint_id, expected_version=0, state=CheckpointState.PAUSING, process_settled=False)
            assert pausing.version == 1
            with pytest.raises(CheckpointConflict):
                await repo.transition(checkpoint_id, expected_version=1, state=CheckpointState.PAUSED, process_settled=False)
            paused = await repo.transition(checkpoint_id, expected_version=1, state=CheckpointState.PAUSED, process_settled=True)
            assert paused.version == 2 and paused.process_settled
            await session.execute(text("INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 2, 'checkpoint', 1.5, 2, 50, 20, :actor)"), {"project": project, "actor": actor})
            policy_command = OwnerCommand(
                command_id=uuid4(), checkpoint_id=checkpoint_id, expected_version=2,
                idempotency_key="policy", action="set_policy", actor_id=actor,
                requested_policy=MonitoringPolicy(project_id=project, revision=2, mode=MonitoringMode.CHECKPOINT),
            )
            await repo.apply_owner_command(
                policy_command, state=CheckpointState.PAUSED,
                affected_snapshot="f" * 64, monitoring_policy_revision=2,
            )
            command = OwnerCommand(command_id=uuid4(), checkpoint_id=checkpoint_id, expected_version=3, idempotency_key="next", action="continue", actor_id=actor, continue_mode="next_checkpoint")
            receipt = await repo.apply_owner_command(command, state=CheckpointState.RESUMED, affected_snapshot="f" * 64, next_checkpoint_threshold_tokens=101, usage_pauses_disabled=False)
            assert receipt.result_version == 4 and not receipt.replayed
            assert (await repo.apply_owner_command(replace(command, command_id=uuid4()), state=CheckpointState.RESUMED, affected_snapshot="f" * 64)).replayed
            with pytest.raises(CheckpointConflict):
                await repo.apply_owner_command(replace(command, actor_id=uuid4()), state=CheckpointState.RESUMED, affected_snapshot="f" * 64)
            latest = await repo.latest_for_subject(binding.subject)
            assert latest is not None and latest.next_checkpoint_threshold_tokens == 101
            assert latest.process_settled and latest.state is CheckpointState.RESUMED
            assert (await repo.request(checkpoint)).state is CheckpointState.RESUMED
            created_second = await repo.request(second)
            assert created_second.generation == 2
            assert created_second.monitoring_policy_revision == 2
            assert created_second.policy_revision == 1
            assert created_second.reference_digest == "e" * 64
            assert created_second.next_checkpoint_threshold_tokens == 101
            assert (await repo.latest_for_subject(binding.subject)).checkpoint_id == second.checkpoint_id
            assert (await repo.active_for_subject(binding.subject)).checkpoint_id == second.checkpoint_id
            pausing_second = await repo.transition(
                second.checkpoint_id, expected_version=0,
                state=CheckpointState.PAUSING, process_settled=False,
            )
            paused_second = await repo.transition(
                second.checkpoint_id, expected_version=pausing_second.version,
                state=CheckpointState.PAUSED, process_settled=True,
            )
            await repo.apply_owner_command(
                OwnerCommand(
                    command_id=uuid4(), checkpoint_id=second.checkpoint_id,
                    expected_version=paused_second.version, idempotency_key="next-again",
                    action="continue", actor_id=actor, continue_mode="next_checkpoint",
                ),
                state=CheckpointState.RESUMED, affected_snapshot="f" * 64,
                next_checkpoint_threshold_tokens=150,
                usage_pauses_disabled=False,
            )
            await session.execute(text("INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, observed_at, schema_version) VALUES (:id, :project, :unit, 'event3', 3, 'cumulative', 'source', 151, false, now(), 1)"), {"id": uuid4(), "project": project, "unit": unit})
            third = await repo.request(
                replace(checkpoint, checkpoint_id=uuid4(), trigger_event_id="event3")
            )
            assert third.generation == 3
            assert third.monitoring_policy_revision == 2
            assert third.policy_revision == 1
            assert third.reference_digest == "e" * 64
            assert third.next_checkpoint_threshold_tokens == 150
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_admission_freezes_policy_and_absence_before_delayed_source(migrated_database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(migrated_database_url)
    cohort = RouteCohort.from_members((RouteMember(role="developer", provider="test", model="test", ordinal=0),))
    admitted = []
    try:
        async with engine.begin() as db:
            for number in (0, 1):
                project, task, run, execution, unit = (uuid4() for _ in range(5))
                await db.execute(text("INSERT INTO projects (id, canonical_path, github_repository, default_branch) VALUES (:id, :path, :repo, 'main')"), {"id": project, "path": f"/tmp/{project}", "repo": f"owner/{project}"})
                await db.execute(text("INSERT INTO project_policy_versions (project_id, version, policy_digest, document_schema_version, document) VALUES (:id, 1, :digest, 1, '{}'::jsonb)"), {"id": project, "digest": "a" * 64})
                await db.execute(text("INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'delayed', :digest)"), {"task": task, "project": project, "digest": "b" * 64})
                await db.execute(text("INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, 'CREATED', 0, 0, 0, 0, 0, 0, 'DISABLED')"), {"run": run, "project": project, "task": task})
                await db.execute(text("INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'test', 'RUNNING')"), {"execution": execution, "run": run})
                await db.execute(text("INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'agent_execution', :execution, :execution, 1)"), {"unit": unit, "project": project, "run": run, "execution": execution, "digest": cohort.fingerprint, "members": '[{"role":"developer","provider":"test","model":"test","ordinal":0}]'})
                if number == 0:
                    await db.execute(text("INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, manual_estimate_tokens, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, 100, :actor)"), {"project": project, "actor": uuid4()})
                admitted.append((project, unit))
        snapshots = []
        for project, unit in admitted:
            async with AsyncSession(engine) as session:
                binding = decode_binding(await session.get(TaskUsageWorkUnit, unit))
                snapshot = await PostgresTaskUsageAdmissionRepository(session).admit(binding.subject, cohort)
                await session.commit()
                snapshots.append((binding, snapshot))
        assert snapshots[0][1].policy_revision == 1
        assert snapshots[1][1].policy_revision is None
        async with engine.begin() as db:
            await db.execute(text("INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, manual_estimate_tokens, actor_id) VALUES (:project, 2, 'checkpoint', 1.5, 2, 50, 20, 1000, :actor)"), {"project": admitted[0][0], "actor": uuid4()})
            await db.execute(text("INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, manual_estimate_tokens, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, 1000, :actor)"), {"project": admitted[1][0], "actor": uuid4()})
        query = TaskUsageBaselineQuery(async_sessionmaker(engine, expire_on_commit=False))
        sampled_at = []
        original_samples = query._samples

        async def observe_history_cutoff(*args, **kwargs):
            sampled_at.append(kwargs["as_of"])
            return await original_samples(*args, **kwargs)

        monkeypatch.setattr(query, "_samples", observe_history_cutoff)
        baseline = await query.freeze_baseline(snapshots[0][0])
        assert baseline is not None
        assert baseline.policy_revision == 1 and baseline.reference_tokens == 100
        assert baseline.frozen_at == snapshots[0][1].admitted_at
        assert sampled_at == [snapshots[0][1].admitted_at]
        absent = await query.freeze_baseline(snapshots[1][0])
        assert absent is None
        factory = async_sessionmaker(engine, expire_on_commit=False)
        _, report = await RunTaskUsageControls(
            factory, TaskUsageTotalsQuery(factory)
        ).get_with_report(snapshots[1][0].subject.subject_id)
        assert report is not None and report.policy_revision is None
    finally:
        await engine.dispose()
