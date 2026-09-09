"""Preparation binds a durable run branch before external provisioning."""

from __future__ import annotations

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.delivery_preparation import DeliveryPreparationService
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.persistence.models import Run, RunEvent
from forge.persistence.repositories.runs import PersistenceError
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_preparation import _prepared_command
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def test_preparation_binds_durable_branch_before_external_effect(
    tmp_path, workflow_session_factory
):
    case, _, command, _ = await _prepared_command(tmp_path, workflow_session_factory)
    async with workflow_session_factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        assert run is not None
        run.branch_name = None

    class ObservingProvisioner:
        async def prepare(self, run_id, policy):
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                run = await work.runs.get(run_id)
                assert run.branch_name == f"forge/run/{run.id.hex}"
                identity = WorktreeIdentity.for_run(
                    run.project_id, run.id, run.branch_name, policy.database.enabled
                )
                await work.runs.update_resource(
                    run.id,
                    run.version,
                    worktree_path=str(tmp_path / "worktree"),
                    database_state=ResourceState.DISABLED,
                    event_type="test.worktree_provisioned",
                    event_payload={},
                )
                await work.commit()
            return ManagedWorktree(
                identity=identity, path=tmp_path / "worktree", base_sha=run.base_sha
            )

    service = DeliveryPreparationService(
        ApprovedPlanLoader(case.artifact_store), ObservingProvisioner()
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)


async def test_preparation_retry_after_binding_reuses_the_causal_branch(
    tmp_path, workflow_session_factory
):
    case, _, command, _ = await _prepared_command(tmp_path, workflow_session_factory)
    async with workflow_session_factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        assert run is not None
        run.branch_name = None

    class FailsAfterBinding:
        def __init__(self) -> None:
            self.calls = 0

        async def prepare(self, run_id, policy):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("simulated crash")
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                run = await work.runs.get(run_id)
                identity = WorktreeIdentity.for_run(
                    run.project_id, run.id, run.branch_name, policy.database.enabled
                )
                await work.runs.update_resource(
                    run.id,
                    run.version,
                    worktree_path=str(tmp_path / "worktree"),
                    database_state=ResourceState.DISABLED,
                    event_type="test.worktree_provisioned",
                    event_payload={},
                )
                await work.commit()
            return ManagedWorktree(
                identity=identity, path=tmp_path / "worktree", base_sha=run.base_sha
            )

    provisioner = FailsAfterBinding()
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    async with workflow_session_factory() as session:
        bindings = await session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == case.run_id,
                RunEvent.event_type == "run.preparation_branch_bound",
            )
        )
        assert len(bindings.all()) == 1
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
    assert provisioner.calls == 2


@pytest.mark.parametrize("tamper", ["command", "branch"])
async def test_preparation_rejects_tampered_causal_branch_binding(
    tmp_path, workflow_session_factory, tamper
):
    case, _, command, _ = await _prepared_command(tmp_path, workflow_session_factory)
    async with workflow_session_factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        assert run is not None
        run.branch_name = None

    class FailingProvisioner:
        calls = 0

        async def prepare(self, run_id, policy):
            self.calls += 1
            raise RuntimeError("simulated crash")

    provisioner = FailingProvisioner()
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    async with workflow_session_factory() as session, session.begin():
        event = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == case.run_id,
                RunEvent.event_type == "run.preparation_branch_bound",
            )
        )
        assert event is not None
        if tamper == "command":
            event.payload = {**event.payload, "source_command_id": "forged"}
        else:
            run = await session.get(Run, case.run_id)
            run.branch_name = "forge/substituted"
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert provisioner.calls == 1


async def test_preparation_does_not_bind_a_branch_after_resource_effect(
    tmp_path, workflow_session_factory
):
    case, _, command, _ = await _prepared_command(tmp_path, workflow_session_factory)
    async with workflow_session_factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        assert run is not None
        run.branch_name = None
        run.worktree_path = str(tmp_path / "already-created")

    class NeverProvision:
        calls = 0

        async def prepare(self, run_id, policy):
            self.calls += 1
            raise AssertionError("resource-effect run must not provision")

    provisioner = NeverProvision()
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(PersistenceError, match="no resource effects"):
            await service.execute(command, work)
    assert provisioner.calls == 0


async def test_preparation_does_not_bind_a_branch_with_stale_command_version(
    tmp_path, workflow_session_factory
):
    case, _, command, _ = await _prepared_command(tmp_path, workflow_session_factory)
    async with workflow_session_factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        assert run is not None
        run.branch_name = None
        run.version += 1

    class NeverProvision:
        calls = 0

        async def prepare(self, run_id, policy):
            self.calls += 1
            raise AssertionError("stale command must not provision")

    provisioner = NeverProvision()
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired, match="branch binding requires recovery"):
            await service.execute(command, work)
    assert provisioner.calls == 0
