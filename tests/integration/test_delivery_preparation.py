"""Durable preparation command coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.delivery_preparation import DeliveryPreparationService
from forge.domain.command import CommandStatus
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunState
from forge.persistence.models import Approval, Run, RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import update
from test_delivery_approved_plan import approved_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


class _PersistingProvisioner:
    def __init__(self, factory, path: Path) -> None:
        self._factory, self._path, self.calls = factory, path, 0

    async def prepare(self, run_id, policy):
        self.calls += 1
        async with PostgresUnitOfWork(self._factory) as work:
            run = await work.runs.get(run_id)
            identity = WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name or "forge/task18", policy.database.enabled
            )
            await work.runs.update_resource(
                run.id,
                run.version,
                worktree_path=str(self._path),
                database_state=ResourceState.DISABLED,
                event_type="test.worktree_provisioned",
                event_payload={},
            )
            await work.commit()
        return ManagedWorktree(
            identity=identity, path=self._path, base_sha=run.base_sha or "a" * 40
        )


class _ReclaimingProvisioner(_PersistingProvisioner):
    def __init__(self, factory, path: Path, command_id) -> None:
        super().__init__(factory, path)
        self._command_id = command_id

    async def prepare(self, run_id, policy):
        worktree = await super().prepare(run_id, policy)
        async with self._factory() as session, session.begin():
            await session.execute(
                update(RunCommand)
                .where(RunCommand.id == self._command_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        reclaimed = await PostgresCommandRepository(self._factory).claim_next(
            worker_id="reclaimer", lease_seconds=60
        )
        assert reclaimed is not None and reclaimed.id == self._command_id
        return worktree


class _MismatchingProvisioner(_PersistingProvisioner):
    async def prepare(self, run_id, policy):
        worktree = await super().prepare(run_id, policy)
        return replace(worktree, path=self._path.parent / "different-worktree")


async def _prepared_command(tmp_path, factory, *, database_enabled=False):
    case, approval_id = await approved_case(tmp_path, factory, database_enabled=database_enabled)
    commands = PostgresCommandRepository(factory)
    await commands.complete(case.command.id, worker_id="test-worker")
    async with factory() as session, session.begin():
        run, approval = (
            await session.get(Run, case.run_id),
            await session.get(Approval, approval_id),
        )
        assert run is not None and approval is not None
        run.branch_name = "forge/task18"
        version, actor = run.version, approval.authenticated_actor_id
    queued = await commands.enqueue(
        run_id=case.run_id,
        command_type="prepare_worktree",
        idempotency_key=f"{case.run_id}:prepare-worktree:{version}",
        payload={},
        expected_run_version=version,
        actor_id=actor,
    )
    command = await commands.claim_next(worker_id="test-worker", lease_seconds=60)
    assert command is not None and command.id == queued.id
    return case, approval_id, command, commands


@pytest.mark.parametrize("forgery", ["actor", "key", "payload"])
async def test_preparation_rejects_forged_delivery_before_effect(
    tmp_path, workflow_session_factory, forgery
):
    from dataclasses import replace
    from uuid import uuid4

    from forge.application.ports.commands import CommandRecoveryRequired

    case, _approval_id, command, _commands = await _prepared_command(
        tmp_path, workflow_session_factory
    )
    command = replace(
        command,
        **(
            {"actor_id": uuid4()}
            if forgery == "actor"
            else {"idempotency_key": "forged"}
            if forgery == "key"
            else {"payload": {"forged": True}}
        ),
    )
    provisioner = _PersistingProvisioner(workflow_session_factory, tmp_path / "worktree")
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert provisioner.calls == 0


async def test_preparation_settles_once_and_replays_without_second_effect(
    tmp_path, workflow_session_factory
):
    case, approval_id, command, commands = await _prepared_command(
        tmp_path, workflow_session_factory
    )
    provisioner = _PersistingProvisioner(workflow_session_factory, tmp_path / "worktree")
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
    assert provisioner.calls == 1
    async with workflow_session_factory() as session:
        run, approval = (
            await session.get(Run, case.run_id),
            await session.get(Approval, approval_id),
        )
    assert run is not None and approval is not None and run.state == RunState.IMPLEMENTING.value
    implement = await commands.get_by_idempotency_key(f"{case.run_id}:implement:1")
    assert implement is not None and implement.status is CommandStatus.PENDING
    assert implement.actor_id == approval.authenticated_actor_id
    assert implement.expected_run_version == run.version


async def test_reclaimed_lease_after_effect_prevents_implementation_dispatch(
    tmp_path, workflow_session_factory
):
    from forge.persistence.repositories.commands import CommandLeaseError

    case, _approval_id, command, commands = await _prepared_command(
        tmp_path, workflow_session_factory
    )
    provisioner = _ReclaimingProvisioner(
        workflow_session_factory, tmp_path / "worktree", command.id
    )
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandLeaseError):
            await service.execute(command, work)
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
    assert run is not None and run.state == RunState.PREPARING_WORKTREE.value
    assert run.worktree_path == str(tmp_path / "worktree")
    assert await commands.get_by_idempotency_key(f"{case.run_id}:implement:1") is None


async def test_mismatched_returned_worktree_retains_resources_without_dispatch(
    tmp_path, workflow_session_factory
):
    from forge.application.ports.commands import CommandRecoveryRequired

    case, _approval_id, command, commands = await _prepared_command(
        tmp_path, workflow_session_factory
    )
    provisioner = _MismatchingProvisioner(workflow_session_factory, tmp_path / "worktree")
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
    assert run is not None and run.state == RunState.PREPARING_WORKTREE.value
    assert run.worktree_path == str(tmp_path / "worktree")
    assert await commands.get_by_idempotency_key(f"{case.run_id}:implement:1") is None
