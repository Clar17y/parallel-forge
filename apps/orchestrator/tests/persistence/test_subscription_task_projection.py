"""Actual PostgreSQL task inspection, lineage and unknown-usage coverage."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_tasks import SubscriptionAttemptPage, SubscriptionTaskPage
from forge.domain.subscription import (
    AttemptIdentity,
    AttemptTelemetry,
    ExecutionEnvelope,
    LogicalTaskContract,
    OperatorProfile,
    RolePreference,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    encode_subscription_record,
)
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionProfileVersion
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete


@pytest.mark.integration
async def test_projection_uses_scheduler_and_keeps_unknown_usage_and_run_isolation(
    session_factory, persisted_run
):
    query = SubscriptionTaskQuery(session_factory)
    legacy = await query.tasks(persisted_run.id)
    assert legacy["subscription"] is False and legacy["tasks"] == []
    assert await query.tasks(uuid4()) is None
    route = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    binding = RouteBinding(requested=route, effective=route)
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=route),),
    )
    task = LogicalTaskContract(
        run_id=persisted_run.id,
        task_id=uuid4(),
        purpose=SpecialistPurpose.PRIMARY,
        route=binding,
        budget=TaskBudget(),
        owned_paths=("src",),
    )
    other = replace(persisted_run, id=uuid4())
    attempts = [
        AttemptIdentity(
            run_id=task.run_id, task_id=task.task_id, attempt_id=uuid4(), attempt_number=number
        )
        for number in (1, 2)
    ]
    try:
        async with PostgresUnitOfWork(session_factory) as work:
            await work.runs.create(other)
            await work.subscription.store_profile(profile)
            await work.subscription.freeze_envelope(
                ExecutionEnvelope(
                    run_id=task.run_id,
                    profile_id=profile.profile_id,
                    profile_version=1,
                    safety_policy_version=1,
                    routes=((SpecialistPurpose.PRIMARY, binding),),
                )
            )
            await work.subscription.create_task(task, idempotency_key="projection-task")
            for attempt in attempts:
                await work.subscription.create_attempt(
                    attempt,
                    route_payload=binding,
                    idempotency_key=f"projection-attempt-{attempt.attempt_number}",
                )
            await work.commit()
        async with session_factory() as session:
            session.add(
                SubscriptionScheduledTask(
                    id=uuid4(),
                    run_id=task.run_id,
                    task_id=task.task_id,
                    worktree_id="test-worktree",
                    provider="openai",
                    state="leased",
                    owned_paths=["src"],
                    pause_requested=True,
                )
            )
            measured = await session.get(SubscriptionAttempt, attempts[1].attempt_id)
            measured.telemetry_payload = encode_subscription_record(
                AttemptTelemetry(
                    input_tokens=12,
                    output_tokens=4,
                    duration_ms=150,
                    tool_call_count=1,
                )
            )
            await session.commit()
        tasks = SubscriptionTaskPage.model_validate(await query.tasks(task.run_id))
        assert tasks.subscription and len(tasks.tasks) == 1
        assert tasks.tasks[0].state == "leased" and tasks.tasks[0].pause_requested
        assert tasks.tasks[0].owned_paths == ["src"]
        first = SubscriptionAttemptPage.model_validate(
            await query.attempts(task.run_id, task.task_id, limit=1)
        )
        assert first.has_more and first.attempts[0].input_tokens is None
        assert first.attempts[0].duration_ms is None
        assert first.attempts[0].quota_status == "unknown"
        second = SubscriptionAttemptPage.model_validate(
            await query.attempts(task.run_id, task.task_id, offset=1, limit=1)
        )
        assert not second.has_more and second.attempts[0].input_tokens == 12
        assert second.attempts[0].effective_route.model == "gpt-6-astra"
        assert second.attempts[0].estimated_api_cost_minor is None
        assert await query.attempts(other.id, task.task_id) is None
        assert (await query.tasks(other.id))["tasks"] == []
    finally:
        async with session_factory() as session:
            await session.execute(delete(Run).where(Run.id.in_((task.run_id, other.id))))
            await session.execute(
                delete(SubscriptionProfileVersion).where(
                    SubscriptionProfileVersion.profile_id == profile.profile_id
                )
            )
            await session.commit()
