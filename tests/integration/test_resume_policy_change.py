"""Discriminating tests for terminal refusal of resume when project policy changes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.runs import (
    RunCommandRequest,
    RunCommandService,
    StaleProjectPolicyConflict,
    preparation_resume_policy_conflict_reason,
)
from forge.application.services.worker import Worker
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import Project, Run, RunCommand, Task
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select, update
from test_delivery_preparation import _PersistingProvisioner, _prepared_command
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _bump_project_policy(factory, project_id) -> int:
    async with PostgresUnitOfWork(factory) as work:
        project = await work.projects.get(project_id, for_update=True)
        assert project.policy is not None
        assert project.current_policy_version is not None
        current_version = project.current_policy_version
        doc = dict(project.policy.document)
        doc["version"] = current_version + 1
        encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        policy_digest = hashlib.sha256(encoded).hexdigest()
        new_record = await work.projects.append_policy(
            project_id=project_id,
            expected_policy_version=current_version,
            policy_digest=policy_digest,
            policy_document=doc,
        )
        await work.commit()
        return new_record.version


async def test_queued_resume_of_stale_policy_paused_preparation_fails_terminally(
    tmp_path, workflow_session_factory
):
    factory = workflow_session_factory
    case, _approval_id, source, commands = await _prepared_command(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)

    # Pause the run
    await commands.enqueue(
        run_id=case.run_id,
        command_type="pause",
        idempotency_key="prep-pause-stale-test",
        payload={},
        expected_run_version=run.version,
        actor_id=uuid4(),
    )
    pause = await commands.claim_next(worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL)
    assert pause is not None
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
        paused = await work.runs.get(case.run_id)
    await commands.complete(pause.id, worker_id="control")

    # Expire source prepare command lease
    async with factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id, RunCommand.status == "LEASED")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    # Bump project policy version so project.current_policy_version != run.policy_version
    new_policy_version = await _bump_project_policy(factory, paused.project_id)
    assert paused.policy_version is not None
    assert new_policy_version != paused.policy_version

    # Enqueue TWO resume commands (e.g. duplicate operator clicks)
    resume1 = await commands.enqueue(
        run_id=case.run_id,
        command_type="resume",
        idempotency_key="queued-resume-1",
        payload={},
        expected_run_version=paused.version,
        actor_id=uuid4(),
    )
    resume2 = await commands.enqueue(
        run_id=case.run_id,
        command_type="resume",
        idempotency_key="queued-resume-2",
        payload={},
        expected_run_version=paused.version,
        actor_id=uuid4(),
    )

    provisioner = _PersistingProvisioner(factory, tmp_path / "worktree")
    worker = Worker(
        commands,
        factory,
        handlers={
            "resume": ResumeRunHandler(
                artifact_store=case.artifact_store,
                preparation_inspector=provisioner,
            )
        },
        worker_id="test-worker",
        lease_seconds=30,
    )

    source_before = await commands.get(source.id)

    # Tick 1: processes resume1
    result1 = await worker.tick()
    assert result1 is False
    cmd1 = await commands.get(resume1.id)
    assert cmd1.status is CommandStatus.FAILED
    assert cmd1.attempt == 1 and cmd1.lease_expires_at is None
    assert cmd1.error_summary is not None
    assert "policy" in cmd1.error_summary.lower()

    # Tick 2: processes resume2
    result2 = await worker.tick()
    assert result2 is False
    cmd2 = await commands.get(resume2.id)
    assert cmd2.status is CommandStatus.FAILED
    assert cmd2.attempt == 1 and cmd2.lease_expires_at is None
    assert cmd2.error_summary is not None
    assert "policy" in cmd2.error_summary.lower()

    # Tick 3: queue is idle
    result3 = await worker.tick()
    assert result3 is None

    # Terminal commands have no lease to expire and remain ineligible for delivery.
    next_claim = await commands.claim_next(worker_id="test-worker", lease_seconds=30)
    assert next_claim is None

    # Run state must NOT transition
    async with PostgresUnitOfWork(factory) as work:
        current_run = await work.runs.get(case.run_id)
        events = await work.events.list_after(case.run_id, 0)
    assert current_run.state is RunState.PAUSED
    assert current_run.version == paused.version

    # Source command must NOT be settled or mutated
    source_after = await commands.get(source.id)
    assert source_after == source_before

    # No worktree provisioning calls
    assert provisioner.calls == 0

    # No run.resumed event
    assert not any(event.event_type == "run.resumed" for event in events)


async def test_api_enqueue_refuses_stale_policy_paused_preparation(
    tmp_path, workflow_session_factory
):
    factory = workflow_session_factory
    case, _approval_id, _source, commands = await _prepared_command(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)

    # Pause the run
    await commands.enqueue(
        run_id=case.run_id,
        command_type="pause",
        idempotency_key="prep-pause-api-test",
        payload={},
        expected_run_version=run.version,
        actor_id=uuid4(),
    )
    pause = await commands.claim_next(worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL)
    assert pause is not None
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
        paused = await work.runs.get(case.run_id)
    await commands.complete(pause.id, worker_id="control")

    # Bump project policy
    await _bump_project_policy(factory, paused.project_id)

    service = RunCommandService(lambda: PostgresUnitOfWork(factory))
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())

    with pytest.raises(StaleProjectPolicyConflict) as exc_info:
        await service.enqueue(
            actor=actor,
            run_id=case.run_id,
            idempotency_key="api-resume-key-1",
            request=RunCommandRequest(command_type="resume", expected_run_version=paused.version),
        )
    assert "policy" in str(exc_info.value).lower()

    # Nothing should be enqueued
    queued_cmd = await commands.get_by_idempotency_key(
        f"run-command:commands:{actor.actor_id}:{case.run_id}:{hashlib.sha256(b'api-resume-key-1').hexdigest()}"
    )
    assert queued_cmd is None


async def test_same_policy_preparation_resume_continues_to_succeed(
    tmp_path, workflow_session_factory
):
    factory = workflow_session_factory
    case, _approval_id, source, commands = await _prepared_command(tmp_path, factory)
    async with factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        assert run is not None
        run.branch_name = None
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id)
            .values(status="PENDING", attempt_count=0, lease_owner=None, lease_expires_at=None)
        )
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)

    # Pause the run
    await commands.enqueue(
        run_id=case.run_id,
        command_type="pause",
        idempotency_key="prep-pause-same-policy",
        payload={},
        expected_run_version=run.version,
        actor_id=uuid4(),
    )
    pause = await commands.claim_next(worker_id="control", lease_seconds=60, lane=CommandLane.CONTROL)
    assert pause is not None
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
        paused = await work.runs.get(case.run_id)
    await commands.complete(pause.id, worker_id="control")

    # Expire source prepare command lease
    async with factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == source.id, RunCommand.status == "LEASED")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    # Enqueue resume without policy change
    resume = await commands.enqueue(
        run_id=case.run_id,
        command_type="resume",
        idempotency_key="same-policy-resume-1",
        payload={},
        expected_run_version=paused.version,
        actor_id=uuid4(),
    )

    provisioner = _PersistingProvisioner(factory, tmp_path / "worktree")
    worker = Worker(
        commands,
        factory,
        handlers={
            "resume": ResumeRunHandler(
                artifact_store=case.artifact_store,
                preparation_inspector=provisioner,
            )
        },
        worker_id="test-worker",
        lease_seconds=30,
    )

    result = await worker.tick()
    assert result is True
    resumed_cmd = await commands.get(resume.id)
    assert resumed_cmd.status is CommandStatus.COMPLETED

    async with PostgresUnitOfWork(factory) as work:
        resumed_run = await work.runs.get(case.run_id)
        events = await work.events.list_after(case.run_id, 0)
    assert resumed_run.state is RunState.PREPARING_WORKTREE
    assert resumed_run.version == paused.version + 1
    assert any(event.event_type == "run.resumed" for event in events)


async def test_resume_policy_check_does_not_block_a_task_owner_from_locking_project(
    tmp_path, workflow_session_factory, monkeypatch
):
    factory = workflow_session_factory
    case, _approval_id, _source, _commands = await _prepared_command(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
    paused = replace(run, state=RunState.PAUSED, suspended_state=RunState.PREPARING_WORKTREE)

    async with PostgresUnitOfWork(factory) as resume_work:
        reached_lock = asyncio.Event()
        get_task = resume_work.tasks.get
        get_project = resume_work.projects.get

        async def observed_task_get(*args, **kwargs):
            reached_lock.set()
            return await get_task(*args, **kwargs)

        async def observed_project_get(*args, **kwargs):
            project = await get_project(*args, **kwargs)
            reached_lock.set()
            return project

        monkeypatch.setattr(resume_work.tasks, "get", observed_task_get)
        monkeypatch.setattr(resume_work.projects, "get", observed_project_get)
        checking = None
        try:
            async with factory() as concurrent, concurrent.begin():
                # Creation and approval loading take the task before the project.
                await concurrent.execute(
                    select(Task).where(Task.id == paused.task_id).with_for_update()
                )
                checking = asyncio.create_task(
                    preparation_resume_policy_conflict_reason(resume_work, paused)
                )
                await asyncio.wait_for(reached_lock.wait(), timeout=5)
                # The resume must wait for our task, without holding our next lock.
                await concurrent.execute(
                    select(Project)
                    .where(Project.id == paused.project_id)
                    .with_for_update(nowait=True)
                )
            assert await asyncio.wait_for(checking, timeout=5) is None
        finally:
            if checking is not None:
                checking.cancel()
                await asyncio.gather(checking, return_exceptions=True)
