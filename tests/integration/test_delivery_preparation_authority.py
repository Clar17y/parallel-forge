"""A repeated preparation command cannot bless changed downstream authority."""

from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.delivery_preparation import DeliveryPreparationService
from forge.domain.resource import ResourceState, WorktreeIdentity, database_secret_id
from forge.persistence.models import Run, RunCommand, RunEvent
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_preparation import _PersistingProvisioner, _prepared_command
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("tamper", ("queue_actor", "queue_payload", "event_approval", "resource"))
async def test_preparation_replay_rejects_changed_authority(
    tmp_path, workflow_session_factory, tamper
):
    case, _, command, _ = await _prepared_command(tmp_path, workflow_session_factory)
    provisioner = _PersistingProvisioner(workflow_session_factory, tmp_path / "worktree")
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
    async with workflow_session_factory() as session, session.begin():
        queued = await session.scalar(
            select(RunCommand).where(RunCommand.idempotency_key == f"{case.run_id}:implement:1")
        )
        event = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == case.run_id, RunEvent.event_type == "run.worktree_prepared"
            )
        )
        if tamper == "queue_actor":
            queued.actor_id = uuid4()
        elif tamper == "queue_payload":
            queued.payload = {"semantic_attempt": 2}
        elif tamper == "event_approval":
            event.payload = {**event.payload, "approval_id": str(uuid4())}
        else:
            run = await session.get(Run, case.run_id)
            run.worktree_path = str(tmp_path / "different-worktree")
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert provisioner.calls == 1


async def test_uncertain_provisioning_failure_requires_recovery(tmp_path, workflow_session_factory):
    case, _, command, commands = await _prepared_command(tmp_path, workflow_session_factory)

    class UncertainProvisioner:
        async def prepare(self, run_id, policy):
            raise RuntimeError("untrusted adapter detail")

    service = DeliveryPreparationService(
        ApprovedPlanLoader(case.artifact_store), UncertainProvisioner()
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired) as raised:
            await service.execute(command, work)
    assert "untrusted adapter detail" not in str(raised.value)
    assert await commands.get_by_idempotency_key(f"{case.run_id}:implement:1") is None


@pytest.mark.parametrize("tamper", (None, "database_name", "database_role", "secret_id"))
async def test_enabled_database_must_match_exact_run_identity(
    tmp_path, workflow_session_factory, tamper
):
    case, _, command, commands = await _prepared_command(
        tmp_path,
        workflow_session_factory,
        database_enabled=True,
    )

    class DatabaseProvisioner:
        async def prepare(self, run_id, policy):
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                run = await work.runs.get(run_id)
                identity = WorktreeIdentity.for_run(run.project_id, run.id, run.branch_name, True)
                resource = {
                    "database_name": identity.database_name,
                    "database_role": identity.database_role,
                    "secret_id": database_secret_id(identity),
                }
                if tamper:
                    resource[tamper] = "different_resource"
                await work.runs.update_resource(
                    run.id,
                    run.version,
                    worktree_path=str(tmp_path / "worktree"),
                    database_state=ResourceState.ACTIVE,
                    **resource,
                    event_type="test.database_provisioned",
                    event_payload={},
                )
                await work.commit()
            return ManagedWorktree(
                identity=identity, path=tmp_path / "worktree", base_sha=run.base_sha
            )

    service = DeliveryPreparationService(
        ApprovedPlanLoader(case.artifact_store), DatabaseProvisioner()
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        if tamper:
            with pytest.raises(CommandRecoveryRequired):
                await service.execute(command, work)
        else:
            await service.execute(command, work)
    queued = await commands.get_by_idempotency_key(f"{case.run_id}:implement:1")
    assert (queued is None) is (tamper is not None)
