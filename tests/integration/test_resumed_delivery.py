"""PostgreSQL end-to-end coverage for resuming a stopped Developer delivery."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.prompt_loader import PromptLoader
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired, CommandSuspended
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.development import DevelopmentService
from forge.application.services.resume_source import resume_origin
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import update
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

from tests.integration.test_delivery_development import _Gateway, _Git, _implement_command, _Reader

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def _service(case, factory, path, gateway):
    git = _Git()
    git.path = path
    return DevelopmentService(
        gateway,
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda policy: git,
        lambda policy, worktree: _Reader(),
    )


async def _resumed_implement_delivery(tmp_path, factory):
    case, source, commands, path = await _implement_command(tmp_path, factory)

    class PausingGateway(_Gateway):
        async def execute(self, request):
            result = await super().execute(request)
            async with PostgresUnitOfWork(self.factory) as work:
                run = await work.runs.get(case.run_id)
                pause = await commands.enqueue(
                    run_id=run.id,
                    command_type="pause",
                    idempotency_key=f"{run.id}:pause:resumed-delivery",
                    payload={},
                    expected_run_version=run.version,
                    actor_id=source.actor_id,
                )
                control = await commands.claim_next(
                    worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
                )
                assert control is not None and control.id == pause.id
                await PauseRunHandler()(control, work)
                await work.commit()
            return result

    stopped = _service(case, factory, path, PausingGateway(factory))
    async with PostgresUnitOfWork(factory) as work:
        with pytest.raises(CommandSuspended):
            await stopped.execute(source, work)
    pause = await commands.get_by_idempotency_key(f"{case.run_id}:pause:resumed-delivery")
    assert pause is not None
    await commands.complete(pause.id, worker_id="control")
    async with PostgresUnitOfWork(factory) as work:
        await work.session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        paused = await work.runs.get(case.run_id)
        resume = await work.commands.enqueue(
            run_id=case.run_id,
            command_type="resume",
            idempotency_key=f"resume:{uuid4().hex}",
            payload={},
            expected_run_version=paused.version,
            actor_id=source.actor_id,
        )
        await work.commit()
    claimed = await commands.claim_next(
        worker_id="resume", lease_seconds=60, lane=CommandLane.NORMAL
    )
    assert claimed is not None and claimed.id == resume.id
    async with PostgresUnitOfWork(factory) as work:
        await ResumeRunHandler()(claimed, work)
    await commands.complete(claimed.id, worker_id="resume")
    fresh = await commands.claim_next(
        worker_id="developer", lease_seconds=60, lane=CommandLane.NORMAL
    )
    assert fresh is not None and fresh.command_type == "implement"
    return case, source, claimed, fresh, path


async def test_resume_reexecutes_stopped_developer_without_consuming_remediation_budget(
    tmp_path, workflow_session_factory
):
    case, source, resume, fresh, path = await _resumed_implement_delivery(
        tmp_path, workflow_session_factory
    )
    gateway = _Gateway(workflow_session_factory)
    service = _service(case, workflow_session_factory, path, gateway)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(fresh, work)

    assert fresh.id != source.id
    assert fresh.actor_id == source.actor_id
    assert fresh.expected_run_version == resume.expected_run_version + 1
    assert fresh.payload["semantic_attempt"] == source.payload["semantic_attempt"] + 1
    assert fresh.payload["resume_command_id"] == str(resume.id)
    assert fresh.payload["source_command_id"] == str(source.id)
    assert fresh.idempotency_key == (
        f"{fresh.run_id}:resume:{resume.id}:implement:{fresh.payload['semantic_attempt']}"
    )
    assert len(gateway.requests) == 1
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        restored = await work.runs.get(fresh.run_id)
        assert restored.state is RunState.VALIDATING
        assert restored.local_remediation_count == 0
        assert await resume_origin(work, fresh) == await work.commands.get(source.id)


async def test_tampered_resumed_developer_provenance_fails_before_provider_dispatch(
    tmp_path, workflow_session_factory
):
    case, _source, _resume, fresh, path = await _resumed_implement_delivery(
        tmp_path, workflow_session_factory
    )
    gateway = _Gateway(workflow_session_factory)
    service = _service(case, workflow_session_factory, path, gateway)
    tampered = replace(fresh, payload={**fresh.payload, "source_command_id": str(uuid4())})
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(tampered, work)
    assert gateway.requests == []


async def _pause_stage(factory, command):
    from forge.persistence.repositories.commands import PostgresCommandRepository

    commands = PostgresCommandRepository(factory)
    await commands.enqueue(
        run_id=command.run_id,
        command_type="pause",
        idempotency_key=f"pause:{command.id}",
        payload={},
        expected_run_version=command.expected_run_version,
        actor_id=command.actor_id,
    )
    pause = await commands.claim_next(
        worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
    )
    assert pause is not None
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
    await commands.complete(pause.id, worker_id="control")


async def _resume_stage(factory, source):
    from forge.persistence.repositories.commands import PostgresCommandRepository

    commands = PostgresCommandRepository(factory)
    await commands.complete(source.id, worker_id=source.lease_owner)
    async with PostgresUnitOfWork(factory) as work:
        paused = await work.runs.get(source.run_id)
    await commands.enqueue(
        run_id=source.run_id,
        command_type="resume",
        idempotency_key=f"resume:{source.id}",
        payload={},
        expected_run_version=paused.version,
        actor_id=source.actor_id,
    )
    resume = await commands.claim_next(worker_id="resume", lease_seconds=60)
    assert resume is not None
    async with PostgresUnitOfWork(factory) as work:
        await ResumeRunHandler()(resume, work)
    await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="fresh", lease_seconds=60)
    assert fresh is not None
    return fresh


async def test_resumed_validation_runs_checks_and_decides_review(
    tmp_path, workflow_session_factory
):
    from forge.application.services.delivery import DeliveryService

    from tests.integration.test_delivery_validation import _case

    factory = workflow_session_factory
    case, source, service, runner = await _case(tmp_path, factory)
    original = runner.run_terminal

    async def paused_check(request):
        result = await original(request)
        await _pause_stage(factory, source)
        return result

    runner.run_terminal = paused_check
    async with PostgresUnitOfWork(factory) as work:
        with pytest.raises(CommandSuspended):
            await service.execute(source, work)
    runner.run_terminal = original
    fresh = await _resume_stage(factory, source)
    delivery = DeliveryService(
        case.artifact_store, validation=service, git_factory=service._git_factory
    )
    async with PostgresUnitOfWork(factory) as work:
        decision = await delivery.validate(fresh, work)
    assert decision.state is RunState.REVIEWING
    assert runner.calls == ["unit", "unit", "lint"]
    async with PostgresUnitOfWork(factory) as work:
        assert (await work.runs.get(source.run_id)).local_remediation_count == 0


async def test_resumed_review_publishes_and_decides_candidate(tmp_path, workflow_session_factory):
    from forge.application.handlers.delivery import ReviewHandler
    from forge.application.services.review_decision import ReviewDecisionService

    from tests.integration.test_delivery_review import _review_case

    factory = workflow_session_factory
    case, source, service, gateway, git, _ = await _review_case(tmp_path, factory)
    original = gateway.execute

    async def paused_review(request):
        result = await original(request)
        await _pause_stage(factory, source)
        return result

    gateway.execute = paused_review
    async with PostgresUnitOfWork(factory) as work:
        with pytest.raises(CommandSuspended):
            await service.execute(source, work)
    gateway.execute = original
    fresh = await _resume_stage(factory, source)
    handler = ReviewHandler(
        service, ReviewDecisionService(case.artifact_store, git_factory=lambda policy: git)
    )
    async with PostgresUnitOfWork(factory) as work:
        decision = await handler(fresh, work)
    assert decision.state is RunState.AWAITING_PR_APPROVAL
    assert len(gateway.requests) == 2
    assert gateway.requests[0].execution_id != gateway.requests[1].execution_id
    assert gateway.requests[1].parent_execution_id is None
