"""New executions must use the latest monitoring policy at admission."""

import asyncio
from contextlib import suppress
from time import monotonic
from uuid import uuid4

import pytest
from forge.application.services.task_usage_policy import resolve_agent_budget_for_admission
from forge.domain.actor import AgentRole
from forge.domain.policy import AgentModelPolicy
from forge.domain.task_usage_contract import MonitoringPolicy
from forge.persistence.models import AgentExecution, RunEvent, Step
from forge.persistence.repositories.executions import ExecutionConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, select, text


async def _wait_for_blocked(session_factory, backend_pid: int) -> None:
    """Observe an actual PostgreSQL lock wait, with a bounded deadline."""
    deadline = monotonic() + 3
    while monotonic() < deadline:
        async with session_factory() as session:
            blockers = await session.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": backend_pid}
            )
        if blockers:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("expected PostgreSQL project-row lock wait was not observed")


async def _append_policy(work, project_id, revision: int, cap: int | None) -> None:
    await work.task_usage_policy.append_policy_revision(
        MonitoringPolicy(project_id=project_id, revision=revision, hard_token_cap=cap),
        expected_revision=revision - 1,
        actor_id=uuid4(),
    )


async def _admit(work, run_id, step_id, execution_id, *, revision: int | None, cap: int | None):
    return await work.executions.admit(
        run_id,
        step_id,
        execution_id,
        "plan",
        1,
        AgentRole.PLANNER,
        "v1",
        "google",
        "planner",
        monitoring_policy_revision=revision,
        hard_token_cap=cap,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_cap", "new_cap"),
    [(None, 100), (200, 100), (100, None), ("unconfigured", 100)],
)
async def test_new_admission_rejects_budget_resolved_before_owner_revision(
    session_factory, persisted_run, old_cap: int | None | str, new_cap: int | None
) -> None:
    project_id = persisted_run.project_id
    if old_cap != "unconfigured":
        async with PostgresUnitOfWork(session_factory) as work:
            await work.task_usage_policy.append_policy_revision(
                MonitoringPolicy(project_id=project_id, hard_token_cap=old_cap),
                expected_revision=0,
                actor_id=uuid4(),
            )
            await work.commit()

    async with PostgresUnitOfWork(session_factory) as work:
        stale = await resolve_agent_budget_for_admission(
            work, project_id, AgentModelPolicy(model="planner")
        )
        await work.commit()

    next_revision = 1 if old_cap == "unconfigured" else 2
    async with PostgresUnitOfWork(session_factory) as work:
        await work.task_usage_policy.append_policy_revision(
            MonitoringPolicy(project_id=project_id, revision=next_revision, hard_token_cap=new_cap),
            expected_revision=next_revision - 1,
            actor_id=uuid4(),
        )
        await work.commit()

    step_id, execution_id = uuid4(), uuid4()
    with pytest.raises(ExecutionConflict):
        async with PostgresUnitOfWork(session_factory) as work:
            await work.executions.admit(
                persisted_run.id,
                step_id,
                execution_id,
                "plan",
                1,
                AgentRole.PLANNER,
                "v1",
                "google",
                "planner",
                monitoring_policy_revision=stale.monitoring_policy_revision,
                hard_token_cap=stale.max_total_tokens,
            )
            await work.commit()

    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Step).where(Step.id == step_id))
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentExecution)
                .where(AgentExecution.id == execution_id)
            )
            == 0
        )

    async with PostgresUnitOfWork(session_factory) as work:
        fresh = await resolve_agent_budget_for_admission(
            work, project_id, AgentModelPolicy(model="planner")
        )
        admitted = await work.executions.admit(
            persisted_run.id,
            step_id,
            execution_id,
            "plan",
            1,
            AgentRole.PLANNER,
            "v1",
            "google",
            "planner",
            monitoring_policy_revision=fresh.monitoring_policy_revision,
            hard_token_cap=fresh.max_total_tokens,
        )
        await work.commit()
    assert admitted.monitoring_policy_revision == next_revision
    assert admitted.hard_token_cap == new_cap


@pytest.mark.asyncio
async def test_admission_first_freezes_cap_until_owner_revision_commits(
    session_factory, persisted_run
) -> None:
    project_id = persisted_run.project_id
    async with PostgresUnitOfWork(session_factory) as work:
        await _append_policy(work, project_id, 1, 100)
        await work.commit()

    step_id, execution_id = uuid4(), uuid4()
    started = asyncio.Event()
    writer_pid: list[int] = []

    async def revise() -> None:
        async with PostgresUnitOfWork(session_factory) as work:
            pid = await work.session.scalar(text("SELECT pg_backend_pid()"))
            assert type(pid) is int
            writer_pid.append(pid)
            started.set()
            await _append_policy(work, project_id, 2, 50)
            await work.commit()

    async with PostgresUnitOfWork(session_factory) as admission_work:
        admission = await _admit(
            admission_work, persisted_run.id, step_id, execution_id, revision=1, cap=100
        )
        writer = asyncio.create_task(revise())
        try:
            await asyncio.wait_for(started.wait(), 3)
            await _wait_for_blocked(session_factory, writer_pid[0])
            assert not writer.done()
            await admission_work.commit()
            await asyncio.wait_for(writer, 3)
        finally:
            if not writer.done():
                writer.cancel()
                with suppress(asyncio.CancelledError):
                    await writer
    assert admission.is_new and admission.monitoring_policy_revision == 1
    assert admission.hard_token_cap == 100

    async with PostgresUnitOfWork(session_factory) as work:
        replay = await _admit(work, persisted_run.id, step_id, execution_id, revision=1, cap=100)
        assert not replay.is_new and replay.hard_token_cap == 100
        with pytest.raises(ExecutionConflict):
            await _admit(work, persisted_run.id, step_id, execution_id, revision=2, cap=50)
        with pytest.raises(ExecutionConflict):
            await _admit(work, persisted_run.id, step_id, uuid4(), revision=1, cap=100)
    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Step).where(Step.id == step_id))
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentExecution)
                .where(AgentExecution.id == execution_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == persisted_run.id,
                    RunEvent.event_type == "agent_execution.admitted",
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_owner_revision_first_blocks_then_rejects_stale_new_admission(
    session_factory, persisted_run
) -> None:
    project_id = persisted_run.project_id
    async with PostgresUnitOfWork(session_factory) as work:
        await _append_policy(work, project_id, 1, 100)
        await work.commit()

    started = asyncio.Event()
    admission_pid: list[int] = []
    stale_step, stale_execution = uuid4(), uuid4()

    async def stale_admission() -> None:
        async with PostgresUnitOfWork(session_factory) as work:
            pid = await work.session.scalar(text("SELECT pg_backend_pid()"))
            assert type(pid) is int
            admission_pid.append(pid)
            started.set()
            await _admit(work, persisted_run.id, stale_step, stale_execution, revision=1, cap=100)
            await work.commit()

    async with PostgresUnitOfWork(session_factory) as writer_work:
        await _append_policy(writer_work, project_id, 2, 50)
        contender = asyncio.create_task(stale_admission())
        try:
            await asyncio.wait_for(started.wait(), 3)
            await _wait_for_blocked(session_factory, admission_pid[0])
            assert not contender.done()
            await writer_work.commit()
            with pytest.raises(ExecutionConflict):
                await asyncio.wait_for(contender, 3)
        finally:
            if not contender.done():
                contender.cancel()
                with suppress(asyncio.CancelledError):
                    await contender

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Step).where(Step.id == stale_step)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentExecution)
                .where(AgentExecution.id == stale_execution)
            )
            == 0
        )
    async with PostgresUnitOfWork(session_factory) as work:
        fresh = await _admit(work, persisted_run.id, uuid4(), uuid4(), revision=2, cap=50)
        await work.commit()
    assert fresh.is_new and fresh.hard_token_cap == 50


@pytest.mark.asyncio
async def test_cancelled_blocked_admission_releases_run_lock_without_partial_rows(
    session_factory, persisted_run
) -> None:
    project_id = persisted_run.project_id
    async with PostgresUnitOfWork(session_factory) as work:
        await _append_policy(work, project_id, 1, 100)
        await work.commit()

    started = asyncio.Event()
    admission_pid: list[int] = []
    step_id, execution_id = uuid4(), uuid4()

    async def pending_admission() -> None:
        async with PostgresUnitOfWork(session_factory) as work:
            pid = await work.session.scalar(text("SELECT pg_backend_pid()"))
            assert type(pid) is int
            admission_pid.append(pid)
            started.set()
            await _admit(work, persisted_run.id, step_id, execution_id, revision=1, cap=100)
            await work.commit()

    async with PostgresUnitOfWork(session_factory) as writer_work:
        await _append_policy(writer_work, project_id, 2, None)
        contender = asyncio.create_task(pending_admission())
        try:
            await asyncio.wait_for(started.wait(), 3)
            await _wait_for_blocked(session_factory, admission_pid[0])
            contender.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(contender, 3)
            await writer_work.commit()
        finally:
            if not contender.done():
                contender.cancel()
                with suppress(asyncio.CancelledError):
                    await contender

    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Step).where(Step.id == step_id))
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentExecution)
                .where(AgentExecution.id == execution_id)
            )
            == 0
        )
    async with PostgresUnitOfWork(session_factory) as work:
        fresh = await _admit(work, persisted_run.id, step_id, execution_id, revision=2, cap=None)
        await work.commit()
    assert fresh.is_new and fresh.hard_token_cap is None
