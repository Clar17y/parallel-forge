"""Named-check service admission must precede command launch."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from forge.application.ports.runner import CommandResult, CommandTerminalResult
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.recovery import OperationExecutor
from forge.application.services.tools import ControlledToolService
from forge.domain.actor import AgentRole
from forge.domain.operation import OperationStatus
from forge.domain.policy import CommandSpec, RunnerMode, StepKind
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.tool import ToolAuthorizationContext, ToolCallStatus, ToolName, ToolRequest
from forge.domain.validation import command_spec_digest
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork

from apps.orchestrator.tests.application.test_tool_git_commit import _CommitGit
from apps.orchestrator.tests.persistence.test_tool_write_invocation import (
    _ControlledArtifactStore,
    _seed_test_database,
)


async def _named_case(session_factory, tmp_path, *, exit_code=0, timed_out=False):
    command = CommandSpec(name="unit", kind=StepKind.TEST, argv=("pytest",), timeout_seconds=30)
    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch,
        _,
        path,
        policy,
    ) = await _seed_test_database(session_factory, tmp_path, commands=(command,))
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = ManagedWorktree(identity=identity, path=Path(path), base_sha=base_sha)
    store = _ControlledArtifactStore(tmp_path / "artifacts")
    call_id = UUID("f1111111-1111-4111-8111-111111111111")

    class Factory:
        calls = 0

        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.release.set()
            self.cancel_received = asyncio.Event()

        def create(self, actual_worktree, actual_policy):
            assert actual_worktree == worktree
            assert actual_policy.model_dump() == {
                **policy.model_dump(),
                "github_repository": policy.github_repository.casefold(),
            }
            return self

        async def run_terminal(self, request):
            self.calls += 1
            async with PostgresUnitOfWork(session_factory) as work:
                reserved = await work.tool_calls.get(call_id)
                assert reserved.status is ToolCallStatus.RUNNING
                admitted = await work.operations.get(reserved.operation_intent_id)
                assert admitted.status is OperationStatus.PENDING
                assert admitted.request_payload["command_digest"] == command_spec_digest(command)
            assert request.command_name == "unit"
            assert request.kind is StepKind.TEST
            assert dict(request.environment) == {}
            if request.launch_ownership is not None:
                request.launch_ownership.accept_launch()
            self.entered.set()
            cancelled = False
            while True:
                try:
                    await self.release.wait()
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    self.cancel_received.set()
            digests = []
            for stream in ("stdout", "stderr"):
                data = json.dumps(
                    {
                        "stream": stream,
                        "encoding": "utf-8-replacement",
                        "text": "",
                        "captured_byte_count": 0,
                        "original_byte_count": 0,
                        "truncated": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                descriptor = await store.put_bytes(
                    data, media_type="application/vnd.forge.command-output+json"
                )
                digests.append(descriptor.digest)
            return CommandTerminalResult(
                result=CommandResult(
                    command_name="unit",
                    kind=StepKind.TEST,
                    command_digest=command_spec_digest(command),
                    policy_version=1,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    started_at=datetime.now(UTC),
                    duration_ms=1,
                    stdout_digest=digests[0],
                    stderr_digest=digests[1],
                    runner_mode=RunnerMode.DOCKER,
                    image_digest="sha256:" + "d" * 64,
                    network_enabled=False,
                    stdout_original_byte_count=0,
                    stderr_original_byte_count=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                    unsandboxed=False,
                ),
                caller_cancelled=cancelled,
            )

    factory = Factory()

    class Git(_CommitGit):
        repository_path = Path(policy.repository_path)

        def expected_worktree(self, actual_identity, actual_base):
            assert actual_identity == identity
            assert actual_base == base_sha
            return worktree

    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory),
        controlled_git=Git(worktree),
        worktree=worktree,
        artifact_store=store,
        runner_factory=factory,
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
    )
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        agent_execution_id=execution_id,
        step_id=step_id,
        invocation_id=call_id,
    )
    request = ToolRequest(name=ToolName.BUILD_RUN_NAMED_CHECK, arguments={"command_name": "unit"})
    return SimpleNamespace(
        service=service, context=context, request=request, factory=factory, store=store
    )


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "expected"),
    [
        (0, False, ToolCallStatus.SUCCEEDED),
        (1, False, ToolCallStatus.FAILED),
        (None, True, ToolCallStatus.FAILED),
    ],
)
async def test_named_check_admits_before_launch_and_replays_terminal_receipt(
    session_factory, tmp_path, exit_code, timed_out, expected
) -> None:
    case = await _named_case(session_factory, tmp_path, exit_code=exit_code, timed_out=timed_out)
    service, context, request, factory = case.service, case.context, case.request, case.factory
    call_id, run_id = context.invocation_id, context.run_id
    result = await service.invoke(context, request)
    assert result.status is expected, result.error
    assert result.tool_call_id == call_id
    assert len(result.artifact_digests) == 1
    async with PostgresUnitOfWork(session_factory) as work:
        record = await work.tool_calls.get(call_id)
        operation = await work.operations.get(record.operation_intent_id)
        assert operation.status is OperationStatus.SUCCEEDED
        assert record.status is expected
        events = await work.events.list_after(run_id, 0)
        assert sum(event.payload.get("tool_call_id") == str(call_id) for event in events) == 1
    replay = await service.invoke(context, request)
    assert replay.status is expected
    assert replay.artifact_digests == result.artifact_digests
    assert factory.calls == 1


async def test_caller_cancellation_reaches_runner_and_waits_for_terminal_audit(
    session_factory, tmp_path
):
    case = await _named_case(session_factory, tmp_path)
    case.factory.release.clear()
    caller = asyncio.create_task(case.service.invoke(case.context, case.request))
    await asyncio.wait_for(case.factory.entered.wait(), 5)
    try:
        caller.cancel()
        await asyncio.wait_for(case.factory.cancel_received.wait(), 2)
        caller.cancel()
        assert not caller.done()
    finally:
        case.factory.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 5)
    async with PostgresUnitOfWork(session_factory) as work:
        record = await work.tool_calls.get(case.context.invocation_id)
        operation = await work.operations.get(record.operation_intent_id)
        assert record.status is ToolCallStatus.CANCELLED
        assert operation.status is OperationStatus.SUCCEEDED
        receipt = json.loads(await case.store.open_bytes(record.artifact_digests[0]))
        assert receipt["caller_cancelled"] is True
        events = await work.events.list_after(case.context.run_id, 0)
        assert sum(event.payload.get("tool_call_id") == str(record.id) for event in events) == 1
    assert case.factory.calls == 1


async def test_running_named_check_does_not_hold_run_row_lock(session_factory, tmp_path) -> None:
    case = await _named_case(session_factory, tmp_path)
    case.factory.release.clear()
    invocation = asyncio.create_task(case.service.invoke(case.context, case.request))
    await asyncio.wait_for(case.factory.entered.wait(), 5)

    async def pause_run() -> None:
        async with PostgresUnitOfWork(session_factory) as work:
            run = await work.runs.get_for_update(case.context.run_id)
            await work.runs.transition(run.id, run.version, RunState.CANCELLED, "run.cancelled", {})
            await work.commit()

    try:
        await asyncio.wait_for(pause_run(), 1)
        assert not invocation.done()
    finally:
        case.factory.release.set()
    assert (await asyncio.wait_for(invocation, 5)).status is ToolCallStatus.SUCCEEDED


async def test_cross_process_named_check_observer_backs_off_between_receipt_reads(
    session_factory, tmp_path
) -> None:
    case = await _named_case(session_factory, tmp_path)
    case.factory.release.clear()
    receipt_reads = 0

    class CountingUow(PostgresUnitOfWork):
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            work = await super().__aenter__()
            original = work.tool_calls.get

            async def count_receipt_reads(call_id):  # type: ignore[no-untyped-def]
                nonlocal receipt_reads
                receipt_reads += 1
                return await original(call_id)

            work.tool_calls.get = count_receipt_reads  # type: ignore[method-assign]
            return work

    observer = ControlledToolService(
        lambda: CountingUow(session_factory),
        controlled_git=case.service._git,
        worktree=case.service._worktree,
        artifact_store=case.store,
        runner_factory=case.factory,
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
    )
    owner = asyncio.create_task(case.service.invoke(case.context, case.request))
    await asyncio.wait_for(case.factory.entered.wait(), 5)
    duplicate = asyncio.create_task(observer.invoke(case.context, case.request))
    try:
        await asyncio.sleep(1.2)
        # A 50 ms fixed poll would perform about 24 reads here.  Backoff keeps
        # this cross-process observer from repeatedly opening transaction UoWs.
        assert receipt_reads <= 6
    finally:
        case.factory.release.set()
    first, replay = await asyncio.wait_for(asyncio.gather(owner, duplicate), 5)
    assert replay.tool_call_id == first.tool_call_id
    assert case.factory.calls == 1


async def test_operator_cancellation_after_admission_settles_without_launching_runner(
    session_factory, tmp_path
) -> None:
    case = await _named_case(session_factory, tmp_path)
    original = case.service._settle_named_check
    admitted = asyncio.Event()
    release = asyncio.Event()

    async def delayed_settlement(admission, *args):
        await admission.commit()
        admitted.set()
        await release.wait()
        return await original(admission, *args)

    case.service._settle_named_check = delayed_settlement
    invocation = asyncio.create_task(case.service.invoke(case.context, case.request))
    await asyncio.wait_for(admitted.wait(), 5)
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get_for_update(case.context.run_id)
        await work.runs.transition(
            run.id,
            run.version,
            RunState.CANCELLED,
            "run.cancelled",
            {},
        )
        await work.commit()
    release.set()

    result = await asyncio.wait_for(invocation, 5)
    assert result.status is ToolCallStatus.CANCELLED
    assert case.factory.calls == 0


async def test_caller_cancellation_before_admission_settles_without_launching_runner(
    session_factory, tmp_path
) -> None:
    case = await _named_case(session_factory, tmp_path)
    original = case.service._settle_named_check
    waiting_before_admission = asyncio.Event()
    release = asyncio.Event()

    async def delayed_settlement(admission, *args):
        waiting_before_admission.set()
        await release.wait()
        return await original(admission, *args)

    case.service._settle_named_check = delayed_settlement
    caller = asyncio.create_task(case.service.invoke(case.context, case.request))
    await asyncio.wait_for(waiting_before_admission.wait(), 5)
    caller.cancel()
    assert not caller.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 5)
    assert case.factory.calls == 0
    async with PostgresUnitOfWork(session_factory) as work:
        record = await work.tool_calls.get(case.context.invocation_id)
        operation = await work.operations.get(record.operation_intent_id)
        assert record.status is ToolCallStatus.CANCELLED
        assert operation.status is OperationStatus.SUCCEEDED
        receipt_digest = record.artifact_digests
        events = await work.events.list_after(case.context.run_id, 0)
        assert sum(event.payload.get("tool_call_id") == str(record.id) for event in events) == 1
    replay = await case.service.invoke(case.context, case.request)
    assert replay.status is ToolCallStatus.CANCELLED
    assert replay.artifact_digests == receipt_digest
    assert case.factory.calls == 0


async def test_caller_cancellation_after_preflight_settles_before_runner_ownership(
    session_factory, tmp_path
) -> None:
    case = await _named_case(session_factory, tmp_path)
    reached_execution = asyncio.Event()
    release_execution = asyncio.Event()
    executor = case.service._operation_executor

    class PausedExecutor:
        async def invoke_admitted(self, intent, adapter):  # type: ignore[no-untyped-def]
            reached_execution.set()
            await release_execution.wait()
            return await executor.invoke_admitted(intent, adapter)

    case.service._operation_executor = PausedExecutor()
    caller = asyncio.create_task(case.service.invoke(case.context, case.request))
    await asyncio.wait_for(reached_execution.wait(), 5)
    caller.cancel()
    caller.cancel()
    release_execution.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 5)
    assert case.factory.calls == 0
    async with PostgresUnitOfWork(session_factory) as work:
        record = await work.tool_calls.get(case.context.invocation_id)
        operation = await work.operations.get(record.operation_intent_id)
        assert record.status is ToolCallStatus.CANCELLED
        assert operation.status is OperationStatus.SUCCEEDED
        receipt_digest = record.artifact_digests
        assert len(receipt_digest) == 1
        events = await work.events.list_after(case.context.run_id, 0)
        assert sum(event.payload.get("tool_call_id") == str(record.id) for event in events) == 1
    replay = await case.service.invoke(case.context, case.request)
    assert replay.status is ToolCallStatus.CANCELLED
    assert replay.artifact_digests == receipt_digest
    assert case.factory.calls == 0
