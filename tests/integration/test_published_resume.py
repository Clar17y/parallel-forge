"""Resume after stage publication commits but before the decision commits."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.delivery import DeliveryService
from forge.application.services.resume_reconciliation import ResumeReconciler
from forge.application.services.review_decision import ReviewDecisionService
from forge.domain.command import CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunState
from forge.persistence.models import RunCommand, Step
from forge.persistence.models import RunEvent as RunEventRecord
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select, update
from test_delivery_review import _review_case
from test_delivery_validation import _case
from test_resumed_delivery import _pause_stage
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _published(tmp_path, factory, phase):
    if phase == "validate":
        case, source, service, runner = await _case(tmp_path, factory)
        observer = runner.calls
        git = service._git_factory(None)
    else:
        case, source, service, gateway, git, _validation = await _review_case(tmp_path, factory)
        observer = gateway.requests
    async with PostgresUnitOfWork(factory) as work:
        descriptor = await service.execute(source, work)
    await _pause_stage(factory, source)
    commands = PostgresCommandRepository(factory)
    async with factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    async with PostgresUnitOfWork(factory) as work:
        paused = await work.runs.get(source.run_id)
    await commands.enqueue(
        run_id=source.run_id,
        command_type="resume",
        idempotency_key=str(uuid4()),
        payload={},
        expected_run_version=paused.version,
        actor_id=source.actor_id,
    )
    resume = await commands.claim_next(worker_id="resume", lease_seconds=60)
    assert resume.command_type == "resume"
    return case, source, service, descriptor, observer, git, paused, resume, commands


@pytest.mark.parametrize("phase", ["validate", "review"])
async def test_published_stage_resumes_fresh_attempt_without_rewriting_success(
    tmp_path, workflow_session_factory, phase
):
    factory = workflow_session_factory
    case, source, service, descriptor, observer, git, _paused, resume, commands = await _published(
        tmp_path, factory, phase
    )
    before = list(observer)
    async with PostgresUnitOfWork(factory) as work:
        old_outcome = (
            await work.executions.get_outcome(source.run_id, "review", 1)
            if phase == "review"
            else None
        )
    async with factory() as session:
        original = await session.get(Step, descriptor.step_id)
        original_identity = (original.status, original.completed_at, original.output_artifact_id)
    handler = ResumeRunHandler(artifact_store=case.artifact_store)
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await handler(resume, work)
    assert observer == before
    assert (await commands.get(source.id)).status is CommandStatus.CANCELLED
    await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="fresh", lease_seconds=60)
    assert fresh.command_type == phase and fresh.payload["semantic_attempt"] == 2
    async with PostgresUnitOfWork(factory) as work:
        if phase == "validate":
            decision = await DeliveryService(
                case.artifact_store, validation=service, git_factory=lambda _: git
            ).validate(fresh, work)
            assert decision.state is RunState.REVIEWING
            assert observer == ["unit", "lint"] * 2
        else:
            await service.execute(fresh, work)
            decision = await ReviewDecisionService(
                case.artifact_store, git_factory=lambda _: git
            ).decide(fresh, work)
            assert decision.state is RunState.AWAITING_PR_APPROVAL
            assert len(observer) == 2 and observer[0].execution_id != observer[1].execution_id
            assert observer[1].parent_execution_id is None
    async with factory() as session:
        original = await session.get(Step, descriptor.step_id)
        assert (
            original.status,
            original.completed_at,
            original.output_artifact_id,
        ) == original_identity
        assert original.status == "SUCCEEDED"
        steps = list(
            await session.scalars(
                select(Step).where(Step.run_id == source.run_id, Step.kind == phase)
            )
        )
        assert len(steps) == 2 and all(step.status == "SUCCEEDED" for step in steps)
    if old_outcome is not None:
        async with PostgresUnitOfWork(factory) as work:
            assert await work.executions.get_outcome(source.run_id, "review", 1) == old_outcome


@pytest.mark.parametrize("phase", ["validate", "review"])
@pytest.mark.parametrize("obstruction", ["decision", "actor", "corrupt_bytes"])
async def test_published_recovery_rejects_changed_authority_without_settlement(
    tmp_path, workflow_session_factory, phase, obstruction
):
    factory = workflow_session_factory
    case, source, _service, descriptor, observer, _git, paused, resume, commands = await _published(
        tmp_path, factory, phase
    )
    if obstruction == "decision":
        async with PostgresUnitOfWork(factory) as work:
            await work.events.append(
                RunEvent(
                    run_id=source.run_id,
                    run_version=source.expected_run_version,
                    event_type="run.validation_decided"
                    if phase == "validate"
                    else "run.review_decided",
                    actor_class="worker",
                    payload={"source_command_id": str(source.id)},
                )
            )
            await work.commit()
    elif obstruction == "actor":
        async with factory() as session, session.begin():
            (await session.get(RunCommand, source.id)).actor_id = uuid4()
    else:
        original = case.artifact_store.open_bytes

        async def corrupted(digest):
            return b"{}" if digest == descriptor.manifest_digest else await original(digest)

        case.artifact_store.open_bytes = corrupted
    before = list(observer)
    with pytest.raises(CommandRecoveryRequired):
        async with PostgresUnitOfWork(factory) as work:
            await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
    async with PostgresUnitOfWork(factory) as work:
        assert await work.runs.get(source.run_id) == paused
    assert (await commands.get(source.id)).status is CommandStatus.LEASED
    assert observer == before


@pytest.mark.parametrize("phase", ["validate", "review"])
@pytest.mark.parametrize("obstruction", [None, "corrupt_bytes", "output_id"])
async def test_published_receipt_is_verified_again_before_continuation(
    tmp_path, workflow_session_factory, phase, obstruction
):
    factory = workflow_session_factory
    case, source, _service, descriptor, observer, _git, paused, resume, commands = await _published(
        tmp_path, factory, phase
    )
    async with PostgresUnitOfWork(factory) as work:
        settled = await ResumeReconciler(case.artifact_store).reconcile(work, resume)
        assert len(settled) == 1 and settled[0].status is CommandStatus.CANCELLED
        await work.commit()
    if obstruction == "corrupt_bytes":
        original = case.artifact_store.open_bytes

        async def corrupted(digest):
            return b"{}" if digest == descriptor.manifest_digest else await original(digest)

        case.artifact_store.open_bytes = corrupted
    elif obstruction == "output_id":
        async with factory() as session, session.begin():
            receipt = await session.scalar(
                select(RunEventRecord).where(
                    RunEventRecord.run_id == source.run_id,
                    RunEventRecord.event_type == "delivery.suspended",
                )
            )
            receipt.payload = {**receipt.payload, "output_artifact_id": str(uuid4())}
    before = list(observer)
    handler = ResumeRunHandler(artifact_store=case.artifact_store)
    if obstruction is None:
        async with PostgresUnitOfWork(factory) as work:
            await handler(resume, work)
            assert (await work.runs.get(source.run_id)).version == paused.version + 1
    else:
        with pytest.raises(CommandRecoveryRequired):
            async with PostgresUnitOfWork(factory) as work:
                await handler(resume, work)
        async with PostgresUnitOfWork(factory) as work:
            assert await work.runs.get(source.run_id) == paused
    assert (await commands.get(source.id)).status is CommandStatus.CANCELLED
    assert observer == before
