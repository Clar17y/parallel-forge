"""Historical prompt identity is immutable across execution admission replay."""

from uuid import uuid4

import pytest
from forge.domain.actor import AgentRole
from forge.persistence.models import AgentExecution
from forge.persistence.repositories.executions import ExecutionConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError


async def test_instruction_digest_survives_reload_and_rejects_replay_drift(
    session_factory, persisted_run
):
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
        admitted = await work.executions.admit(*args, instruction_digest="a" * 64)
        assert admitted.instruction_digest == "a" * 64
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        stored = await work.session.get(AgentExecution, execution_id)
        assert stored.instruction_digest == "a" * 64
        replay = await work.executions.admit(*args, instruction_digest="a" * 64)
        assert not replay.is_new
        assert replay.instruction_digest == "a" * 64
        for changed in (None, "b" * 64):
            with pytest.raises(ExecutionConflict):
                await work.executions.admit(*args, instruction_digest=changed)


async def test_legacy_admission_does_not_invent_prompt_identity(session_factory, persisted_run):
    args = (
        persisted_run.id,
        uuid4(),
        uuid4(),
        "plan",
        1,
        AgentRole.PLANNER,
        "1",
        "test",
        "planner",
    )
    async with PostgresUnitOfWork(session_factory) as work:
        admitted = await work.executions.admit(*args)
        assert admitted.instruction_digest is None
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(*args, instruction_digest="a" * 64)


async def test_database_rejects_malformed_digest_outside_repository(session_factory, persisted_run):
    execution_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.executions.admit(
            persisted_run.id,
            uuid4(),
            execution_id,
            "plan",
            1,
            AgentRole.PLANNER,
            "1",
            "test",
            "planner",
        )
        await work.commit()
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match="ck_agent_executions_instruction_digest"):
            await session.execute(
                update(AgentExecution)
                .where(AgentExecution.id == execution_id)
                .values(instruction_digest="invalid")
            )


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64])
async def test_invalid_instruction_digest_is_rejected(session_factory, persisted_run, digest):
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(ValueError, match="instruction digest"):
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
                instruction_digest=digest,
            )
