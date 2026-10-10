"""Admission freezes the effective monitoring cap across later owner revisions."""

from uuid import uuid4

import pytest
from forge.domain.actor import AgentRole
from forge.domain.task_usage_contract import MonitoringMode, MonitoringPolicy
from forge.persistence.models import AgentExecution
from forge.persistence.repositories.executions import ExecutionConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork


async def test_future_admission_uses_revised_cap_while_prior_execution_stays_frozen(
    session_factory, persisted_run
) -> None:
    project_id, actor_id = persisted_run.project_id, uuid4()
    first = MonitoringPolicy(
        project_id=project_id, mode=MonitoringMode.CHECKPOINT, hard_token_cap=100
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.task_usage_policy.append_policy_revision(
            first, expected_revision=0, actor_id=actor_id
        )
        await work.commit()

    step_id, execution_id = uuid4(), uuid4()
    args = (
        persisted_run.id,
        step_id,
        execution_id,
        "plan",
        1,
        AgentRole.PLANNER,
        "1",
        "test",
        "planner",
    )
    async with PostgresUnitOfWork(session_factory) as work:
        admitted = await work.executions.admit(
            *args, monitoring_policy_revision=1, hard_token_cap=100
        )
        assert admitted.monitoring_policy_revision == 1
        assert admitted.hard_token_cap == 100
        await work.commit()

    cleared = MonitoringPolicy(project_id=project_id, revision=2, mode=MonitoringMode.CHECKPOINT)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.task_usage_policy.append_policy_revision(
            cleared, expected_revision=1, actor_id=actor_id
        )
        await work.commit()

    async with PostgresUnitOfWork(session_factory) as work:
        replay = await work.executions.admit(
            *args, monitoring_policy_revision=1, hard_token_cap=100
        )
        assert not replay.is_new
        assert replay.hard_token_cap == 100
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(*args, monitoring_policy_revision=2)
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(*args, monitoring_policy_revision=1, hard_token_cap=99)
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(*args, monitoring_policy_revision=3)
        stored = await work.session.get(AgentExecution, execution_id)
        assert stored is not None and stored.hard_token_cap == 100

    async with PostgresUnitOfWork(session_factory) as work:
        next_admission = await work.executions.admit(
            persisted_run.id,
            uuid4(),
            uuid4(),
            "review",
            1,
            AgentRole.REVIEWER,
            "1",
            "test",
            "reviewer",
            monitoring_policy_revision=2,
            hard_token_cap=None,
        )
        assert next_admission.monitoring_policy_revision == 2
        assert next_admission.hard_token_cap is None


async def test_monitoring_admission_rejects_foreign_project_revision(
    session_factory, persisted_run
) -> None:
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(
                persisted_run.id,
                uuid4(),
                uuid4(),
                "plan",
                1,
                AgentRole.PLANNER,
                "1",
                "test",
                "planner",
                monitoring_policy_revision=1,
                hard_token_cap=100,
            )
