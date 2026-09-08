"""A command failure before stage admission retains a safe continuation route."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.observability.usage import UsageRecord
from forge.persistence.models import RunCommand
from forge.persistence.models import RunEvent as RunEventRecord
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_development import _service_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _fail_pause_resume(factory, source):
    commands = PostgresCommandRepository(factory)
    actor = source.actor_id or uuid4()
    failed = await commands.fail(
        source.id, worker_id=source.lease_owner, error="pre-admission failure"
    )
    await commands.enqueue(
        run_id=source.run_id,
        command_type="pause",
        idempotency_key=str(uuid4()),
        payload={},
        expected_run_version=source.expected_run_version,
        actor_id=actor,
    )
    pause = await commands.claim_next(worker_id="pause", lease_seconds=60, lane=CommandLane.CONTROL)
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
        paused = await work.runs.get(source.run_id)
    await commands.complete(pause.id, worker_id="pause")
    await commands.enqueue(
        run_id=source.run_id,
        command_type="resume",
        idempotency_key=str(uuid4()),
        payload={},
        expected_run_version=paused.version,
        actor_id=actor,
    )
    resume = await commands.claim_next(worker_id="resume", lease_seconds=60)
    return commands, failed, paused, resume


@pytest.mark.parametrize("failures", [1, 2])
async def test_failed_unadmitted_developer_resumes_same_attempt_and_replays(
    tmp_path, workflow_session_factory, failures
):
    case, source, service, gateway, _git = await _service_case(tmp_path, workflow_session_factory)
    old_sources = []
    for _ in range(failures):
        commands, failed, paused, resume = await _fail_pause_resume(
            workflow_session_factory, source
        )
        old_sources.append(failed)
        handler = ResumeRunHandler(artifact_store=case.artifact_store)
        for _ in range(2):
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                await handler(resume, work)
                run = await work.runs.get(source.run_id)
                assert run.state is RunState.IMPLEMENTING
                assert run.version == paused.version + 1
        assert (await commands.get(failed.id)) == failed
        await commands.complete(resume.id, worker_id="resume")
        source = await commands.claim_next(worker_id="developer", lease_seconds=60)
        assert source is not None and source.command_type == "implement"
        assert source.payload["semantic_attempt"] == 1
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(source, work)
        assert (await work.runs.get(source.run_id)).state is RunState.VALIDATING
    assert len(gateway.requests) == 1
    for old in old_sources:
        assert (await commands.get(old.id)).status is CommandStatus.FAILED


async def test_failed_delivery_with_unresolved_effect_cannot_resume(
    tmp_path, workflow_session_factory
):
    case, source, _service, gateway, _git = await _service_case(tmp_path, workflow_session_factory)
    commands, failed, paused, resume = await _fail_pause_resume(workflow_session_factory, source)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await work.operations.begin(
            run_id=source.run_id,
            operation_type="repository_write",
            idempotency_key="unresolved",
            request_digest="a" * 64,
            request_payload={},
        )
        await work.commit()
    with pytest.raises(CommandRecoveryRequired):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert await work.runs.get(source.run_id) == paused
    assert await commands.get(source.id) == failed
    assert gateway.requests == []


async def test_failed_receipt_drift_blocks_replay_and_fresh_dispatch(
    tmp_path, workflow_session_factory
):
    case, source, service, gateway, _git = await _service_case(tmp_path, workflow_session_factory)
    commands, failed, _paused, resume = await _fail_pause_resume(workflow_session_factory, source)
    handler = ResumeRunHandler(artifact_store=case.artifact_store)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await handler(resume, work)
    async with workflow_session_factory() as session, session.begin():
        receipt = await session.scalar(
            select(RunEventRecord).where(
                RunEventRecord.run_id == source.run_id,
                RunEventRecord.event_type == "delivery.failed_before_admission",
            )
        )
        receipt.payload = {**receipt.payload, "actor_id": str(uuid4())}
    with pytest.raises(CommandRecoveryRequired, match="receipt"):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await handler(resume, work)
    await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="developer", lease_seconds=60)
    with pytest.raises(CommandRecoveryRequired, match="receipt"):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await service.execute(fresh, work)
    assert await commands.get(source.id) == failed
    assert gateway.requests == []


@pytest.mark.parametrize("stage", ["start_planning", "validate", "review"])
async def test_failed_stage_before_admission_resumes_original_inputs(
    tmp_path, workflow_session_factory, stage
):
    if stage == "start_planning":
        from test_planning_failed_usage import _build_case

        case = await _build_case(tmp_path, workflow_session_factory, fail_invalid=False)
        source, service = case.command, case.service
    elif stage == "validate":
        from test_delivery_validation import _case

        case, source, service, _runner = await _case(tmp_path, workflow_session_factory)
    else:
        from test_delivery_review import _review_case

        case, source, service, _gateway, _git, _validation = await _review_case(
            tmp_path, workflow_session_factory
        )
    commands, failed, _paused, resume = await _fail_pause_resume(workflow_session_factory, source)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
    await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="fresh", lease_seconds=60)
    assert fresh.command_type == stage
    assert fresh.payload["semantic_attempt"] == source.payload.get("semantic_attempt", 1)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert await service.execute(fresh, work) is not None
    assert await commands.get(source.id) == failed


@pytest.mark.parametrize(
    "obstruction", ["running_admission", "settled_admission", "ambiguous_source"]
)
async def test_failed_delivery_rejects_admission_or_ambiguous_source(
    tmp_path, workflow_session_factory, obstruction
):
    case, source, _service, gateway, _git = await _service_case(tmp_path, workflow_session_factory)
    commands, failed, paused, resume = await _fail_pause_resume(workflow_session_factory, source)
    if obstruction == "ambiguous_source":
        duplicate = await commands.enqueue(
            run_id=source.run_id,
            command_type=source.command_type,
            idempotency_key=str(uuid4()),
            payload=source.payload,
            expected_run_version=source.expected_run_version,
            actor_id=source.actor_id,
        )
        async with workflow_session_factory() as session, session.begin():
            row = await session.get(RunCommand, duplicate.id)
            row.status, row.attempt_count = "FAILED", 1
            row.completed_at = datetime.now(UTC)
    else:
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            step_id, execution_id = uuid4(), uuid4()
            await work.executions.admit(
                source.run_id,
                step_id,
                execution_id,
                "implement",
                1,
                AgentRole.DEVELOPER,
                "test-v1",
                "google",
                "fixture-model",
            )
            if obstruction == "settled_admission":
                await work.executions.finalize(
                    source.run_id,
                    step_id,
                    execution_id,
                    AgentFinishStatus.FAILED,
                    UsageRecord(
                        provider="google",
                        model="fixture-model",
                        prompt_version="test-v1",
                        pricing_version="fixture-v1",
                        estimated_cost_minor=0,
                        currency="USD",
                    ),
                    provider="google",
                    model="fixture-model",
                    instruction_version="test-v1",
                    kind="implement",
                    attempt=1,
                    role=AgentRole.DEVELOPER,
                )
            await work.commit()
    with pytest.raises(CommandRecoveryRequired):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert await work.runs.get(source.run_id) == paused
        assert not any(
            event.event_type == "delivery.failed_before_admission"
            for event in await work.events.list_after(source.run_id, 0)
        )
    assert await commands.get(source.id) == failed
    assert gateway.requests == []
