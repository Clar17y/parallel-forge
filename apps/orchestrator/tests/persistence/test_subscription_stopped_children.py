"""A paused child retains its contract while independent queued siblings survive."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import TaskBudget
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.repositories.commands import PostgresCommandRepository
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_resume_controls import pause_for_resume
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_stopped_child_resumes_without_reassigning_sibling_or_waking_primary(
    session_factory, tmp_path
):
    def children(child, _):
        budget = replace(
            child.budget,
            max_provider_attempts=3,
            max_repairs=1,
            max_duration_seconds=30,
            max_tool_calls=24,
            max_named_checks=6,
            max_input_tokens=300,
            max_output_tokens=120,
            max_cost_minor=60,
        )
        return tuple(
            replace(
                child,
                task_id=uuid4(),
                owned_paths=(f"apps/child-{i}",),
                max_repairs=1,
                budget=budget,
            )
            for i in range(2)
        )

    factory, parent, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        children,
        primary_budget=TaskBudget(max_provider_attempts=20, max_repairs=10),
    )
    await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    commands = PostgresCommandRepository(session_factory)
    async with factory() as work:
        sources = await work.commands.list_outstanding_normal(
            run_id=parent.task.run_id, exclude_command_id=UUID(int=0)
        )
    assert len(sources) == 1 and sources[0].command_type == "prepare_worktree"
    await commands.complete(sources[0].id, worker_id=sources[0].lease_owner)
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("stopped-child", _reservation())
    assert child is not None
    stopped = await record_stopped_launch(session_factory, child)
    _, resume = await pause_for_resume(factory, session_factory, child.task.run_id)
    assert (
        await executor.settle(
            child,
            SubscriptionInvocationResult(
                attempt=child.attempt,
                failure=SubscriptionFailure.INTERRUPTED,
                telemetry=_known(),
                launch_proof=stopped,
            ),
        )
    ).disposition == "stale"
    for _ in range(2):
        async with factory() as work:
            await ResumeRunHandler()(resume, work)
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
        for contract in children:
            assert await work.subscription.get_task(child.task.run_id, contract.task_id) == contract
            scheduled = await work.session.get(SubscriptionScheduledTask, contract.task_id)
            assert scheduled.state == "queued"
            assert scheduled.repairs == int(contract.task_id == child.task.task_id)
    resumed = [await executor.admit_next(f"child-{i}", _reservation()) for i in range(2)]
    assert all(item is not None for item in resumed)
    assert {item.task.task_id for item in resumed} == {contract.task_id for contract in children}
