"""Cancellation boundaries for admitted repository writes."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self, cast

import forge.application.services.tools as tools_service
import pytest
from forge.application.services.tools import ToolInvocationError, _await_committed_write
from forge.domain.tool import ToolName, ToolRequest
from forge.persistence.models import ArtifactLineage, OperationIntent, RunEvent, ToolCall
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.orchestrator.tests.persistence.test_tool_write_invocation import (
    _ControlledArtifactStore,
    _ControlledWriter,
    _seed_test_database,
    _setup_service,
)


@pytest.mark.asyncio
async def test_committed_write_waits_for_owned_completion_after_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation must not cancel an already admitted write owner."""

    entered = asyncio.Event()
    release = asyncio.Event()
    first_cancellation_handled = asyncio.Event()
    original_shield = asyncio.shield
    shield_calls = 0

    def observed_shield(task: Any) -> Any:
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls == 2:
            first_cancellation_handled.set()
        return original_shield(task)

    monkeypatch.setattr(tools_service.asyncio, "shield", observed_shield)

    async def complete_write() -> object:
        entered.set()
        await release.wait()
        return object()

    completion = asyncio.create_task(complete_write())
    caller = asyncio.create_task(_await_committed_write(cast(Any, completion)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        caller.cancel()
        await asyncio.wait_for(first_cancellation_handled.wait(), timeout=1.0)
        assert caller.done() is False
        assert caller.cancelling() == 1

        # Deliver a distinct second cancellation only after the first one has
        # been caught and the join has resumed waiting for the owned task.
        caller.cancel()
        await asyncio.sleep(0)
        assert caller.cancelling() == 2
        assert caller.done() is False
        assert completion.done() is False
        assert completion.cancelled() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=1.0)
        assert completion.done() is True
        assert completion.cancelled() is False
    finally:
        release.set()
        for task in (caller, completion):
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(original_shield(task), timeout=1.0)


@pytest.mark.asyncio
async def test_committed_write_join_does_not_loop_when_child_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External shutdown of the owned child must terminate the shielded join."""

    completion = asyncio.create_task(asyncio.sleep(60))
    completion.cancel()

    shield = tools_service.asyncio.shield
    calls = 0

    def bounded_shield(task: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise AssertionError("join retried a completed cancelled child")
        return shield(task)

    monkeypatch.setattr(tools_service.asyncio, "shield", bounded_shield)
    join = asyncio.create_task(_await_committed_write(cast(Any, completion)))
    with pytest.raises(asyncio.CancelledError):
        await join
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_wait_retrieves_child_failure_before_propagating_cancellation() -> None:
    """A failed owned task is observed even when the caller has already cancelled."""

    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_write() -> object:
        entered.set()
        await release.wait()
        raise RuntimeError("durable finalization failed")

    completion = asyncio.create_task(fail_write())
    caller = asyncio.create_task(_await_committed_write(cast(Any, completion)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        caller.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=1.0)
        assert isinstance(completion.exception(), RuntimeError)
    finally:
        release.set()
        for task in (caller, completion):
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)


async def test_cancellation_before_admission_has_no_write_effect(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Cancellation before the invocation starts cannot commit admission or run the writer."""

    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch_name,
        repo_path,
        worktree_path,
        _policy,
    ) = await _seed_test_database(session_factory, tmp_path)
    writer = _ControlledWriter(Path(worktree_path))
    reservation_entered = asyncio.Event()
    allow_reservation = asyncio.Event()

    class _BlockedAdmissionUoW(PostgresUnitOfWork):
        async def __aenter__(self) -> Self:
            work = await super().__aenter__()
            reserve = work.tool_calls.reserve

            async def blocked_reserve(record: Any) -> Any:
                reservation_entered.set()
                await allow_reservation.wait()
                return await reserve(record)

            work.tool_calls.reserve = blocked_reserve  # type: ignore[method-assign]
            return work

    def uow_factory() -> PostgresUnitOfWork:
        return _BlockedAdmissionUoW(session_factory)

    service, base_context, _ = _setup_service(
        session_factory,
        project_id,
        run_id,
        branch_name,
        base_sha,
        repo_path,
        worktree_path,
        writer,
        _ControlledArtifactStore(tmp_path / "artifacts"),
        custom_uow_factory=uow_factory,
    )
    caller = asyncio.create_task(
        service.invoke(
            replace(base_context, step_id=step_id, agent_execution_id=execution_id),
            ToolRequest(
                name=ToolName.REPOSITORY_WRITE_FILE, arguments={"path": "nope.py", "content": "x"}
            ),
        )
    )
    try:
        await asyncio.wait_for(reservation_entered.wait(), timeout=2.0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=2.0)
    finally:
        allow_reservation.set()
        if not caller.done():
            caller.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(caller), timeout=2.0)
    async with session_factory() as session:
        assert (await session.execute(select(ToolCall))).scalars().all() == []
        assert (await session.execute(select(OperationIntent))).scalars().all() == []
    assert writer.call_count == 0


async def test_postcommit_exit_cancellation_observes_completed_write_without_deadlock(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A committed reservation starts its owner before cancellable UoW cleanup."""

    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch_name,
        repo_path,
        worktree_path,
        _policy,
    ) = await _seed_test_database(session_factory, tmp_path)
    writer = _ControlledWriter(Path(worktree_path))
    postcommit_exit_entered = asyncio.Event()
    release_postcommit_exit = asyncio.Event()
    uow_calls = 0

    class _BlockedPostCommitExitUoW(PostgresUnitOfWork):
        async def __aexit__(self, *args: object) -> None:
            if self._committed:
                postcommit_exit_entered.set()
                try:
                    await release_postcommit_exit.wait()
                finally:
                    await super().__aexit__(*args)
                return
            await super().__aexit__(*args)

    def uow_factory() -> PostgresUnitOfWork:
        nonlocal uow_calls
        uow_calls += 1
        if uow_calls == 1:
            return _BlockedPostCommitExitUoW(session_factory)
        return PostgresUnitOfWork(session_factory)

    service, base_context, _ = _setup_service(
        session_factory,
        project_id,
        run_id,
        branch_name,
        base_sha,
        repo_path,
        worktree_path,
        writer,
        _ControlledArtifactStore(tmp_path / "artifacts"),
        custom_uow_factory=uow_factory,
    )
    caller = asyncio.create_task(
        service.invoke(
            replace(base_context, step_id=step_id, agent_execution_id=execution_id),
            ToolRequest(
                name=ToolName.REPOSITORY_WRITE_FILE,
                arguments={"path": "src/postcommit.py", "content": "value = 1\n"},
            ),
        )
    )
    try:
        await asyncio.wait_for(postcommit_exit_entered.wait(), timeout=2.0)
        assert writer.call_count == 1
        async with session_factory() as session:
            call = (await session.execute(select(ToolCall))).scalar_one()
            intent = (await session.execute(select(OperationIntent))).scalar_one()
        assert call.status == "SUCCEEDED"
        assert intent.status == "SUCCEEDED"

        caller.cancel()
        await asyncio.sleep(0)
        release_postcommit_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=5.0)
    finally:
        release_postcommit_exit.set()
        if not caller.done():
            caller.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(caller), timeout=5.0)

    assert writer.call_count == 1
    async with session_factory() as session:
        call = (await session.execute(select(ToolCall))).scalar_one()
        intent = (await session.execute(select(OperationIntent))).scalar_one()
        artifacts = (await session.execute(select(ArtifactLineage))).scalars().all()
        events = (
            (
                await session.execute(
                    select(RunEvent).where(RunEvent.event_type == "tool_call.completed")
                )
            )
            .scalars()
            .all()
        )
    assert call.status == "SUCCEEDED"
    assert intent.status == "SUCCEEDED"
    assert len(artifacts) == 1
    assert len(events) == 1


async def test_cancellation_during_commit_settlement_finishes_admitted_write(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Durable admission owns settlement through effect and terminal audit."""

    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch_name,
        repo_path,
        worktree_path,
        _policy,
    ) = await _seed_test_database(session_factory, tmp_path)
    writer = _ControlledWriter(Path(worktree_path))
    writer.write_started_event = threading.Event()
    writer.write_proceed_event = threading.Event()
    settlement_entered = asyncio.Event()
    release_settlement = asyncio.Event()
    uow_calls = 0

    class _BlockedSettlementUoW(PostgresUnitOfWork):
        async def commit(self) -> None:
            await super().commit()
            settlement_entered.set()
            await release_settlement.wait()

    def uow_factory() -> PostgresUnitOfWork:
        nonlocal uow_calls
        uow_calls += 1
        if uow_calls == 1:
            return _BlockedSettlementUoW(session_factory)
        return PostgresUnitOfWork(session_factory)

    service, base_context, _ = _setup_service(
        session_factory,
        project_id,
        run_id,
        branch_name,
        base_sha,
        repo_path,
        worktree_path,
        writer,
        _ControlledArtifactStore(tmp_path / "artifacts"),
        custom_uow_factory=uow_factory,
    )
    caller = asyncio.create_task(
        service.invoke(
            replace(base_context, step_id=step_id, agent_execution_id=execution_id),
            ToolRequest(
                name=ToolName.REPOSITORY_WRITE_FILE,
                arguments={"path": "src/settled.py", "content": "value = 1\n"},
            ),
        )
    )
    try:
        await asyncio.wait_for(settlement_entered.wait(), timeout=2.0)
        async with session_factory() as session:
            assert (await session.execute(select(ToolCall))).scalar_one().status == "RUNNING"
        caller.cancel()
        await asyncio.sleep(0)
        assert caller.done() is False
        release_settlement.set()
        assert await asyncio.to_thread(writer.write_started_event.wait, 2.0)
        writer.write_proceed_event.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=5.0)
    finally:
        release_settlement.set()
        writer.write_proceed_event.set()
        if not caller.done():
            caller.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(caller), timeout=5.0)

    assert writer.call_count == 1
    async with session_factory() as session:
        call = (await session.execute(select(ToolCall))).scalar_one()
        intent = (await session.execute(select(OperationIntent))).scalar_one()
        artifacts = (await session.execute(select(ArtifactLineage))).scalars().all()
        events = (
            (
                await session.execute(
                    select(RunEvent).where(RunEvent.event_type == "tool_call.completed")
                )
            )
            .scalars()
            .all()
        )
    assert call.status == "SUCCEEDED"
    assert intent.status == "SUCCEEDED"
    assert len(artifacts) == 1
    assert len(events) == 1


async def test_admission_commit_failure_does_not_execute_write(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A failed admission settlement does not start the repository effect."""

    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch_name,
        repo_path,
        worktree_path,
        _policy,
    ) = await _seed_test_database(session_factory, tmp_path)
    writer = _ControlledWriter(Path(worktree_path))

    class _FailingAdmissionUoW(PostgresUnitOfWork):
        async def commit(self) -> None:
            raise RuntimeError("admission commit failed")

    service, base_context, _ = _setup_service(
        session_factory,
        project_id,
        run_id,
        branch_name,
        base_sha,
        repo_path,
        worktree_path,
        writer,
        _ControlledArtifactStore(tmp_path / "artifacts"),
        custom_uow_factory=lambda: _FailingAdmissionUoW(session_factory),
    )
    with pytest.raises(ToolInvocationError):
        await service.invoke(
            replace(base_context, step_id=step_id, agent_execution_id=execution_id),
            ToolRequest(
                name=ToolName.REPOSITORY_WRITE_FILE,
                arguments={"path": "src/failed.py", "content": "value = 1\n"},
            ),
        )
    assert writer.call_count == 0


@pytest.mark.parametrize(
    ("artifact_verified", "terminal_commit_fails"),
    [(True, False), (False, False), (True, True)],
)
async def test_cancelled_admitted_write_keeps_executor_owner_until_terminal_audit(
    artifact_verified: bool,
    terminal_commit_fails: bool,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A blocked real writer remains owned until its effect and audit commit finish."""

    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch_name,
        repo_path,
        worktree_path,
        _policy,
    ) = await _seed_test_database(session_factory, tmp_path)
    writer = _ControlledWriter(Path(worktree_path))
    writer.write_started_event = threading.Event()
    writer.write_proceed_event = threading.Event()
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
    artifact_store.verify_returns = artifact_verified
    uow_calls = 0

    class _FailTerminalCommitUoW(PostgresUnitOfWork):
        async def commit(self) -> None:
            raise RuntimeError("terminal audit commit failed")

    def uow_factory() -> PostgresUnitOfWork:
        nonlocal uow_calls
        uow_calls += 1
        if terminal_commit_fails and uow_calls > 1:
            return _FailTerminalCommitUoW(session_factory)
        return PostgresUnitOfWork(session_factory)

    service, base_context, _ = _setup_service(
        session_factory,
        project_id,
        run_id,
        branch_name,
        base_sha,
        repo_path,
        worktree_path,
        writer,
        artifact_store,
        custom_uow_factory=uow_factory,
    )
    renewed = asyncio.Event()
    renewed_intent: OperationIntent | None = None
    executor = service._operation_executor
    assert executor is not None
    executor._execution_lease_seconds = 3  # type: ignore[attr-defined]
    operations = executor._operations  # type: ignore[attr-defined]
    renew_execution = operations.renew_execution

    async def record_renewal(*args: Any, **kwargs: Any) -> Any:
        nonlocal renewed_intent
        renewed_intent = await renew_execution(*args, **kwargs)
        renewed.set()
        return renewed_intent

    operations.renew_execution = record_renewal
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "src/cancelled.py", "content": "value = 1\n"},
    )

    caller = asyncio.create_task(service.invoke(context, request))
    try:
        assert await asyncio.to_thread(writer.write_started_event.wait, 5.0)
        # Admission uses the production 30-second lease. Shorten the persisted
        # lease while preserving its owner so a prompt real renewal can be
        # observed as an extension without a timing-sensitive long wait.
        async with session_factory() as session, session.begin():
            shortened_expiry = datetime.now(UTC) + timedelta(seconds=2)
            await session.execute(
                update(OperationIntent).values(execution_lease_expires_at=shortened_expiry)
            )
        async with session_factory() as session:
            initial_intent = (await session.execute(select(OperationIntent))).scalar_one()
            initial_owner = initial_intent.execution_owner
            initial_expiry = initial_intent.execution_lease_expires_at
        assert initial_owner is not None
        assert initial_expiry is not None

        caller.cancel()
        await asyncio.sleep(0)
        assert caller.done() is False
        caller.cancel()
        await asyncio.sleep(0)
        caller_pending_while_writer_blocked = caller.done() is False
        assert writer.call_count == 0
        await asyncio.wait_for(renewed.wait(), timeout=2.0)
        assert renewed_intent is not None
        assert renewed_intent.execution_owner == initial_owner
        assert renewed_intent.execution_lease_expires_at is not None
        assert renewed_intent.execution_lease_expires_at > initial_expiry

        async with session_factory() as session:
            call = (await session.execute(select(ToolCall))).scalar_one()
            intent = (await session.execute(select(OperationIntent))).scalar_one()
            observed_running = call.status == "RUNNING"
            observed_pending_owner = (
                intent.status == "PENDING"
                and intent.execution_owner == initial_owner
                and intent.execution_lease_expires_at == renewed_intent.execution_lease_expires_at
            )

        writer.write_proceed_event.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=5.0)
    finally:
        writer.write_proceed_event.set()
        if not caller.done():
            caller.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(caller), timeout=5.0)

    assert writer.call_count == 1
    assert caller_pending_while_writer_blocked
    assert observed_running
    assert observed_pending_owner
    async with session_factory() as session:
        call = (await session.execute(select(ToolCall))).scalar_one()
        intent = (await session.execute(select(OperationIntent))).scalar_one()
        lineages = (await session.execute(select(ArtifactLineage))).scalars().all()
        events = (
            (
                await session.execute(
                    select(RunEvent).where(RunEvent.event_type == "tool_call.completed")
                )
            )
            .scalars()
            .all()
        )
        assert call.status == (
            "SUCCEEDED" if artifact_verified and not terminal_commit_fails else "RUNNING"
        )
        assert intent.status == "SUCCEEDED"
        assert len(lineages) == (1 if artifact_verified and not terminal_commit_fails else 0)
        assert len(events) == (1 if artifact_verified and not terminal_commit_fails else 0)
