"""Validation decisions atomically bind the next command to durable evidence."""

from dataclasses import replace

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.delivery import DeliveryService
from forge.domain.run import RunState
from forge.persistence.models import Run, RunCommand
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_validation import _case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _decision_case(tmp_path, factory, *, failing=False):
    case, command, validation, runner = await _case(tmp_path, factory)
    if failing:
        original = runner.run_terminal

        async def failed_check(request):
            terminal = await original(request)
            return replace(terminal, result=replace(terminal.result, exit_code=1))

        runner.run_terminal = failed_check
    delivery = DeliveryService(
        case.artifact_store,
        validation=validation,
        git_factory=validation._git_factory,
    )
    return case, command, delivery, runner


async def test_passed_validation_enqueues_evidence_bound_fresh_review(
    tmp_path, workflow_session_factory
):
    case, command, delivery, runner = await _decision_case(tmp_path, workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        result = await delivery.validate(command, work)
    assert result.state is RunState.REVIEWING
    assert runner.calls == ["unit", "lint"]
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        queued = await work.commands.get_by_idempotency_key(f"{case.run_id}:review:1")
        assert queued.command_type == "review"
        assert queued.payload == {
            "semantic_attempt": 1,
            "validation_evidence_set_id": str(result.validation_evidence_set_id),
        }
        assert queued.expected_run_version == result.version
        assert queued.actor_id == command.actor_id


async def test_failed_validation_counts_before_enqueuing_remediation(
    tmp_path, workflow_session_factory
):
    case, command, delivery, runner = await _decision_case(
        tmp_path, workflow_session_factory, failing=True
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        result = await delivery.validate(command, work)
    assert result.state is RunState.REMEDIATING
    assert runner.calls == ["unit", "lint"]
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.local_remediation_count == 1
        queued = await work.commands.get_by_idempotency_key(f"{case.run_id}:remediate:1")
        assert queued.expected_run_version == run.version
        assert queued.payload["automatic"] is True
        assert queued.payload["validation_evidence_set_id"] == str(
            result.validation_evidence_set_id
        )


async def test_exhausted_validation_enters_intervention_without_queued_agent(
    tmp_path, workflow_session_factory
):
    case, command, delivery, _runner = await _decision_case(
        tmp_path, workflow_session_factory, failing=True
    )
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        run.local_remediation_count = 3
        await session.commit()
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        result = await delivery.validate(command, work)
    assert result.state is RunState.AWAITING_HUMAN_INTERVENTION
    async with workflow_session_factory() as session:
        queued = await session.scalars(
            select(RunCommand).where(
                RunCommand.run_id == case.run_id, RunCommand.status == "PENDING"
            )
        )
        assert list(queued) == []
        assert (await session.get(Run, case.run_id)).local_remediation_count == 3


async def test_decision_replay_does_not_rerun_checks_or_increment_count(
    tmp_path, workflow_session_factory
):
    case, command, delivery, runner = await _decision_case(
        tmp_path, workflow_session_factory, failing=True
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        first = await delivery.validate(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        replay = await delivery.validate(command, work)
        assert (await work.runs.get(case.run_id)).local_remediation_count == 1
    assert replay == first
    assert runner.calls == ["unit", "lint"]


async def test_decision_replay_rejects_mutated_queued_authority(tmp_path, workflow_session_factory):
    case, command, delivery, _runner = await _decision_case(tmp_path, workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await delivery.validate(command, work)
    async with workflow_session_factory() as session:
        queued = await session.scalar(
            select(RunCommand).where(
                RunCommand.run_id == case.run_id, RunCommand.command_type == "review"
            )
        )
        queued.payload = {"semantic_attempt": 1}
        await session.commit()
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await delivery.validate(command, work)


async def test_pause_between_publication_and_decision_prevents_next_dispatch(
    tmp_path, workflow_session_factory
):
    case, command, delivery, _runner = await _decision_case(tmp_path, workflow_session_factory)
    original = delivery._validation.execute

    async def paused(command, work):
        descriptor = await original(command, work)
        run = await work.runs.get(case.run_id)
        await work.runs.pause(run.id, run.version, "test.paused", {})
        await work.commit()
        return descriptor

    delivery._validation.execute = paused
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await delivery.validate(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert (await work.runs.get(case.run_id)).state is RunState.PAUSED
        assert await work.commands.get_by_idempotency_key(f"{case.run_id}:review:1") is None


async def test_changed_head_after_publication_cannot_dispatch_review(
    tmp_path, workflow_session_factory
):
    case, command, delivery, _runner = await _decision_case(tmp_path, workflow_session_factory)
    original = delivery._validation.execute

    async def changed(command, work):
        descriptor = await original(command, work)
        delivery._git_factory(None).head = "c" * 40
        return descriptor

    delivery._validation.execute = changed
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired, match="HEAD"):
            await delivery.validate(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert (await work.runs.get(case.run_id)).state is RunState.VALIDATING
        assert await work.commands.get_by_idempotency_key(f"{case.run_id}:review:1") is None


async def test_conflicting_next_command_rolls_back_decision_and_count(
    tmp_path, workflow_session_factory
):
    case, command, delivery, _runner = await _decision_case(
        tmp_path, workflow_session_factory, failing=True
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await work.commands.enqueue(
            run_id=case.run_id,
            command_type="remediate",
            idempotency_key=f"{case.run_id}:remediate:1",
            payload={"semantic_attempt": 1},
            expected_run_version=command.expected_run_version + 1,
            actor_id=command.actor_id,
        )
        await work.commit()
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await delivery.validate(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.VALIDATING
        assert run.local_remediation_count == 0
