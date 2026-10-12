"""Checkpoint races retain an already completed provider result."""

from datetime import timedelta
from uuid import uuid4

import pytest
from forge.application.ports.scheduling import SchedulingConflict
from forge.domain.scheduling import ScheduleTask
from forge.domain.subscription import (
    AttemptIdentity,
    AuthMode,
    BillingMode,
    ExecutionEnvelope,
    LogicalTaskContract,
    OperatorProfile,
    ReasoningEffort,
    RolePreference,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
)
from forge.persistence.models.run import Run
from forge.persistence.repositories.scheduling import UsageCheckpointLeaseRevoked
from forge.persistence.repositories.subscription import SubscriptionConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.subscription_runtime import SubscriptionAttemptRunner
from sqlalchemy import text
from test_subscription_attempt_runner import WorkFactory, invocation, successful


def _route(provider: str) -> RouteSpec:
    return RouteSpec(
        provider=provider,
        client=f"{provider}-client",
        model=f"{provider}-model",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


async def _admit_run(work, run_id, routes: tuple[RouteSpec, RouteSpec]):
    purposes = (SpecialistPurpose.PRIMARY, SpecialistPurpose.ROUTINE_IMPLEMENTATION)
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=tuple(
            RolePreference(purpose=purpose, preferred_route=route)
            for purpose, route in zip(purposes, routes, strict=True)
        ),
    )
    await work.subscription.store_profile(profile)
    await work.subscription.freeze_envelope(
        ExecutionEnvelope(
            run_id=run_id,
            profile_id=profile.profile_id,
            profile_version=1,
            safety_policy_version=1,
            routes=tuple(
                (
                    purpose,
                    RouteBinding(
                        requested=route,
                        effective=route,
                        is_primary=purpose is SpecialistPurpose.PRIMARY,
                    ),
                )
                for purpose, route in zip(purposes, routes, strict=True)
            ),
        )
    )
    primary_id = uuid4()
    await work.subscription.create_task(
        LogicalTaskContract(
            run_id=run_id,
            task_id=primary_id,
            purpose=SpecialistPurpose.PRIMARY,
            route=RouteBinding(requested=routes[0], effective=routes[0], is_primary=True),
            budget=TaskBudget(),
            owned_paths=("apps",),
        ),
        idempotency_key=f"primary-{primary_id}",
    )
    await work.scheduler.admit_run(run_id)
    return primary_id


async def test_completed_provider_result_survives_checkpoint_renewal_fence() -> None:
    admission, request = invocation()

    class CheckpointFactory(WorkFactory):
        async def renew(self, lease, duration):
            self.renewals += 1
            if self.renewals == 2:
                raise UsageCheckpointLeaseRevoked("checkpoint requested")
            return lease

    factory = CheckpointFactory()

    class Gateway:
        async def execute(self, value):
            return successful(value)

    async def revoke() -> None:
        pass

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request, Gateway(), revoke
    )
    assert factory.renewals == 2
    assert outcome.result.failure is None
    assert outcome.result.decision is not None
    assert factory.results == [outcome.result]


@pytest.mark.integration
async def test_preleased_child_cannot_launch_or_admit_effect_after_checkpoint(
    session_factory,
    persisted_run,
) -> None:
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(Run, persisted_run.id)
        row.state = "IMPLEMENTING"
        primary = await _admit_run(work, persisted_run.id, (_route("p"), _route("p")))
        await work.scheduler.enqueue(
            ScheduleTask(
                run_id=persisted_run.id,
                task_id=primary,
                worktree_id="checkpoint-tree",
                owned_paths=("apps",),
                max_repairs=3,
            )
        )
        lease = await work.scheduler.claim_ready("worker", timedelta(seconds=30))
        assert lease is not None
        await work.commit()
    project = persisted_run.project_id
    execution, unit, observation, actor, checkpoint = (uuid4() for _ in range(5))
    from forge.domain.task_usage_contract import RouteCohort, RouteMember

    cohort = RouteCohort.from_members(
        (
            RouteMember(
                role="developer",
                provider="test",
                model="test",
                ordinal=0,
            ),
        )
    ).fingerprint
    members = '[{"role":"developer","provider":"test","model":"test","ordinal":0}]'
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'test', 'test', 'SUCCEEDED')"
            ),
            {"execution": execution, "run": persisted_run.id},
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'agent_execution', :execution, :execution, 1)"
            ),
            {
                "unit": unit,
                "project": project,
                "run": persisted_run.id,
                "execution": execution,
                "digest": cohort,
                "members": members,
            },
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, :actor) ON CONFLICT DO NOTHING"
            ),
            {"project": project, "actor": actor},
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, observed_at, schema_version) VALUES (:id, :project, :unit, 'event', 1, 'cumulative', 'source', 100, false, now(), 1)"
            ),
            {"id": observation, "project": project, "unit": unit},
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, reference_tokens, warning_threshold_tokens, checkpoint_threshold_tokens, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :history, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'manual', 25, 37.5, 50, '{}'::jsonb)"
            ),
            {
                "unit": unit,
                "project": project,
                "run": persisted_run.id,
                "history": "d" * 64,
                "snapshot": "e" * 64,
                "cohort": cohort,
            },
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_checkpoints (id, project_id, work_unit_id, baseline_work_unit_id, subject_kind, subject_id, policy_revision, reference_digest, trigger_event_id, state, version, generation, process_settled) VALUES (:checkpoint, :project, :unit, :unit, 'run', :run, 1, :digest, 'event', 'checkpoint_requested', 0, 1, false)"
            ),
            {
                "checkpoint": checkpoint,
                "project": project,
                "unit": unit,
                "run": persisted_run.id,
                "digest": "e" * 64,
            },
        )
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionConflict):
            await work.subscription.create_task(
                LogicalTaskContract(
                    run_id=persisted_run.id,
                    task_id=uuid4(),
                    purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                    route=RouteBinding(requested=_route("p"), effective=_route("p")),
                    budget=TaskBudget(),
                    parent_task_id=primary,
                    owned_paths=("apps",),
                ),
                idempotency_key=str(uuid4()),
            )
        with pytest.raises(SubscriptionConflict):
            await work.subscription.create_attempt(
                AttemptIdentity(run_id=persisted_run.id, task_id=primary, attempt_id=uuid4()),
                route_payload=RouteBinding(
                    requested=_route("p"),
                    effective=_route("p"),
                    is_primary=True,
                ),
                idempotency_key=str(uuid4()),
            )
        with pytest.raises(UsageCheckpointLeaseRevoked):
            await work.scheduler.renew(lease, timedelta(seconds=30))
        with pytest.raises(SchedulingConflict):
            await work.scheduler.admit_effect(lease, uuid4(), owned_paths=("apps",))
        with pytest.raises(SubscriptionConflict):
            await work.subscription_execution.admit(lease, uuid4())
