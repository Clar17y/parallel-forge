"""PostgreSQL coverage for zero-admission paused delivery settlement."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.ports.controller_steps import ControllerStepUnsettledError
from forge.application.ports.executions import ExecutionUnsettledError
from forge.application.services.deferred_delivery import _validate_source, settle_deferred_delivery
from forge.domain.actor import AgentRole
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import RunEvent as RunEventRecord
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _paused_commands(
    command_repository,
    persisted_run,
    session_factory,
    *,
    forged=False,
    prior_attempt=False,
    source_actor=None,
):
    actor = uuid4()
    source = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="deferred-source",
        payload={},
        expected_run_version=0,
        actor_id=source_actor,
    )
    if prior_attempt:
        claimed_source = await command_repository.claim_next(worker_id="old", lease_seconds=60)
        assert claimed_source is not None and claimed_source.id == source.id
        source = await command_repository.fail(
            source.id, worker_id="old", error="interrupted", transient=True
        )
    pause = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="pause",
        idempotency_key="deferred-pause",
        payload={},
        expected_run_version=0,
        actor_id=actor,
    )
    claimed_pause = await command_repository.claim_next(
        worker_id="operator", lease_seconds=60, lane=CommandLane.CONTROL
    )
    assert claimed_pause is not None and claimed_pause.id == pause.id
    await command_repository.complete(pause.id, worker_id="operator")
    async with PostgresUnitOfWork(session_factory) as work:
        paused = await work.runs.pause(
            persisted_run.id,
            0,
            "run.paused",
            {
                "command_id": str(pause.id),
                "command_type": "pause",
                "command_payload": {"forged": True} if forged else {},
                "expected_run_version": 0,
            },
            actor_class="operator",
            actor_id=actor,
        )
        await work.commit()
    resume = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="resume",
        idempotency_key="deferred-resume",
        payload={},
        expected_run_version=paused.version,
        actor_id=actor,
    )
    claimed_resume = await command_repository.claim_next(worker_id="resumer", lease_seconds=60)
    assert claimed_resume is not None and claimed_resume.id == resume.id
    return source, claimed_resume


@pytest.mark.integration
async def test_settle_deferred_delivery_cancels_unstarted_source_and_records_zero_admission(
    command_repository, persisted_run, session_factory
) -> None:
    source, resume = await _paused_commands(
        command_repository, persisted_run, session_factory, source_actor=None
    )

    async with PostgresUnitOfWork(session_factory) as work:
        cancelled = await settle_deferred_delivery(work, resume, source)
        assert cancelled.status is CommandStatus.CANCELLED
        assert cancelled.attempt == 0
        await work.commit()

    stored = await command_repository.get(source.id)
    assert stored.status is CommandStatus.CANCELLED
    assert stored.attempt == 0
    async with PostgresUnitOfWork(session_factory) as work:
        events = await work.events.list_after(source.run_id, 0)
    receipt = [event for event in events if event.event_type == "delivery.deferred"]
    assert len(receipt) == 1
    assert receipt[0].run_version == resume.expected_run_version
    assert receipt[0].payload == {
        "command_id": str(source.id),
        "command_type": "start_planning",
        "idempotency_key": "deferred-source",
        "command_payload": {},
        "expected_run_version": 0,
        "actor_id": None,
        "payload_schema_version": 1,
        "delivery_attempt": 0,
        "kind": "plan",
        "semantic_attempt": 1,
        "deferred_state": "CREATED",
        "pause_command_id": receipt[0].payload["pause_command_id"],
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    "authority,blocker",
    [
        ("forged", None),
        ("missing", None),
        ("valid", "command"),
        ("valid", "operation"),
        ("valid", "execution"),
    ],
)
async def test_settle_deferred_delivery_rejects_bad_authority_or_effects_without_cancelling_source(
    command_repository, persisted_run, session_factory, authority, blocker
) -> None:
    source, resume = await _paused_commands(
        command_repository, persisted_run, session_factory, forged=authority == "forged"
    )
    if authority == "missing":
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(RunEventRecord).where(
                    RunEventRecord.run_id == source.run_id,
                    RunEventRecord.event_type == "run.paused",
                )
            )
    if blocker == "command":
        await command_repository.enqueue(
            run_id=source.run_id,
            command_type="other-normal-work",
            idempotency_key="deferred-active-effect",
            payload={},
            expected_run_version=0,
            actor_id=source.actor_id,
        )
    elif blocker == "operation":
        async with PostgresUnitOfWork(session_factory) as work:
            await work.operations.begin(
                run_id=source.run_id,
                operation_type="repository_write",
                idempotency_key="deferred-unresolved-operation",
                request_digest="a" * 64,
                request_payload={},
            )
            await work.commit()
    elif blocker == "execution":
        async with PostgresUnitOfWork(session_factory) as work:
            await work.executions.admit(
                source.run_id,
                uuid4(),
                uuid4(),
                "plan",
                1,
                AgentRole.PLANNER,
                "1",
                "test",
                "fixture",
            )
            await work.commit()

    with pytest.raises(CommandRecoveryRequired):
        async with PostgresUnitOfWork(session_factory) as work:
            await settle_deferred_delivery(work, resume, source)
            await work.commit()

    assert (await command_repository.get(source.id)).status is CommandStatus.PENDING


@pytest.mark.integration
async def test_settle_deferred_delivery_rejects_previously_attempted_pending_source(
    command_repository, persisted_run, session_factory
) -> None:
    source, resume = await _paused_commands(
        command_repository, persisted_run, session_factory, prior_attempt=True
    )
    assert source.status is CommandStatus.PENDING and source.attempt == 1
    with pytest.raises(CommandRecoveryRequired):
        async with PostgresUnitOfWork(session_factory) as work:
            await settle_deferred_delivery(work, resume, source)
            await work.commit()
    assert (await command_repository.get(source.id)).status is CommandStatus.PENDING


@pytest.mark.parametrize(
    ("state", "command_type", "error"),
    [
        (RunState.PLANNING, "start_planning", ExecutionUnsettledError("running")),
        (RunState.VALIDATING, "validate", ControllerStepUnsettledError("running")),
    ],
)
async def test_deferred_source_maps_unsettled_attempt_to_recovery(
    command_repository, persisted_run, session_factory, state, command_type, error
) -> None:
    source, _resume = await _paused_commands(command_repository, persisted_run, session_factory)
    async with PostgresUnitOfWork(session_factory) as work:
        paused = await work.runs.get(source.run_id)
    run = replace(paused, suspended_state=state)
    candidate = replace(source, command_type=command_type)
    fake_work = SimpleNamespace(
        executions=SimpleNamespace(next_attempt=AsyncMock(side_effect=error)),
        controller_steps=SimpleNamespace(next_attempt=AsyncMock(side_effect=error)),
    )
    with pytest.raises(CommandRecoveryRequired, match="unsettled prior attempt"):
        await _validate_source(fake_work, run, candidate)
