"""Durable delivery preparation service test coverage for setup failure settlement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.worktrees import WorktreeSetupFailed
from forge.application.services.worker import Worker
from forge.domain.command import CommandStatus
from forge.domain.run import RunState, SuspensionKind
from forge.persistence.models import Run, RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import update
from test_subscription_preparation import (
    _remove_disposable_subscription_rows,  # noqa: F401
    preparation_case,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _make_setup_failure(
    *, exit_code: int | None = 1, timed_out: bool = False
) -> WorktreeSetupFailed:
    return WorktreeSetupFailed(
        "worktree setup command failed",
        failure={
            "operation_intent_id": str(uuid4()),
            "ordinal": 0,
            "kind": "install",
            "command_name": "install-deps",
            "command_digest": "a" * 64,
            "evidence_digest": "b" * 64,
            "policy_version": 1,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "started_at": "2026-09-28T12:00:00+00:00",
            "duration_ms": 1234,
            "stdout_digest": "c" * 64,
            "stderr_digest": "d" * 64,
            "runner_mode": "trusted_host",
            "image_digest": None,
            "network_enabled": True,
            "stdout_original_byte_count": 50,
            "stderr_original_byte_count": 100,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "unsandboxed": True,
        },
    )


class _FailingProvisioner:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls = 0

    async def prepare(self, run_id, policy):
        self.calls += 1
        raise self.failure


@pytest.mark.parametrize(("exit_code", "timed_out"), [(1, False), (None, True)])
async def test_known_setup_failure_places_run_in_intervention(
    session_factory, tmp_path, exit_code, timed_out
) -> None:
    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    failure = _make_setup_failure(exit_code=exit_code, timed_out=timed_out)
    failing_provisioner = _FailingProvisioner(failure)
    service._provisioner = failing_provisioner

    async with factory() as work:
        approved = await service._approved_plans.load(work, evidence.producer.run_id)
        await service.execute(command, work)

    assert failing_provisioner.calls == 1

    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        assert run.suspended_state is RunState.PREPARING_WORKTREE
        assert run.suspension_kind is SuspensionKind.INTERVENTION

        events = [
            e
            for e in await work.events.list_after(run.id, 0)
            if e.event_type == "run.preparation_failed"
        ]
        assert len(events) == 1
        event = events[0]
        assert event.actor_class == "worker"
        assert event.payload["source_command_id"] == str(command.id)
        assert event.payload["approval_id"] == str(approved.approval_id)
        assert event.payload["reason"] == "setup_command_failed"
        assert event.payload["command_name"] == "install-deps"
        assert event.payload["exit_code"] == exit_code
        assert event.payload["timed_out"] is timed_out
        assert event.payload["stdout_digest"] == "c" * 64
        assert event.payload["stderr_digest"] == "d" * 64
        assert "stderr" not in event.payload
        assert "environment" not in event.payload

        # Ensure no implement command was enqueued
        implement = await work.commands.get_by_idempotency_key(f"{run.id}:implement:1")
        assert implement is None


async def test_replaying_setup_failure_is_idempotent_and_does_not_loop(
    session_factory, tmp_path
) -> None:
    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    failure = _make_setup_failure(exit_code=1)
    failing_provisioner = _FailingProvisioner(failure)
    service._provisioner = failing_provisioner

    async with factory() as work:
        await service.execute(command, work)

    assert failing_provisioner.calls == 1

    # Replay the command delivery: should settle cleanly without re-executing prepare
    async with factory() as work:
        await service.execute(command, work)

    assert failing_provisioner.calls == 1

    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        events = [
            e
            for e in await work.events.list_after(run.id, 0)
            if e.event_type == "run.preparation_failed"
        ]
        assert len(events) == 1


@pytest.mark.parametrize("control_type", ["pause", "cancel"])
async def test_pending_control_stop_fences_failure_publication(
    session_factory, tmp_path, control_type
) -> None:
    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    failure = _make_setup_failure(exit_code=1)

    class _ControlStoppingProvisioner:
        def __init__(self, failure: Exception) -> None:
            self.failure = failure

        async def prepare(self, run_id, policy):
            async with session_factory() as session, session.begin():
                run = await session.get(Run, run_id)
                assert run is not None
                version = run.version
            commands = PostgresCommandRepository(session_factory)
            await commands.enqueue(
                run_id=run_id,
                command_type=control_type,
                idempotency_key=f"{run_id}:{control_type}:{version}",
                payload={},
                expected_run_version=version,
                actor_id=uuid4(),
            )
            raise self.failure

    service._provisioner = _ControlStoppingProvisioner(failure)

    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired, match="fenced by operator control"):
            await service.execute(command, work)

    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.PREPARING_WORKTREE
        events = [
            e
            for e in await work.events.list_after(run.id, 0)
            if e.event_type == "run.preparation_failed"
        ]
        assert len(events) == 0


async def test_stale_lease_fences_failure_publication(session_factory, tmp_path) -> None:
    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    failure = _make_setup_failure(exit_code=1)

    class _LeaseExpiringProvisioner:
        def __init__(self, failure: Exception) -> None:
            self.failure = failure

        async def prepare(self, run_id, policy):
            # Transfer the lease so the current delivery no longer owns it.
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(RunCommand)
                    .where(RunCommand.id == command.id)
                    .values(lease_owner="another-worker")
                )
            raise self.failure

    service._provisioner = _LeaseExpiringProvisioner(failure)

    async with factory() as work:
        with pytest.raises(CommandLeaseLost):
            await service.execute(command, work)

    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.PREPARING_WORKTREE
        events = [
            e
            for e in await work.events.list_after(evidence.producer.run_id, 0)
            if e.event_type == "run.preparation_failed"
        ]
        assert len(events) == 0


async def test_worker_completes_failed_setup_delivery_and_cannot_reclaim_it(
    session_factory, tmp_path
) -> None:
    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    provisioner = _FailingProvisioner(_make_setup_failure())
    service._provisioner = provisioner
    async with session_factory() as session, session.begin():
        await session.execute(
            update(RunCommand)
            .where(RunCommand.id == command.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    commands = PostgresCommandRepository(session_factory)
    worker = Worker(
        commands,
        session_factory,
        handlers={"prepare_worktree": service.execute},
        worker_id="recovery-test-worker",
    )
    assert await worker.tick() is True
    settled = await commands.get(command.id)
    assert settled.status is CommandStatus.COMPLETED
    assert await worker.tick() is None
    assert provisioner.calls == 1
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        events = await work.events.list_after(run.id, 0)
        assert sum(event.event_type == "run.preparation_failed" for event in events) == 1


async def test_unknown_provisioner_error_requires_recovery(session_factory, tmp_path) -> None:
    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    service._provisioner = _FailingProvisioner(RuntimeError("unexpected disk error"))

    async with factory() as work:
        with pytest.raises(
            CommandRecoveryRequired, match="worktree provisioning requires recovery"
        ):
            await service.execute(command, work)

    async with factory() as work:
        events = [
            e
            for e in await work.events.list_after(evidence.producer.run_id, 0)
            if e.event_type == "run.preparation_failed"
        ]
        assert len(events) == 0
