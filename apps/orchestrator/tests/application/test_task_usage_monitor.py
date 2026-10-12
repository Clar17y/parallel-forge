import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.task_usage_controls import RunTaskUsageControls, next_threshold
from forge.application.services.task_usage_monitor import RunTaskUsageMonitor
from forge.domain.task_usage_contract import MonitoringMode, MonitoringPolicy, OwnerCommand
from forge.domain.task_usage_monitor import evaluate_run_usage
from forge.persistence.database import create_engine
from forge.persistence.models.run import Run
from forge.persistence.models.task_usage import TaskUsageObservation, TaskUsageWorkUnit
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.task_usage_checkpoints import CheckpointConflict
from forge.persistence.repositories.task_usage_observations import (
    decode_binding,
    decode_observation,
)
from forge.persistence.repositories.task_usage_policy import (
    MonitoringPolicyConflict,
    PostgresTaskUsagePolicyRepository,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from task_usage_contract.fixtures import examples


def test_lower_bound_crosses_frozen_threshold_but_unknown_never_pauses() -> None:
    values = examples()
    reference = values["ready_baseline"]
    policy = values["checkpoint_policy"]
    threshold = reference.checkpoint_threshold_tokens
    assert (
        evaluate_run_usage(reference, policy, measured_tokens=43, completeness="lower_bound")
        == "checkpoint"
    )
    assert (
        evaluate_run_usage(reference, policy, measured_tokens=None, completeness="unknown")
        == "report_only"
    )
    assert (
        evaluate_run_usage(
            reference,
            replace(policy, mode=MonitoringMode.WARN),
            measured_tokens=43,
            completeness="complete",
        )
        == "warning"
    )
    assert (
        evaluate_run_usage(values["baseline"], policy, measured_tokens=100, completeness="complete")
        == "report_only"
    )
    assert threshold == 43


def test_continuation_uses_measured_total_and_cannot_immediately_repause() -> None:
    reference = examples()["ready_baseline"]
    assert next_threshold(reference, 43) == 65
    assert (
        evaluate_run_usage(
            reference,
            examples()["checkpoint_policy"],
            measured_tokens=64,
            completeness="lower_bound",
            next_checkpoint_threshold_tokens=65,
        )
        == "warning"
    )
    assert (
        evaluate_run_usage(
            reference,
            examples()["checkpoint_policy"],
            measured_tokens=999,
            completeness="complete",
            usage_pauses_disabled=True,
        )
        == "warning"
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "run_state,execution_state,final,terminal_during_pause,owner_action",
    [
        ("IMPLEMENTING", "RUNNING", False, False, "next"),
        ("IMPLEMENTING", "RUNNING", False, False, "bypass_policy"),
        ("IMPLEMENTING", "RUNNING", False, False, "manual_pause"),
        ("IMPLEMENTING", "RUNNING", False, False, "cancel"),
        ("IMPLEMENTING", "RUNNING", False, True, "terminal"),
        ("COMPLETED", "SUCCEEDED", True, False, "terminal"),
    ],
)
async def test_checkpoint_waits_for_root_settlement_and_final_only_crossing_resolves(
    migrated_database_url: str,
    run_state: str,
    execution_state: str,
    final: bool,
    terminal_during_pause: bool,
    owner_action: str,
) -> None:
    engine = create_engine(migrated_database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project, task, run, execution, unit, event, actor = (uuid4() for _ in range(7))
    route = examples()["ready_baseline"].binding.configured_route
    members = '[{"role":"developer","provider":"test","model":"model","ordinal":0,"reasoning_effort":"medium"}]'
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO projects (id, canonical_path, github_repository, default_branch) VALUES (:id, :path, :repo, 'main')"
                ),
                {"id": project, "path": f"/tmp/{project}", "repo": f"owner/{project}"},
            )
            await db.execute(
                text(
                    "INSERT INTO project_policy_versions (project_id, version, policy_digest, document_schema_version, document) VALUES (:id, 1, :digest, 1, '{}'::jsonb)"
                ),
                {"id": project, "digest": "a" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'checkpoint', :digest)"
                ),
                {"task": task, "project": project, "digest": "b" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, :state, 0, 0, 0, 0, 0, 0, 'DISABLED')"
                ),
                {"run": run, "project": project, "task": task, "state": run_state},
            )
            await db.execute(
                text(
                    "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'model', :status)"
                ),
                {"execution": execution, "run": run, "status": execution_state},
            )
            await db.execute(
                text(
                    "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'agent_execution', :execution, :execution, 1)"
                ),
                {
                    "unit": unit,
                    "project": project,
                    "run": run,
                    "execution": execution,
                    "digest": route.fingerprint,
                    "members": members,
                },
            )
            await db.execute(
                text(
                    "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, :actor)"
                ),
                {"project": project, "actor": actor},
            )
            await db.execute(
                text(
                    "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, output_tokens, final, final_status, observed_at, schema_version) VALUES (:id, :project, :unit, 'event', 1, 'cumulative', 'source', 100, 1, :final, :final_status, now(), 1)"
                ),
                {
                    "id": event,
                    "project": project,
                    "unit": unit,
                    "final": final,
                    "final_status": "succeeded" if final else None,
                },
            )
            await db.execute(
                text(
                    "INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, reference_tokens, warning_threshold_tokens, checkpoint_threshold_tokens, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :history, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'manual', 25, 37.5, 50, '{}'::jsonb)"
                ),
                {
                    "unit": unit,
                    "project": project,
                    "run": run,
                    "history": "d" * 64,
                    "snapshot": "e" * 64,
                    "cohort": route.fingerprint,
                },
            )
        async with AsyncSession(engine) as db:
            binding = decode_binding(await db.get(TaskUsageWorkUnit, unit))
            observed = decode_observation(await db.get(TaskUsageObservation, event), binding)
        monitor = RunTaskUsageMonitor(sessions, TaskUsageTotalsQuery(sessions))
        observed_requests = await asyncio.gather(
            monitor.observe(observed), monitor.observe(observed)
        )
        requested = next((item for item in observed_requests if item is not None), None)
        assert requested is not None
        if not final:
            assert all(
                item is not None and item.checkpoint_id == requested.checkpoint_id
                for item in observed_requests
            )
        shown, report = await RunTaskUsageControls(
            sessions, TaskUsageTotalsQuery(sessions)
        ).get_with_report(run)
        assert shown is not None and shown.checkpoint_id == requested.checkpoint_id
        assert report is not None and report.input_tokens == 100
        assert report.checkpoint_version == requested.version
        if final:
            assert requested.state.value == "resolved" and requested.process_settled
            return
        assert requested.state.value == "checkpoint_requested"
        commands = PostgresCommandRepository(sessions)
        await commands.enqueue(
            run_id=run,
            command_type="prepare_worktree",
            idempotency_key=str(uuid4()),
            payload={},
            expected_run_version=0,
        )
        assert await commands.claim_next(worker_id="worker", lease_seconds=30) is None
        assert (await monitor.observe(observed)).checkpoint_id == requested.checkpoint_id
        pausing = await monitor.advance(requested.checkpoint_id)
        assert pausing.state.value == "pausing"
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE agent_executions SET status='SUCCEEDED' WHERE id=:id"),
                {"id": execution},
            )
            if terminal_during_pause:
                await db.execute(
                    text("UPDATE runs SET state='COMPLETED' WHERE id=:id"), {"id": run}
                )
        paused = await monitor.advance(requested.checkpoint_id)
        if terminal_during_pause:
            assert paused.state.value == "resolved" and paused.process_settled
            return
        assert paused.state.value == "paused" and paused.process_settled
        controls = RunTaskUsageControls(sessions, TaskUsageTotalsQuery(sessions))
        owner = AuthenticatedActor(actor_id=actor, actor_class="operator", session_id=uuid4())
        if owner_action in {"manual_pause", "cancel"}:
            async with engine.begin() as db:
                if owner_action == "manual_pause":
                    await db.execute(
                        text(
                            "UPDATE runs SET state='PAUSED', suspended_state='IMPLEMENTING', suspension_kind='PAUSE', suspension_context='{}'::jsonb, suspension_context_schema_version=1 WHERE id=:id"
                        ),
                        {"id": run},
                    )
                else:
                    await db.execute(
                        text("UPDATE runs SET state='CANCELLED' WHERE id=:id"), {"id": run}
                    )
            with pytest.raises(CheckpointConflict, match="independent run control"):
                await controls.apply(
                    run_id=run,
                    actor=owner,
                    command=OwnerCommand(
                        command_id=uuid4(),
                        checkpoint_id=paused.checkpoint_id,
                        expected_version=paused.version,
                        idempotency_key=f"{owner_action}-continue",
                        action="continue",
                        actor_id=actor,
                        continue_mode="next_checkpoint",
                    ),
                )
            assert (await controls.get(run)).state.value == "paused"
            return
        if owner_action == "bypass_policy":
            policy_command = OwnerCommand(
                command_id=uuid4(),
                checkpoint_id=paused.checkpoint_id,
                expected_version=paused.version,
                idempotency_key="policy-2",
                action="set_policy",
                actor_id=actor,
                requested_policy=MonitoringPolicy(
                    project_id=project,
                    revision=2,
                    mode=MonitoringMode.CHECKPOINT,
                    hard_token_cap=1000,
                ),
            )
            _, paused = await controls.apply(run_id=run, actor=owner, command=policy_command)
            with pytest.raises(MonitoringPolicyConflict, match="stale"):
                await controls.apply(
                    run_id=run,
                    actor=owner,
                    command=replace(
                        policy_command,
                        command_id=uuid4(),
                        expected_version=paused.version,
                        idempotency_key="policy-stale",
                    ),
                )
        command = OwnerCommand(
            command_id=uuid4(),
            checkpoint_id=paused.checkpoint_id,
            expected_version=paused.version,
            idempotency_key="continue-next",
            action="continue",
            actor_id=actor,
            continue_mode="without_usage_pauses"
            if owner_action == "bypass_policy"
            else "next_checkpoint",
        )
        with pytest.raises(CheckpointConflict, match="actor differs"):
            await controls.apply(
                run_id=run,
                actor=replace(owner, actor_id=uuid4()),
                command=command,
            )
        receipt, resumed = await controls.apply(run_id=run, actor=owner, command=command)
        assert resumed.state.value == "resumed"
        if owner_action == "bypass_policy":
            assert resumed.usage_pauses_disabled and resumed.monitoring_policy_revision == 2
            assert resumed.next_checkpoint_threshold_tokens is None
            async with sessions() as db:
                run_row = await db.get(Run, run)
                policy = await PostgresTaskUsagePolicyRepository(db).get(project, 2)
            assert run_row is not None and run_row.token_budget == 0
            assert policy is not None and policy.hard_token_cap == 1000
        else:
            assert resumed.next_checkpoint_threshold_tokens == 126
        assert not receipt.replayed
        replay, again = await controls.apply(
            run_id=run, actor=owner, command=replace(command, command_id=uuid4())
        )
        assert replay.replayed and again == resumed
        with pytest.raises(CheckpointConflict):
            await controls.apply(
                run_id=run,
                actor=owner,
                command=replace(
                    command,
                    command_id=uuid4(),
                    continue_mode=(
                        "next_checkpoint"
                        if owner_action == "bypass_policy"
                        else "without_usage_pauses"
                    ),
                ),
            )
        assert await commands.claim_next(worker_id="worker", lease_seconds=30) is not None
        with pytest.raises(CheckpointConflict):
            await controls.apply(
                run_id=run,
                actor=owner,
                command=replace(command, command_id=uuid4(), idempotency_key="stale"),
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("usage_rows", "expected_checkpoint", "expected_tokens"),
    [
        ([(90, 11, 0, True, True, True)], True, (90, 11, 0)),
        ([(0, 0, 0, None, None, None)], False, (None, None, None)),
        ([(0, 0, 0, True, True, True)], False, (0, 0, 0)),
        ([(90, 11, 0, True, False, False)], True, (90, None, None)),
        (
            [(60, 5, 0, True, True, True), (20, 1, 0, False, True, True)],
            True,
            (None, 6, 0),
        ),
    ],
    ids=(
        "complete-crossing",
        "legacy-unknown",
        "reported-zero",
        "partial-crossing",
        "mixed-repairs",
    ),
)
async def test_terminal_root_model_usage_is_reconciled_after_restart(
    migrated_database_url: str,
    usage_rows: list[tuple[int, int, int, bool | None, bool | None, bool | None]],
    expected_checkpoint: bool,
    expected_tokens: tuple[int | None, int | None, int | None],
) -> None:
    engine = create_engine(migrated_database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project, task, run, execution, unit, actor = (uuid4() for _ in range(6))
    route = examples()["ready_baseline"].binding.configured_route
    members = '[{"role":"developer","provider":"test","model":"model","ordinal":0,"reasoning_effort":"medium"}]'
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO projects (id, canonical_path, github_repository, default_branch) VALUES (:id, :path, :repo, 'main')"
                ),
                {"id": project, "path": f"/tmp/{project}", "repo": f"owner/{project}"},
            )
            await db.execute(
                text(
                    "INSERT INTO project_policy_versions (project_id, version, policy_digest, document_schema_version, document) VALUES (:id, 1, :digest, 1, '{}'::jsonb)"
                ),
                {"id": project, "digest": "a" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'checkpoint', :digest)"
                ),
                {"task": task, "project": project, "digest": "b" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, 'COMPLETED', 0, 0, 0, 0, 0, 0, 'DISABLED')"
                ),
                {"run": run, "project": project, "task": task},
            )
            await db.execute(
                text(
                    "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'model', 'SUCCEEDED')"
                ),
                {"execution": execution, "run": run},
            )
            await db.execute(
                text(
                    "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'agent_execution', :execution, :execution, 1)"
                ),
                {
                    "unit": unit,
                    "project": project,
                    "run": run,
                    "execution": execution,
                    "digest": route.fingerprint,
                    "members": members,
                },
            )
            await db.execute(
                text(
                    "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, :actor)"
                ),
                {"project": project, "actor": actor},
            )
            await db.execute(
                text(
                    "INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, reference_tokens, warning_threshold_tokens, checkpoint_threshold_tokens, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :history, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'manual', 25, 37.5, 50, '{}'::jsonb)"
                ),
                {
                    "unit": unit,
                    "project": project,
                    "run": run,
                    "history": "d" * 64,
                    "snapshot": "e" * 64,
                    "cohort": route.fingerprint,
                },
            )
            for (
                input_tokens,
                output_tokens,
                cached_tokens,
                input_complete,
                output_complete,
                cached_complete,
            ) in usage_rows:
                await db.execute(
                    text(
                        "INSERT INTO model_usage (id, run_id, agent_execution_id, provider, model, prompt_version, input_tokens, output_tokens, cached_input_tokens, input_tokens_complete, output_tokens_complete, cached_input_tokens_complete, duration_ms, tool_call_count, pricing_version, estimated_cost_minor, currency) VALUES (:id, :run, :execution, 'test', 'model', 'v1', :input, :output, :cached, :input_complete, :output_complete, :cached_complete, 1, 0, 'v1', 0, 'USD')"
                    ),
                    {
                        "id": uuid4(),
                        "run": run,
                        "execution": execution,
                        "input": input_tokens,
                        "output": output_tokens,
                        "cached": cached_tokens,
                        "input_complete": input_complete,
                        "output_complete": output_complete,
                        "cached_complete": cached_complete,
                    },
                )
        monitor = RunTaskUsageMonitor(sessions, TaskUsageTotalsQuery(sessions))
        results = await monitor.reconcile_final_sources()
        assert len(results) == int(expected_checkpoint)
        if expected_checkpoint:
            assert results[0].state.value == "resolved" and results[0].process_settled
        async with sessions() as db:
            final = await db.scalar(
                select(TaskUsageObservation).where(
                    TaskUsageObservation.work_unit_id == unit, TaskUsageObservation.final.is_(True)
                )
            )
        assert final is not None
        assert (
            final.input_tokens,
            final.output_tokens,
            final.cached_input_tokens,
        ) == expected_tokens
        assert await monitor.reconcile_final_sources() == ()
    finally:
        await engine.dispose()
