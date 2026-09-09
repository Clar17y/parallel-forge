"""A paused remote poll resumes with the same causal poll and fresh delivery."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.command import CommandStatus
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_pr_monitoring import published
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("mode", ["pending", "expired", "ready", "renewed", "corrupt_origin"])
async def test_paused_monitor_resumes_poll_without_duplicate_observation(
    tmp_path, workflow_session_factory, mode
):
    factory = workflow_session_factory
    case, git, read, writes, validator, source, policy, _ = await published(tmp_path, factory)
    result = await resumed_poll(case, source, factory, mode=mode)
    if result is None:
        return
    continued, _paused = result
    read.merge_protections[policy.github_repository.casefold(), "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )
    if mode == "ready":
        read.checks[policy.github_repository.casefold(), git.head] = (
            CheckSnapshot("ci", "completed", "success", head_sha=git.head),
        )
    monitor = ReleaseMonitor(case.artifact_store, validator, read, writes)
    if mode == "corrupt_origin":
        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.get(RunCommand, continued.id)
            row.payload = {**row.payload, "source_command_id": str(uuid4())}
            await work.commit()
            continued = await work.commands.get(continued.id)
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(CommandRecoveryRequired, match="monitor resume"):
                await monitor(continued, work)
            assert not any(
                e.event_type == "run.pr_observed"
                for e in await work.events.list_after(case.run_id, 0)
            )
        return
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await monitor(continued, work)
    async with PostgresUnitOfWork(factory) as work:
        assert (await work.runs.get(case.run_id)).state is (
            RunState.AWAITING_MERGE_APPROVAL if mode == "ready" else RunState.MONITORING_PR
        )
        assert (await work.commands.get(source.id)).status is CommandStatus.CANCELLED
        events = [
            e
            for e in await work.events.list_after(case.run_id, 0)
            if e.event_type == "run.pr_observed"
        ]
        assert len(events) == 1
        assert events[0].payload["source_command_id"] == str(continued.id)
        assert events[0].payload["poll"] == source.payload["poll"]


async def resumed_poll(case, source, factory, *, mode="expired"):
    commands = PostgresCommandRepository(factory)
    actor = uuid4()
    async with PostgresUnitOfWork(factory) as work:
        row = await work.session.get(RunCommand, source.id)
        row.available_at = datetime.now(UTC) + timedelta(hours=1)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if mode == "pending":
            row.status, row.attempt_count = "PENDING", 0
            row.lease_owner = row.lease_expires_at = None
        await work.commands.enqueue(
            run_id=case.run_id,
            command_type="pause",
            idempotency_key=f"{case.run_id}:pause",
            payload={},
            expected_run_version=(await work.runs.get(case.run_id)).version,
            actor_id=actor,
        )
        await work.commit()
    pause = await commands.claim_next(
        worker_id="control", lease_seconds=120, lane=CommandLane.CONTROL
    )
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
    await commands.complete(pause.id, worker_id=pause.lease_owner)
    async with PostgresUnitOfWork(factory) as work:
        paused = await work.runs.get(case.run_id)
        await work.commands.enqueue(
            run_id=case.run_id,
            command_type="resume",
            idempotency_key=f"{case.run_id}:resume",
            payload={},
            expected_run_version=paused.version,
            actor_id=actor,
        )
        await work.commit()
    resume = await commands.claim_next(worker_id="resume", lease_seconds=120)
    assert resume.command_type == "resume"
    if mode == "renewed":
        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.get(RunCommand, source.id)
            row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
            await work.commit()
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(CommandRecoveryRequired, match="lease is active"):
                await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.runs.get(case.run_id)).state is RunState.PAUSED
            assert (await work.commands.get(source.id)).status is CommandStatus.LEASED
        return
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
    await commands.complete(resume.id, worker_id=resume.lease_owner)
    if mode == "ready_observed":
        return None, paused
    continued = await commands.claim_next(worker_id="continued", lease_seconds=120)
    assert continued.command_type == "monitor_pr"
    assert continued.id != source.id
    assert continued.actor_id == source.actor_id
    assert continued.expected_run_version == paused.version + 1
    return continued, paused
