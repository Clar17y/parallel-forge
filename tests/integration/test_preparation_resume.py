"""Preparation resumes against original approval and exact branch provenance."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.delivery_preparation import DeliveryPreparationService
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import Approval, Run, RunCommand
from forge.persistence.models import RunEvent as RunEventRecord
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.worktree import WorktreeProvisioner
from sqlalchemy import select, update
from test_delivery_preparation import _PersistingProvisioner, _prepared_command
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

from apps.orchestrator.tests.tools.test_worktree_integration import _DisabledDatabase, _GitEffect

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize(
    "bound, pause_count",
    [
        (False, 1),
        (False, 2),
        (True, 1),
        ("prepared", 1),
        ("missing_resource", 1),
        ("forged_branch", 1),
        ("approval_drift", 1),
        ("renewed_lease", 1),
        ("provenance", 2),
    ],
)
async def test_prepare_resumes_once_without_losing_approval_or_rebinding_branch(
    tmp_path, workflow_session_factory, bound, pause_count
):
    factory = workflow_session_factory
    case, approval_id, source, commands = await _prepared_command(tmp_path, factory)
    async with factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        assert run is not None
        run.branch_name = None
        if not bound:
            await session.execute(
                update(RunCommand)
                .where(RunCommand.id == source.id)
                .values(status="PENDING", attempt_count=0, lease_owner=None, lease_expires_at=None)
            )
    if bound:
        async with PostgresUnitOfWork(factory) as work:
            await work.runs.bind_preparation_branch(
                case.run_id,
                source.expected_run_version,
                branch_name=f"forge/run/{case.run_id.hex}",
                event_type="run.preparation_branch_bound",
                event_payload={
                    "source_command_id": str(source.id),
                    "approval_id": str(approval_id),
                    "branch_name": f"forge/run/{case.run_id.hex}",
                },
                actor_class="worker",
                actor_id=source.actor_id,
            )
            await work.commit()
    provisioner = _PersistingProvisioner(factory, tmp_path / "worktree")
    git = None
    if isinstance(bound, str):
        async with PostgresUnitOfWork(factory) as work:
            approved = await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)
        git = _GitEffect()
        git.repository_path = Path(approved.policy.repository_path)
        provisioner = WorktreeProvisioner(
            lambda: PostgresUnitOfWork(factory),
            operations=PostgresOperationRepository(factory),
            git=git,
            database=_DisabledDatabase(),
        )
        await provisioner.prepare(case.run_id, approved.policy)
    service = DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    for index in range(pause_count):
        async with PostgresUnitOfWork(factory) as work:
            run = await work.runs.get(case.run_id)
        await commands.enqueue(
            run_id=case.run_id,
            command_type="pause",
            idempotency_key=f"prep-pause-{index}",
            payload={},
            expected_run_version=run.version,
            actor_id=uuid4(),
        )
        pause = await commands.claim_next(
            worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL
        )
        assert pause is not None
        async with PostgresUnitOfWork(factory) as work:
            await PauseRunHandler()(pause, work)
            paused = await work.runs.get(case.run_id)
        await commands.complete(pause.id, worker_id="control")
        async with factory() as session, session.begin():
            await session.execute(
                update(RunCommand)
                .where(RunCommand.id == source.id, RunCommand.status == "LEASED")
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        await commands.enqueue(
            run_id=case.run_id,
            command_type="resume",
            idempotency_key=f"prep-resume-{index}",
            payload={},
            expected_run_version=paused.version,
            actor_id=uuid4(),
        )
        resume = await commands.claim_next(worker_id="resume", lease_seconds=60)
        assert resume is not None and resume.command_type == "resume"
        if bound in {"missing_resource", "forged_branch", "approval_drift", "renewed_lease"} or (
            bound == "provenance" and index == 1
        ):
            before_status = (await commands.get(source.id)).status
            if bound == "missing_resource":
                git.handle = None
            elif bound == "forged_branch":
                async with factory() as session, session.begin():
                    event = await session.scalar(
                        select(RunEventRecord).where(
                            RunEventRecord.run_id == case.run_id,
                            RunEventRecord.event_type == "run.preparation_branch_bound",
                        )
                    )
                    assert event is not None
                    event.payload = {**event.payload, "source_command_id": str(uuid4())}
            elif bound == "approval_drift":
                async with factory() as session, session.begin():
                    approval = await session.get(Approval, approval_id)
                    assert approval is not None
                    approval.invalidated_at = datetime.now(UTC)
            elif bound == "provenance":
                async with factory() as session, session.begin():
                    event = await session.scalar(
                        select(RunEventRecord).where(
                            RunEventRecord.run_id == case.run_id,
                            RunEventRecord.event_type == "run.resumed",
                            RunEventRecord.run_version == source.expected_run_version,
                        )
                    )
                    assert event is not None
                    event.payload = {
                        **event.payload,
                        "continuation": {
                            **event.payload["continuation"],
                            "actor_id": str(uuid4()),
                        },
                    }
            else:
                async with factory() as session, session.begin():
                    await session.execute(
                        update(RunCommand)
                        .where(RunCommand.id == source.id)
                        .values(lease_expires_at=datetime.now(UTC) + timedelta(minutes=1))
                    )
            with pytest.raises(CommandRecoveryRequired):
                async with PostgresUnitOfWork(factory) as work:
                    await ResumeRunHandler(
                        artifact_store=case.artifact_store, preparation_inspector=provisioner
                    )(resume, work)
            assert (await commands.get(source.id)).status is before_status
            async with PostgresUnitOfWork(factory) as work:
                assert await work.runs.get(case.run_id) == paused
            assert git.create_calls == 1
            return
        async with PostgresUnitOfWork(factory) as work:
            handler = ResumeRunHandler(
                artifact_store=case.artifact_store,
                preparation_inspector=provisioner if git else None,
            )
            await handler(resume, work)
            await handler(resume, work)
            events = await work.events.list_for_version(case.run_id, paused.version + 1)
            event = next(event for event in events if event.event_type == "run.resumed")
        assert git.create_calls == 1 if git else provisioner.calls == 0
        assert (await commands.get(source.id)).status is CommandStatus.CANCELLED
        source = await commands.get(event.payload["continuation"]["command_id"])
        assert source.status is CommandStatus.PENDING
        await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="fresh", lease_seconds=60)
    assert fresh is not None and fresh.id == source.id
    async with PostgresUnitOfWork(factory) as work:
        await service.execute(fresh, work)
        await service.execute(fresh, work)
        run = await work.runs.get(case.run_id)
    assert git.create_calls == 1 if git else provisioner.calls == 1
    assert run.state is RunState.IMPLEMENTING
    implement = await commands.get_by_idempotency_key(f"{case.run_id}:implement:1")
    assert implement is not None and implement.actor_id == fresh.actor_id
