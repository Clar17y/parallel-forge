"""Exercise the exact additive revision against a disposable PostgreSQL database."""

import asyncio
import re
from uuid import uuid4

import pytest
from alembic import command
from forge.persistence.database import create_engine
from forge.persistence.models import Base
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

ROUTE_MEMBERS = '[{"role":"developer","provider":"test","model":"test","ordinal":0}]'


def _constraint(error: IntegrityError) -> str | None:
    diagnostic = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if diagnostic:
        return diagnostic
    match = re.search(r'constraint "([^"]+)"', str(error.orig))
    return match.group(1) if match else None


async def _legacy_usage(url: str):
    engine = create_engine(url)
    project, task, run, execution, usage = (uuid4() for _ in range(5))
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
                    "INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'old task', :digest)"
                ),
                {"task": task, "project": project, "digest": "b" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, 'CREATED', 0, 0, 0, 0, 0, 0, 'DISABLED')"
                ),
                {"run": run, "project": project, "task": task},
            )
            await db.execute(
                text(
                    "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:id, :run, 'developer', 'v1', 'test', 'test', 'SUCCEEDED')"
                ),
                {"id": execution, "run": run},
            )
            await db.execute(
                text(
                    "INSERT INTO model_usage (id, run_id, agent_execution_id, provider, model, prompt_version, input_tokens, output_tokens, cached_input_tokens, duration_ms, tool_call_count, pricing_version, estimated_cost_minor, currency) VALUES (:id, :run, :execution, 'test', 'test', 'v1', 17, 3, 2, 100, 0, 'v1', 1, 'USD')"
                ),
                {"id": usage, "run": run, "execution": execution},
            )
    finally:
        await engine.dispose()
    return usage


async def _seed_and_check(url: str):
    engine = create_engine(url)
    a, b, task, run, execution, unit = (uuid4() for _ in range(6))
    try:
        async with engine.begin() as db:
            for name in (
                "task_usage_work_units",
                "task_usage_observations",
                "task_usage_policy_revisions",
                "task_usage_baselines",
                "task_usage_checkpoints",
                "task_usage_owner_commands",
            ):
                actual = set(
                    (
                        await db.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns WHERE table_name=:name"
                            ),
                            {"name": name},
                        )
                    ).scalars()
                )
                assert actual == set(Base.metadata.tables[name].columns.keys())
            for project in (a, b):
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
                {"id": a, "digest": "a" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'task', :digest)"
                ),
                {"task": task, "project": a, "digest": "b" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, 'CREATED', 0, 0, 0, 0, 0, 0, 'DISABLED')"
                ),
                {"run": run, "project": a, "task": task},
            )
            await db.execute(
                text(
                    "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'test', 'RUNNING')"
                ),
                {"execution": execution, "run": run},
            )
            insert = text(
                "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'agent_execution', :execution, :execution, 1)"
            )
            params = {
                "unit": unit,
                "project": a,
                "run": run,
                "execution": execution,
                "digest": "c" * 64,
                "members": ROUTE_MEMBERS,
            }
            await db.execute(insert, params)
            for column in ("run_id", "agent_execution_id"):
                fresh_execution = uuid4()
                await db.execute(
                    text(
                        "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'test', 'RUNNING')"
                    ),
                    {"execution": fresh_execution, "run": run},
                )
                with pytest.raises(IntegrityError) as rejected:
                    async with db.begin_nested():
                        await db.execute(
                            text(
                                "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, "
                                + ("NULL" if column == "run_id" else ":run")
                                + ", 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'agent_execution', :execution, "
                                + ("NULL" if column == "agent_execution_id" else ":execution")
                                + ", 1)"
                            ),
                            {**params, "unit": uuid4(), "execution": fresh_execution},
                        )
                assert _constraint(rejected.value) == "ck_task_usage_work_units_source_shape"
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(insert, {**params, "unit": uuid4(), "members": "[]"})
            for bad in ({**params, "unit": uuid4(), "project": b}, {**params, "unit": uuid4()}):
                with pytest.raises(IntegrityError):
                    async with db.begin_nested():
                        await db.execute(insert, bad)
            other_run = uuid4()
            await db.execute(
                text(
                    "INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, 'CREATED', 0, 0, 0, 0, 0, 0, 'DISABLED')"
                ),
                {"run": other_run, "project": a, "task": task},
            )
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(insert, {**params, "unit": uuid4(), "run": other_run})
            subscription_task, subscription_attempt = uuid4(), uuid4()
            await db.execute(
                text(
                    "INSERT INTO subscription_tasks (id, run_id, task_id, state, pause_requested, cancel_requested, version, idempotency_key, payload) VALUES (:id, :run, :task, 'queued', false, false, 0, 'task-key', '{}'::jsonb)"
                ),
                {"id": subscription_task, "run": run, "task": uuid4()},
            )
            await db.execute(
                text(
                    "INSERT INTO subscription_attempts (id, run_id, task_row_id, attempt_number, status, idempotency_key, route_payload) VALUES (:id, :run, :task, 1, 'queued', 'attempt-key', '{}'::jsonb)"
                ),
                {"id": subscription_attempt, "run": run, "task": subscription_task},
            )
            subscription_insert = text(
                "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, subscription_attempt_id, subscription_task_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'subscription_attempt', :attempt, :attempt, :task, 1)"
            )
            subscription_params = {
                "unit": uuid4(),
                "project": a,
                "run": run,
                "attempt": subscription_attempt,
                "task": subscription_task,
                "digest": "c" * 64,
                "members": ROUTE_MEMBERS,
            }
            await db.execute(subscription_insert, subscription_params)
            fresh_subscription_attempt = uuid4()
            await db.execute(
                text(
                    "INSERT INTO subscription_attempts (id, run_id, task_row_id, attempt_number, status, idempotency_key, route_payload) VALUES (:id, :run, :task, 2, 'queued', 'attempt-key-2', '{}'::jsonb)"
                ),
                {"id": fresh_subscription_attempt, "run": run, "task": subscription_task},
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, subscription_attempt_id, subscription_task_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'subscription_attempt', :attempt, NULL, :task, 1)"
                        ),
                        {
                            **subscription_params,
                            "unit": uuid4(),
                            "attempt": fresh_subscription_attempt,
                        },
                    )
            assert _constraint(rejected.value) == "ck_task_usage_work_units_source_shape"
            fresh_subscription_attempt = uuid4()
            await db.execute(
                text(
                    "INSERT INTO subscription_attempts (id, run_id, task_row_id, attempt_number, status, idempotency_key, route_payload) VALUES (:id, :run, :task, 3, 'queued', 'attempt-key-3', '{}'::jsonb)"
                ),
                {"id": fresh_subscription_attempt, "run": run, "task": subscription_task},
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, subscription_attempt_id, subscription_task_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'subscription_attempt', :attempt, :attempt, NULL, 1)"
                        ),
                        {
                            **subscription_params,
                            "unit": uuid4(),
                            "attempt": fresh_subscription_attempt,
                        },
                    )
            assert _constraint(rejected.value) == "ck_task_usage_work_units_source_shape"
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        subscription_insert,
                        {**subscription_params, "unit": uuid4(), "run": other_run},
                    )
            observation = text(
                "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, observed_at, schema_version) VALUES (:id, :project, :unit, :event, :sequence, 'cumulative', 'source', 0, false, now(), 1)"
            )
            await db.execute(
                observation,
                {"id": uuid4(), "project": a, "unit": unit, "event": "event-1", "sequence": 1},
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        observation,
                        {
                            "id": uuid4(),
                            "project": b,
                            "unit": unit,
                            "event": "foreign-project",
                            "sequence": 99,
                        },
                    )
            assert (
                _constraint(rejected.value)
                == "fk_task_usage_observations_work_unit_id_task_usage_work_units"
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        observation,
                        {
                            "id": uuid4(),
                            "project": a,
                            "unit": unit,
                            "event": "event-1",
                            "sequence": 1,
                        },
                    )
            assert _constraint(rejected.value) in {
                "uq_task_usage_observations_event",
                "uq_task_usage_observations_sequence",
            }
            await db.execute(
                observation,
                {
                    "id": uuid4(),
                    "project": a,
                    "unit": subscription_params["unit"],
                    "event": "child-event",
                    "sequence": 1,
                },
            )
            final = text(
                "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, final_status, reconciles_event_id, observed_at, schema_version) VALUES (:id, :project, :unit, 'final-2', 2, 'cumulative', 'source', 10, true, 'succeeded', :prior, now(), 1)"
            )
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        final, {"id": uuid4(), "project": a, "unit": unit, "prior": "unknown"}
                    )
            await db.execute(final, {"id": uuid4(), "project": a, "unit": unit, "prior": "event-1"})
            assert (
                await db.scalar(
                    text("SELECT input_tokens FROM task_usage_observations WHERE work_unit_id=:id"),
                    {"id": unit},
                )
                == 0
            )
            epic, conversation, job, attempt, authoring_unit = (uuid4() for _ in range(5))
            await db.execute(
                text(
                    "INSERT INTO epics (id, project_id, title, draft_schema_version, draft) VALUES (:epic, :project, 'Draft', 1, '{}'::jsonb)"
                ),
                {"epic": epic, "project": a},
            )
            await db.execute(
                text(
                    "INSERT INTO epic_brainstorm_conversations (id, epic_id, project_id, version) VALUES (:conversation, :epic, :project, 1)"
                ),
                {"conversation": conversation, "epic": epic, "project": a},
            )
            await db.execute(
                text(
                    "INSERT INTO epic_brainstorm_jobs (id, epic_id, project_id, conversation_id, version, state, snapshot, override_unknown_usage) VALUES (:job, :epic, :project, :conversation, 1, 'queued', '{}'::jsonb, false)"
                ),
                {"job": job, "epic": epic, "project": a, "conversation": conversation},
            )
            await db.execute(
                text(
                    "INSERT INTO epic_brainstorm_attempts (id, job_id, number, state, owner, fence, lease_expires_at, launch_intent, process_started, process_settled, reservation, tool_calls_used) VALUES (:attempt, :job, 1, 'running', 'test', :fence, now(), false, false, false, '{}'::jsonb, 0)"
                ),
                {"attempt": attempt, "job": job, "fence": uuid4()},
            )
            authoring_insert = text(
                "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, authoring_job_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, authoring_attempt_id, schema_version) VALUES (:unit, :project, 'authoring_job', :job, :job, 'brainstorm', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'authoring_attempt', :attempt, :attempt, 1)"
            )
            authoring_params = {
                "unit": authoring_unit,
                "project": a,
                "job": job,
                "attempt": attempt,
                "digest": "d" * 64,
                "members": ROUTE_MEMBERS,
            }
            await db.execute(authoring_insert, authoring_params)
            fresh_authoring_attempt = uuid4()
            await db.execute(
                text(
                    "INSERT INTO epic_brainstorm_attempts (id, job_id, number, state, owner, fence, lease_expires_at, launch_intent, process_started, process_settled, reservation, tool_calls_used) VALUES (:attempt, :job, 2, 'running', 'test', :fence, now(), false, false, false, '{}'::jsonb, 0)"
                ),
                {"attempt": fresh_authoring_attempt, "job": job, "fence": uuid4()},
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, authoring_job_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, authoring_attempt_id, schema_version) VALUES (:unit, :project, 'authoring_job', :job, :job, 'brainstorm', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'authoring_attempt', :attempt, NULL, 1)"
                        ),
                        {**authoring_params, "unit": uuid4(), "attempt": fresh_authoring_attempt},
                    )
            assert _constraint(rejected.value) == "ck_task_usage_work_units_source_shape"
            fresh_authoring_attempt = uuid4()
            await db.execute(
                text(
                    "INSERT INTO epic_brainstorm_attempts (id, job_id, number, state, owner, fence, lease_expires_at, launch_intent, process_started, process_settled, reservation, tool_calls_used) VALUES (:attempt, :job, 3, 'running', 'test', :fence, now(), false, false, false, '{}'::jsonb, 0)"
                ),
                {"attempt": fresh_authoring_attempt, "job": job, "fence": uuid4()},
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, authoring_job_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, authoring_attempt_id, schema_version) VALUES (:unit, :project, 'authoring_job', :job, NULL, 'brainstorm', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'authoring_attempt', :attempt, :attempt, 1)"
                        ),
                        {**authoring_params, "unit": uuid4(), "attempt": fresh_authoring_attempt},
                    )
            assert _constraint(rejected.value) == "ck_task_usage_work_units_source_shape"
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        authoring_insert, {**authoring_params, "unit": uuid4(), "project": b}
                    )
            other_job = uuid4()
            await db.execute(
                text(
                    "INSERT INTO epic_brainstorm_jobs (id, epic_id, project_id, conversation_id, version, state, snapshot, override_unknown_usage) VALUES (:job, :epic, :project, :conversation, 1, 'queued', '{}'::jsonb, false)"
                ),
                {"job": other_job, "epic": epic, "project": a, "conversation": conversation},
            )
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        authoring_insert, {**authoring_params, "unit": uuid4(), "job": other_job}
                    )
            await db.execute(
                text(
                    "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'report_only', 1.5, 2, 50, 20, :actor)"
                ),
                {"project": a, "actor": uuid4()},
            )
            baseline_insert = text(
                "INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :digest, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'insufficient_history', '{}'::jsonb)"
            )
            baseline_params = {
                "unit": unit,
                "project": a,
                "run": run,
                "digest": "e" * 64,
                "snapshot": "a" * 64,
                "cohort": "c" * 64,
            }
            await db.execute(baseline_insert, baseline_params)
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        baseline_insert,
                        {
                            **baseline_params,
                            "unit": subscription_params["unit"],
                            "digest": "f" * 64,
                        },
                    )
            assert _constraint(rejected.value) == "uq_task_usage_baselines_subject"
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        baseline_insert,
                        {
                            **baseline_params,
                            "unit": subscription_params["unit"],
                            "run": other_run,
                            "digest": "f" * 64,
                        },
                    )
            assert (
                _constraint(rejected.value)
                == "fk_task_usage_baselines_work_unit_id_task_usage_work_units_subj"
            )
            policy_insert = text(
                "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, hard_token_cap, actor_id) VALUES (:project, :revision, 'warn', 1.5, 2, 50, 20, :cap, :actor)"
            )
            await db.execute(
                policy_insert, {"project": a, "revision": 2, "cap": None, "actor": uuid4()}
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT policy_revision FROM task_usage_baselines WHERE work_unit_id=:unit"
                    ),
                    {"unit": unit},
                )
                == 1
            )
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        policy_insert, {"project": a, "revision": 3, "cap": 0, "actor": uuid4()}
                    )
            with pytest.raises(DBAPIError, match="task usage record is immutable"):
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "UPDATE task_usage_baselines SET history_digest=:digest WHERE work_unit_id=:unit"
                        ),
                        {"digest": "f" * 64, "unit": unit},
                    )
            checkpoint = uuid4()
            checkpoint_insert = text(
                "INSERT INTO task_usage_checkpoints (id, project_id, work_unit_id, baseline_work_unit_id, subject_kind, subject_id, policy_revision, reference_digest, trigger_event_id, state, version, process_settled) VALUES (:id, :project, :unit, :baseline, 'run', :run, 1, :snapshot, :event, 'checkpoint_requested', 0, false)"
            ).bindparams(snapshot="a" * 64)
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        checkpoint_insert,
                        {
                            "id": uuid4(),
                            "project": a,
                            "unit": unit,
                            "baseline": unit,
                            "run": run,
                            "event": "unknown",
                        },
                    )
            await db.execute(
                checkpoint_insert,
                {
                    "id": checkpoint,
                    "project": a,
                    "unit": unit,
                    "baseline": unit,
                    "run": run,
                    "event": "event-1",
                },
            )
            child_checkpoint = uuid4()
            await db.execute(
                checkpoint_insert,
                {
                    "id": child_checkpoint,
                    "project": a,
                    "unit": subscription_params["unit"],
                    "baseline": unit,
                    "run": run,
                    "event": "child-event",
                },
            )
            await db.execute(
                observation,
                {
                    "id": uuid4(),
                    "project": a,
                    "unit": subscription_params["unit"],
                    "event": "child-project-test",
                    "sequence": 2,
                },
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        checkpoint_insert,
                        {
                            "id": uuid4(),
                            "project": a,
                            "unit": subscription_params["unit"],
                            "baseline": unit,
                            "run": run,
                            "event": "child-project-test",
                            "snapshot": "f" * 64,
                        },
                    )
            assert (
                _constraint(rejected.value)
                == "fk_task_usage_checkpoints_baseline_work_unit_id_baselines"
            )
            other_execution, other_unit = uuid4(), uuid4()
            await db.execute(
                text(
                    "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'test', 'RUNNING')"
                ),
                {"execution": other_execution, "run": other_run},
            )
            await db.execute(
                insert,
                {**params, "unit": other_unit, "run": other_run, "execution": other_execution},
            )
            await db.execute(
                observation,
                {
                    "id": uuid4(),
                    "project": a,
                    "unit": other_unit,
                    "event": "other-event",
                    "sequence": 1,
                },
            )
            ready_insert = text(
                "INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, window_started_at, window_ended_at, frozen_at, schema_version, sample_count, status, reference_tokens, median_tokens, p90_tokens, mean_tokens, warning_threshold_tokens, checkpoint_threshold_tokens, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :digest, :snapshot, 'median_p90_v1', 50, 20, :cohort, now() - interval '7 days', now(), now(), 1, 20, 'ready', 21.5, 10.5, 21.5, 12.25, :warning, 43.0, '{}'::jsonb)"
            )
            ready_params = {
                **baseline_params,
                "unit": other_unit,
                "run": other_run,
                "digest": "d" * 64,
                "snapshot": "b" * 64,
                "warning": 32.25,
            }
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(ready_insert, {**ready_params, "warning": -1.0})
            assert _constraint(rejected.value) == "ck_task_usage_baselines_threshold_shape"
            await db.execute(ready_insert, ready_params)
            assert (
                await db.scalar(
                    text("SELECT median_tokens FROM task_usage_baselines WHERE work_unit_id=:unit"),
                    {"unit": other_unit},
                )
                == 10.5
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        checkpoint_insert,
                        {
                            "id": uuid4(),
                            "project": a,
                            "unit": other_unit,
                            "baseline": unit,
                            "run": other_run,
                            "event": "other-event",
                        },
                    )
            assert (
                _constraint(rejected.value)
                == "fk_task_usage_checkpoints_baseline_work_unit_id_baselines"
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        checkpoint_insert,
                        {
                            "id": uuid4(),
                            "project": a,
                            "unit": subscription_params["unit"],
                            "baseline": unit,
                            "run": run,
                            "event": "event-1",
                        },
                    )
            assert (
                _constraint(rejected.value)
                == "fk_task_usage_checkpoints_work_unit_id_task_usage_observations"
            )
            with pytest.raises(IntegrityError) as rejected:
                async with db.begin_nested():
                    await db.execute(
                        checkpoint_insert,
                        {
                            "id": uuid4(),
                            "project": b,
                            "unit": subscription_params["unit"],
                            "baseline": unit,
                            "run": run,
                            "event": "child-project-test",
                        },
                    )
            assert _constraint(rejected.value) in {
                "fk_task_usage_checkpoints_work_unit_id_task_usage_work_units",
                "fk_task_usage_checkpoints_baseline_work_unit_id_baselines",
            }
            with pytest.raises(DBAPIError, match="checkpoint identity is immutable"):
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "UPDATE task_usage_checkpoints SET trigger_event_id='final-2' WHERE id=:id"
                        ),
                        {"id": checkpoint},
                    )
            with pytest.raises(DBAPIError, match="checkpoint identity is immutable"):
                async with db.begin_nested():
                    await db.execute(
                        text("DELETE FROM task_usage_checkpoints WHERE id=:id"), {"id": checkpoint}
                    )
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        text("UPDATE task_usage_checkpoints SET state='paused' WHERE id=:id"),
                        {"id": checkpoint},
                    )
            await db.execute(
                text("UPDATE task_usage_checkpoints SET state='pausing', version=1 WHERE id=:id"),
                {"id": checkpoint},
            )
            command = text(
                "INSERT INTO task_usage_owner_commands (id, project_id, checkpoint_id, idempotency_key, action, expected_version, result_version, actor_id, state, command_payload, warnings, affected_snapshot_digest) VALUES (:id, :project, :checkpoint, 'owner-key', 'override_checkpoint', 0, 1, :actor, 'resolved', '{}'::jsonb, '[]'::jsonb, :snapshot)"
            )
            receipt = {
                "id": uuid4(),
                "project": a,
                "checkpoint": checkpoint,
                "actor": uuid4(),
                "snapshot": "a" * 64,
            }
            await db.execute(command, receipt)
            for bad in ({**receipt, "id": uuid4()}, {**receipt, "id": uuid4(), "project": b}):
                with pytest.raises(IntegrityError):
                    async with db.begin_nested():
                        await db.execute(command, bad)
        return run
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_additive_migration_and_lineage(test_database_url, alembic_config_factory) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "20261007_0037")
    legacy_usage = asyncio.run(_legacy_usage(test_database_url))
    command.upgrade(config, "head")
    run_id = asyncio.run(_seed_and_check(test_database_url))
    with pytest.raises(RuntimeError, match="durable task usage evidence must not be discarded"):
        command.downgrade(config, "20261007_0037")

    async def retained() -> None:
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as db:
                assert (
                    await db.scalar(text("SELECT version_num FROM alembic_version"))
                    == "20261010_0038"
                )
                for table in (
                    "task_usage_work_units",
                    "task_usage_observations",
                    "task_usage_policy_revisions",
                    "task_usage_baselines",
                    "task_usage_checkpoints",
                    "task_usage_owner_commands",
                ):
                    assert await db.scalar(text(f"SELECT count(*) FROM {table}")) > 0
                assert (
                    await db.scalar(text("SELECT count(*) FROM runs WHERE id=:id"), {"id": run_id})
                    == 1
                )
                assert (
                    await db.scalar(
                        text("SELECT input_tokens FROM model_usage WHERE id=:id"),
                        {"id": legacy_usage},
                    )
                    == 17
                )
        finally:
            await engine.dispose()

    asyncio.run(retained())


_USAGE_TABLES = (
    "task_usage_work_units",
    "task_usage_policy_revisions",
    "task_usage_observations",
    "task_usage_baselines",
    "task_usage_checkpoints",
    "task_usage_owner_commands",
)


@pytest.mark.integration
def test_empty_usage_downgrade_reupgrade_preserves_legacy_history(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "20261007_0037")
    legacy_usage = asyncio.run(_legacy_usage(test_database_url))
    command.upgrade(config, "head")
    command.downgrade(config, "20261007_0037")

    async def check(revision: str) -> None:
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as db:
                assert await db.scalar(text("SELECT version_num FROM alembic_version")) == revision
                assert (
                    await db.scalar(
                        text("SELECT input_tokens FROM model_usage WHERE id=:id"),
                        {"id": legacy_usage},
                    )
                    == 17
                )
                assert await db.scalar(text("SELECT count(*) FROM runs")) == 1
                assert (
                    await db.scalar(text("SELECT to_regclass('task_usage_work_units')")) is None
                ) == (revision == "20261007_0037")
        finally:
            await engine.dispose()

    asyncio.run(check("20261007_0037"))
    command.upgrade(config, "head")
    asyncio.run(check("20261010_0038"))


async def _seed_retention_shape(url: str, target: str):
    """Seed one independently populated shape, or the dependencies of a child row."""
    engine = create_engine(url)
    project, task, run, execution, unit, observation, checkpoint = (uuid4() for _ in range(7))
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO projects (id, canonical_path, github_repository, default_branch) VALUES (:id, :path, :repo, 'main')"
                ),
                {"id": project, "path": f"/tmp/{project}", "repo": f"owner/{project}"},
            )
            if target == "task_usage_policy_revisions":
                await db.execute(
                    text(
                        "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'report_only', 1.5, 2, 50, 20, :actor)"
                    ),
                    {"project": project, "actor": uuid4()},
                )
                return
            await db.execute(
                text(
                    "INSERT INTO project_policy_versions (project_id, version, policy_digest, document_schema_version, document) VALUES (:id, 1, :digest, 1, '{}'::jsonb)"
                ),
                {"id": project, "digest": "a" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'retention', :digest)"
                ),
                {"task": task, "project": project, "digest": "b" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, 'CREATED', 0, 0, 0, 0, 0, 0, 'DISABLED')"
                ),
                {"run": run, "project": project, "task": task},
            )
            await db.execute(
                text(
                    "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'test', 'RUNNING')"
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
                    "digest": "c" * 64,
                    "members": ROUTE_MEMBERS,
                },
            )
            if target == "task_usage_work_units":
                return
            if target in {
                "task_usage_baselines",
                "task_usage_checkpoints",
                "task_usage_owner_commands",
            }:
                await db.execute(
                    text(
                        "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'report_only', 1.5, 2, 50, 20, :actor)"
                    ),
                    {"project": project, "actor": uuid4()},
                )
            if target in {
                "task_usage_observations",
                "task_usage_checkpoints",
                "task_usage_owner_commands",
            }:
                await db.execute(
                    text(
                        "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, observed_at, schema_version) VALUES (:id, :project, :unit, 'event', 1, 'cumulative', 'source', 0, false, now(), 1)"
                    ),
                    {"id": observation, "project": project, "unit": unit},
                )
            if target == "task_usage_observations":
                return
            await db.execute(
                text(
                    "INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :history, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'insufficient_history', '{}'::jsonb)"
                ),
                {
                    "unit": unit,
                    "project": project,
                    "run": run,
                    "history": "d" * 64,
                    "snapshot": "e" * 64,
                    "cohort": "f" * 64,
                },
            )
            if target == "task_usage_baselines":
                return
            await db.execute(
                text(
                    "INSERT INTO task_usage_checkpoints (id, project_id, work_unit_id, baseline_work_unit_id, subject_kind, subject_id, policy_revision, reference_digest, trigger_event_id, state, version, process_settled) VALUES (:id, :project, :unit, :unit, 'run', :run, 1, :snapshot, 'event', 'checkpoint_requested', 0, false)"
                ),
                {
                    "id": checkpoint,
                    "project": project,
                    "unit": unit,
                    "run": run,
                    "snapshot": "e" * 64,
                },
            )
            if target == "task_usage_checkpoints":
                return
            await db.execute(
                text(
                    "INSERT INTO task_usage_owner_commands (id, project_id, checkpoint_id, idempotency_key, action, expected_version, result_version, actor_id, state, command_payload, warnings, affected_snapshot_digest) VALUES (:id, :project, :checkpoint, 'retention', 'continue', 0, 1, :actor, 'resumed', '{}'::jsonb, '[]'::jsonb, :snapshot)"
                ),
                {
                    "id": uuid4(),
                    "project": project,
                    "checkpoint": checkpoint,
                    "actor": uuid4(),
                    "snapshot": "e" * 64,
                },
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("table", _USAGE_TABLES)
def test_downgrade_refuses_each_retained_usage_shape(
    test_database_url, alembic_config_factory, table
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "head")
    asyncio.run(_seed_retention_shape(test_database_url, table))
    with pytest.raises(RuntimeError, match="durable task usage evidence must not be discarded"):
        command.downgrade(config, "20261007_0037")

    async def check() -> None:
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as db:
                assert (
                    await db.scalar(text("SELECT version_num FROM alembic_version"))
                    == "20261010_0038"
                )
                assert await db.scalar(text(f"SELECT count(*) FROM {table}")) == 1
                assert (
                    await db.scalar(text("SELECT to_regclass('task_usage_owner_commands')"))
                    == "task_usage_owner_commands"
                )
                assert (
                    await db.scalar(
                        text("SELECT to_regprocedure('task_usage_reject_record_change()')")
                    )
                    == "task_usage_reject_record_change()"
                )
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_downgrade_waits_for_writer_then_refuses_committed_evidence(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_engine(test_database_url)
    project = uuid4()
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO projects (id, canonical_path, github_repository, default_branch) VALUES (:id, :path, :repo, 'main')"
                ),
                {"id": project, "path": f"/tmp/{project}", "repo": f"owner/{project}"},
            )
        async with engine.connect() as writer:
            transaction = await writer.begin()
            downgrade = None
            try:
                await writer.execute(
                    text(
                        "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'report_only', 1.5, 2, 50, 20, :actor)"
                    ),
                    {"project": project, "actor": uuid4()},
                )
                writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
                downgrade = asyncio.create_task(
                    asyncio.to_thread(command.downgrade, config, "20261007_0037")
                )
                async with asyncio.timeout(10):
                    while True:
                        async with engine.connect() as observer:
                            waiting = await observer.scalar(
                                text(
                                    "SELECT c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE NOT l.granted AND :writer_pid = ANY(pg_blocking_pids(l.pid)) LIMIT 1"
                                ),
                                {"writer_pid": writer_pid},
                            )
                        if waiting:
                            assert waiting == "task_usage_policy_revisions", waiting
                            break
                        await asyncio.sleep(0.02)
                await transaction.commit()
                with pytest.raises(
                    RuntimeError, match="durable task usage evidence must not be discarded"
                ):
                    await downgrade
            finally:
                if transaction.is_active:
                    await transaction.rollback()
                if downgrade is not None and not downgrade.done():
                    await asyncio.gather(downgrade, return_exceptions=True)
        async with engine.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version")) == "20261010_0038"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM task_usage_policy_revisions WHERE project_id=:project"
                    ),
                    {"project": project},
                )
                == 1
            )
    finally:
        await engine.dispose()
