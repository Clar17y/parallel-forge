"""Recover returned agent outcomes after a queued pause fences publication."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.errors import AgentOutputInvalid
from forge.agents.prompt_loader import PromptLoader
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.development import DevelopmentService
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import AgentExecution, ArtifactLineage, ModelUsage, RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete, select, update
from test_delivery_development import _Gateway, _Git, _implement_command, _Reader
from test_delivery_review import _review_case
from test_planning_failed_usage import _build_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _stage_case(tmp_path, factory, stage):
    if stage in {"plan", "plan_invalid"}:
        case = await _build_case(tmp_path, factory, fail_invalid=stage == "plan_invalid")
        return case, case.command, case.service, case.gateway
    if stage == "review":
        case, command, service, gateway, _git, _decision = await _review_case(tmp_path, factory)
        return case, command, service, gateway
    case, command, _commands, path = await _implement_command(tmp_path, factory)
    git = _Git()
    git.path = path
    gateway = _Gateway(factory)
    service = DevelopmentService(
        gateway,
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda policy: git,
        lambda policy, worktree: _Reader(),
    )
    return case, command, service, gateway


async def _retained_case(tmp_path, factory, stage):
    case, source, service, gateway = await _stage_case(tmp_path, factory, stage)
    commands = PostgresCommandRepository(factory)
    original = gateway.execute

    async def pause_queued_after_result(request):
        try:
            result = await original(request)
            gateway.retained_usage = result.usage
            return result
        except AgentOutputInvalid as error:
            gateway.retained_usage = error.usage
            raise
        finally:
            async with PostgresUnitOfWork(factory) as work:
                run = await work.runs.get(source.run_id)
            await commands.enqueue(
                run_id=run.id,
                command_type="pause",
                idempotency_key=f"{run.id}:retained-pause",
                payload={},
                expected_run_version=run.version,
                actor_id=uuid4(),
            )

    gateway.execute = pause_queued_after_result
    async with PostgresUnitOfWork(factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(source, work)
    gateway.execute = original
    pause = await commands.claim_next(
        worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
    )
    assert pause is not None and pause.command_type == "pause"
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
    await commands.complete(pause.id, worker_id="control")
    async with PostgresUnitOfWork(factory) as work:
        paused = await work.runs.get(source.run_id)
    assert paused.state is RunState.PAUSED
    async with factory() as session:
        execution = await session.get(AgentExecution, gateway.requests[0].execution_id)
        assert execution is not None and execution.status == "RUNNING"
    async with factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await commands.enqueue(
        run_id=source.run_id,
        command_type="resume",
        idempotency_key=f"{source.run_id}:retained-resume",
        payload={},
        expected_run_version=paused.version,
        actor_id=uuid4(),
    )
    resume = await commands.claim_next(worker_id="resume", lease_seconds=60)
    assert resume is not None and resume.command_type == "resume"
    return case, source, service, gateway, commands, resume, paused


@pytest.mark.parametrize("stage", ["plan", "plan_invalid", "implement", "review"])
async def test_resume_settles_retained_result_before_fresh_dispatch(
    tmp_path, workflow_session_factory, stage
):
    case, source, service, gateway, commands, resume, paused = await _retained_case(
        tmp_path, workflow_session_factory, stage
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        handler = ResumeRunHandler(artifact_store=case.artifact_store)
        await handler(resume, work)
        await handler(resume, work)
    assert len(gateway.requests) == 1
    assert (await commands.get(source.id)).status is CommandStatus.CANCELLED
    async with workflow_session_factory() as session:
        old = await session.get(AgentExecution, gateway.requests[0].execution_id)
        assert old is not None and old.status == "CANCELLED"
        usage = list(
            await session.scalars(
                select(ModelUsage).where(
                    ModelUsage.agent_execution_id == gateway.requests[0].execution_id
                )
            )
        )
        assert len(usage) == 1
        for field in (
            "provider",
            "model",
            "prompt_version",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "estimated_cost_minor",
            "pricing_version",
            "currency",
        ):
            assert getattr(usage[0], field) == getattr(gateway.retained_usage, field)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        restored = await work.runs.get(source.run_id)
        assert restored.state is paused.suspended_state
        assert restored.local_remediation_count == paused.local_remediation_count
        events = await work.events.list_after(source.run_id, 0)
        receipts = [event for event in events if event.event_type == "delivery.suspended"]
        assert len(receipts) == 1 and receipts[0].payload["command_id"] == str(source.id)
    await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="fresh", lease_seconds=60)
    assert fresh is not None and fresh.command_type == source.command_type
    assert fresh.payload["semantic_attempt"] == source.payload.get("semantic_attempt", 1) + 1
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(fresh, work)
    assert len(gateway.requests) == 2
    assert gateway.requests[0].execution_id != gateway.requests[1].execution_id
    async with workflow_session_factory() as session:
        new = await session.get(AgentExecution, gateway.requests[1].execution_id)
        assert new is not None and new.status == (
            "FAILED" if stage == "plan_invalid" else "SUCCEEDED"
        )


@pytest.mark.parametrize(
    "obstruction",
    [
        "renewed_lease",
        "corrupt_result",
        "missing_result",
        "substituted_command",
        "missing_store",
        "pending_operation",
    ],
)
async def test_retained_recovery_rejects_uncertain_authority_atomically(
    tmp_path, workflow_session_factory, obstruction
):
    case, source, _service, gateway, commands, resume, paused = await _retained_case(
        tmp_path, workflow_session_factory, "plan"
    )
    store = case.artifact_store
    if obstruction == "renewed_lease":
        async with workflow_session_factory() as session, session.begin():
            await session.execute(
                update(RunCommand)
                .where(RunCommand.id == source.id)
                .values(lease_expires_at=datetime.now(UTC) + timedelta(minutes=1))
            )
    elif obstruction == "corrupt_result":
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            artifacts = await work.artifacts.get_by_producer(
                run_id=source.run_id,
                producer_type="planning_late_result",
                producer_id=gateway.requests[0].execution_id,
            )
        assert len(artifacts) == 1
        corrupt_digest = artifacts[0].digest

        class CorruptRead:
            async def open_bytes(self, digest):
                wire = await case.artifact_store.open_bytes(digest)
                return wire + b" " if digest == corrupt_digest else wire

        store = CorruptRead()
    elif obstruction in {"missing_result", "substituted_command"}:
        replacement = None
        if obstruction == "substituted_command":
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                artifacts = await work.artifacts.get_by_producer(
                    run_id=source.run_id,
                    producer_type="planning_late_result",
                    producer_id=gateway.requests[0].execution_id,
                )
            assert len(artifacts) == 1
            payload = json.loads(await store.open_bytes(artifacts[0].digest))
            payload["command_id"] = str(uuid4())
            replacement = await store.put_bytes(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                media_type="application/json",
            )
        async with workflow_session_factory() as session, session.begin():
            await session.execute(
                delete(ArtifactLineage).where(
                    ArtifactLineage.run_id == source.run_id,
                    ArtifactLineage.producer_kind == "planning_late_result",
                    ArtifactLineage.producer_id == gateway.requests[0].execution_id,
                )
            )
        if replacement is not None:
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                await work.artifacts.record(
                    replacement,
                    run_id=source.run_id,
                    producer_type="planning_late_result",
                    producer_id=gateway.requests[0].execution_id,
                )
                await work.commit()
    elif obstruction == "pending_operation":
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await work.operations.begin(
                run_id=source.run_id,
                operation_type="repository_write",
                idempotency_key="retained-unresolved-operation",
                request_digest="a" * 64,
                request_payload={},
            )
            await work.commit()
    else:
        store = None
    with pytest.raises(CommandRecoveryRequired):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await ResumeRunHandler(artifact_store=store)(resume, work)
    assert (await commands.get(source.id)).status is CommandStatus.LEASED
    assert len(gateway.requests) == 1
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert await work.runs.get(source.run_id) == paused
        events = await work.events.list_after(source.run_id, 0)
        assert not any(
            event.event_type in {"run.resumed", "delivery.suspended"} for event in events
        )
    async with workflow_session_factory() as session:
        execution = await session.get(AgentExecution, gateway.requests[0].execution_id)
        assert execution is not None and execution.status == "RUNNING"
