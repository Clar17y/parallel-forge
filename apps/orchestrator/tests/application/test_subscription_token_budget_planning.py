"""Planning admission resolves frozen token defaults before durable task creation."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.services.subscription_planning import SubscriptionPlanningService
from forge.domain.run import RunState
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    OperatorProfile,
    RolePreference,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    TokenBudgetDefaults,
)
from forge.domain.subscription_envelope import freeze_profile


@pytest.mark.asyncio
async def test_planning_admits_frozen_primary_default_then_replays_exact_contract():
    run_id, project_id, task_id = uuid4(), uuid4(), uuid4()
    route = RouteSpec(
        provider="openai", client="codex", model="gpt-test",
        auth_mode=AuthMode.SUBSCRIPTION, billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    profile = OperatorProfile(
        profile_id=uuid4(), version=1, preferences=(RolePreference(
            purpose=SpecialistPurpose.PRIMARY, preferred_route=route,
            token_budget=TokenBudgetDefaults(max_input_tokens=20, max_output_tokens=0),
        ),),
    )
    envelope = freeze_profile(profile, run_id=run_id, safety_policy_version=1)
    run = SimpleNamespace(
        id=run_id, project_id=project_id, task_id=task_id, policy_version=1,
        state=RunState.CREATED, version=0, pending_gate=None,
    )
    command = SimpleNamespace(
        id=uuid4(), run_id=run_id, command_type="start_planning",
        idempotency_key=f"{run_id}:start-planning", payload={},
        payload_schema_version=1, expected_run_version=0, actor_id=uuid4(),
    )
    stored = []

    async def create_task(contract, **_kwargs):
        stored.append(contract)

    work = SimpleNamespace(
        commands=SimpleNamespace(
            assert_current_lease=AsyncMock(return_value=command),
            has_pending_current_control_stop=AsyncMock(return_value=False),
        ),
        runs=SimpleNamespace(
            get_for_update=AsyncMock(return_value=run), transition=AsyncMock(),
        ),
        subscription=SimpleNamespace(
            envelope_for_run=AsyncMock(return_value=envelope),
            create_task=create_task,
        ),
        events=SimpleNamespace(list_after=AsyncMock(return_value=[])),
        tasks=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            id=task_id, project_id=project_id, task_digest="a" * 64,
        ))),
        scheduler=SimpleNamespace(admit_run=AsyncMock(), enqueue=AsyncMock()),
        commit=AsyncMock(),
    )
    service = SubscriptionPlanningService(TaskBudget(max_provider_attempts=64))
    admitted_id = await service.execute(command, work)
    assert stored[0].task_id == admitted_id
    assert (stored[0].budget.max_input_tokens, stored[0].budget.max_output_tokens) == (20, 0)
    event = work.runs.transition.call_args.args[4]
    assert event["primary_task"]

    work.events.list_after.return_value = [SimpleNamespace(
        event_type="run.subscription_planning_started", actor_class="worker",
        run_version=1, payload=event,
    )]
    work.subscription.get_task = AsyncMock(return_value=stored[0])
    updated = replace(profile, version=2, preferences=(replace(
        profile.preferences[0], token_budget=TokenBudgetDefaults(max_input_tokens=999)
    ),))
    assert updated.preferences[0].token_budget != envelope.token_budget_for(SpecialistPurpose.PRIMARY)
    assert await SubscriptionPlanningService(
        TaskBudget(max_input_tokens=1), explicit_primary_budget=True
    ).execute(command, work) == admitted_id
    assert len(stored) == 1
