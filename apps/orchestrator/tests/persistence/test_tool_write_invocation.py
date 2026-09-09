from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from forge.application.ports.artifacts import ArtifactDescriptor
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.repository import FileWrite
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.recovery import OperationExecutor
from forge.application.services.tools import (
    ControlledToolService,
    ToolInvocationError,
    _write_replay_result,
    _write_request_digest,
)
from forge.domain.actor import AgentRole
from forge.domain.artifact import canonical_storage_pointer
from forge.domain.operation import OperationIntent, OperationOutcome, OperationStatus
from forge.domain.policy import AgentModelPolicy, CommandSpec, ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.tool import (
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolErrorCode,
    ToolName,
    ToolRequest,
    ToolResult,
)
from forge.persistence.models import (
    AgentExecution,
    Artifact,
    ArtifactLineage,
    Project,
    ProjectPolicyVersion,
    Run,
    RunEvent,
    Step,
    Task,
    ToolCall,
)
from forge.persistence.models import (
    OperationIntent as OperationIntentRecord,
)
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.repository import RepositoryReader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _SpyOperationExecutor(OperationExecutor):
    """Delegating spy that records execute_admitted invocations without altering behavior."""

    def __init__(self, operations: PostgresOperationRepository) -> None:
        super().__init__(operations)
        self.admitted_intents: list[OperationIntent] = []
        self.second_admitted_event = asyncio.Event()

    async def execute_admitted(
        self,
        intent: OperationIntent,
        adapter: OperationAdapter,
    ) -> OperationOutcome:
        self.admitted_intents.append(intent)
        if len(self.admitted_intents) == 2:
            self.second_admitted_event.set()
        return await super().execute_admitted(intent, adapter)


class _ControlledWriter:
    def __init__(self, worktree_path: Path) -> None:
        self.worktree_path = worktree_path
        self.call_count = 0
        self.write_started_event: threading.Event | None = None
        self.write_proceed_event: threading.Event | None = None
        self.raise_on_write: Exception | None = None

    def is_bound_to(self, controlled_git: object, worktree: object, policy: object) -> bool:
        return True

    def write_file(self, path: str, content: str) -> FileWrite:
        if self.write_started_event is not None:
            self.write_started_event.set()
        if self.write_proceed_event is not None:
            self.write_proceed_event.wait(timeout=10.0)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.call_count += 1
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        target_file = self.worktree_path / path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")
        return FileWrite(
            path=path,
            output_digest=digest,
            byte_count=len(encoded),
            created=True,
        )

    def inspect_file(self, path: str, expected_digest: str) -> FileWrite | None:
        target_file = self.worktree_path / path
        if not target_file.exists():
            return None
        content = target_file.read_text(encoding="utf-8")
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != expected_digest:
            return None
        return FileWrite(
            path=path,
            output_digest=digest,
            byte_count=len(encoded),
            created=False,
            previous_digest=digest,
        )


class _ControlledArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.stored: dict[str, bytes] = {}
        self.verify_returns = True

    async def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        max_bytes: int | None = None,
        bounding_policy: str = "none",
    ) -> ArtifactDescriptor:
        digest = hashlib.sha256(data).hexdigest()
        self.stored[digest] = data
        path = self.root / canonical_storage_pointer(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ArtifactDescriptor(
            digest=digest,
            media_type=media_type,
            byte_count=len(data),
            storage_path=path,
        )

    async def verify(self, digest: str) -> bool:
        return self.verify_returns and (digest in self.stored)

    async def open_bytes(self, digest: str) -> bytes:
        return self.stored[digest]


class _Git:
    def __init__(self, repository_path: Path, worktree: ManagedWorktree) -> None:
        self.repository_path = repository_path
        self._worktree = worktree

    def expected_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        assert identity == self._worktree.identity
        assert base_sha == self._worktree.base_sha
        return self._worktree


async def _seed_test_database(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    max_tool_calls: int = 10,
    commands: tuple[CommandSpec, ...] = (),
) -> tuple[
    UUID,
    UUID,
    UUID,
    UUID,
    str,
    str,
    str,
    str,
    ProjectPolicy,
]:
    project_id = uuid4()
    task_id = uuid4()
    run_id = uuid4()
    step_id = uuid4()
    execution_id = uuid4()
    base_sha = "a" * 40
    branch_name = "forge/write-test"
    repo_path = str((tmp_path / "repo").resolve())
    worktree_path = str((tmp_path / "worktree").resolve())

    Path(repo_path).mkdir(parents=True, exist_ok=True)
    Path(worktree_path).mkdir(parents=True, exist_ok=True)

    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=repo_path,
        github_repository="Clar17y/forge-test",
        default_branch="main",
        developer_model=AgentModelPolicy(max_tool_calls=max_tool_calls),
        commands=commands,
    )

    async with session_factory() as session, session.begin():
        project = Project(
            id=project_id,
            canonical_path=repo_path,
            github_repository="Clar17y/forge-test",
            default_branch="main",
        )
        policy_record = ProjectPolicyVersion(
            project_id=project_id,
            version=1,
            policy_digest="a" * 64,
            document_schema_version=1,
            document=policy.model_dump(mode="json"),
        )
        task = Task(
            id=task_id,
            project_id=project_id,
            normalized_text="task",
            task_digest="b" * 64,
        )
        session.add_all([project, policy_record, task])
        await session.flush()
        project.current_policy_version = 1
        await session.flush()

        run = Run(
            id=run_id,
            project_id=project_id,
            task_id=task_id,
            policy_version=1,
            state=RunState.IMPLEMENTING.value,
            version=1,
            base_ref="main",
            base_sha=base_sha,
            branch_name=branch_name,
            worktree_path=worktree_path,
        )
        step = Step(
            id=step_id,
            run_id=run_id,
            kind="implement",
            attempt=1,
            status="RUNNING",
        )
        execution = AgentExecution(
            id=execution_id,
            run_id=run_id,
            step_id=step_id,
            role=AgentRole.DEVELOPER.value,
            instruction_version="1",
            provider="google",
            model="gemini-3.5-flash",
            status="RUNNING",
        )
        session.add(run)
        await session.flush()
        session.add(step)
        await session.flush()
        session.add(execution)
        await session.flush()

    return (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch_name,
        repo_path,
        worktree_path,
        policy,
    )


def _setup_service(
    session_factory: async_sessionmaker[AsyncSession],
    project_id: UUID,
    run_id: UUID,
    branch_name: str,
    base_sha: str,
    repo_path: str,
    worktree_path: str,
    writer: _ControlledWriter,
    artifact_store: _ControlledArtifactStore,
    *,
    custom_uow_factory: object | None = None,
    custom_executor: OperationExecutor | None = None,
) -> tuple[ControlledToolService, ToolAuthorizationContext, ManagedWorktree]:
    identity = WorktreeIdentity.for_run(project_id, run_id, branch_name, False)
    managed_worktree = ManagedWorktree(
        identity=identity,
        path=Path(worktree_path),
        base_sha=base_sha,
    )
    git = _Git(Path(repo_path), managed_worktree)
    reader = RepositoryReader(
        Path(worktree_path),
        secret_paths=(".env",),
        max_file_bytes=1024,
        force_python_search=True,
    )
    operation_repo = PostgresOperationRepository(session_factory)
    executor = custom_executor if custom_executor is not None else OperationExecutor(operation_repo)

    uow_factory = (
        custom_uow_factory
        if custom_uow_factory is not None
        else (lambda: PostgresUnitOfWork(session_factory))
    )
    service = ControlledToolService(
        uow_factory,  # type: ignore[arg-type]
        repository_reader=reader,
        repository_writer=writer,
        controlled_git=git,
        operation_executor=executor,
        artifact_store=artifact_store,
        worktree=managed_worktree,
    )
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        invocation_id=UUID("f1111111-1111-4111-8111-111111111111"),
    )
    return service, context, managed_worktree


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Observe committed RUNNING tool call + linked intent
# from second session before a blocked deterministic writer proceeds
# ---------------------------------------------------------------------------


async def test_observe_committed_running_tool_call_and_intent_before_blocked_writer_proceeds(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "src/hello.py", "content": "print('hello, world')\n"},
    )

    invoke_task = asyncio.create_task(service.invoke(context, request))
    try:
        # Wait until the writer thread is entered and blocked
        await asyncio.wait_for(
            asyncio.to_thread(writer.write_started_event.wait, 5.0),
            timeout=6.0,
        )
        assert writer.write_started_event.is_set(), "writer did not enter blocked write state"

        # Query from an independent second session before writer proceeds
        async with session_factory() as second_session:
            calls = (
                (await second_session.execute(select(ToolCall).where(ToolCall.run_id == run_id)))
                .scalars()
                .all()
            )
            assert len(calls) == 1
            running_call = calls[0]
            assert running_call.status == "RUNNING"
            assert running_call.authorized is True
            assert running_call.tool_name == ToolName.REPOSITORY_WRITE_FILE.value
            assert running_call.completed_at is None
            assert running_call.normalized_arguments["path"] == "src/hello.py"

            intents = (
                (
                    await second_session.execute(
                        select(OperationIntentRecord).where(OperationIntentRecord.run_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(intents) == 1
            intent = intents[0]
            assert intent.operation_kind == ToolName.REPOSITORY_WRITE_FILE.value
            assert intent.idempotency_key == f"tool:{running_call.id}"
            assert intent.status == "PENDING"
            assert intent.request_payload["path"] == "src/hello.py"

        # Allow blocked deterministic writer to proceed
        writer.write_proceed_event.set()
        result = await asyncio.wait_for(invoke_task, timeout=10.0)
    finally:
        if writer.write_proceed_event is not None:
            writer.write_proceed_event.set()
        if not invoke_task.done():
            invoke_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await invoke_task

    # Invocation succeeded
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.error is None
    assert result.tool_call_id == running_call.id
    assert result.operation_intent_id == intent.id

    # Verify final terminal states in PostgreSQL
    async with session_factory() as final_session:
        final_call = await final_session.get(ToolCall, running_call.id)
        assert final_call is not None
        assert final_call.status == "SUCCEEDED"
        assert final_call.completed_at is not None
        assert final_call.result_metadata is not None
        assert final_call.result_metadata["result_status"] == "succeeded"
        assert final_call.result_metadata["path"] == "src/hello.py"

        final_intent = await final_session.get(OperationIntentRecord, intent.id)
        assert final_intent is not None
        assert final_intent.status == "SUCCEEDED"

        # Check recorded artifact descriptor and lineage
        lineages = (
            (
                await final_session.execute(
                    select(ArtifactLineage).where(
                        ArtifactLineage.run_id == run_id,
                        ArtifactLineage.producer_id == running_call.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(lineages) == 1
        lineage = lineages[0]
        assert lineage.producer_kind == "controlled_tool"

        artifacts = (
            (
                await final_session.execute(
                    select(Artifact).where(Artifact.id == lineage.artifact_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(artifacts) == 1
        art = artifacts[0]
        assert art.digest == result.artifact_digests[0]
        assert art.media_type == "application/json"
        assert art.artifact_metadata["operation_intent_id"] == str(intent.id)

        # Check completed RunEvent
        events = (
            (
                await final_session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool_call.completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        ev = events[0]
        assert ev.actor_id == execution_id
        assert ev.payload["tool_call_id"] == str(running_call.id)
        assert ev.payload["status"] == "succeeded"
        assert ev.payload["authorized"] is True
        assert ev.payload["resource_id"] == context.worktree_id
        assert ev.payload["artifact_digests"] == [art.digest]


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Failed admission leaves neither row nor effect
# ---------------------------------------------------------------------------


class _FailingAdmissionUoW(PostgresUnitOfWork):
    async def commit(self) -> None:
        # Simulate admission failure right before committing reservation
        raise RuntimeError("simulated admission commit failure")


async def test_failed_admission_leaves_neither_tool_call_nor_intent_nor_effect(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")

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
        custom_uow_factory=lambda: _FailingAdmissionUoW(session_factory),
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "failed.py", "content": "pass\n"},
    )

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)

    # Neither row exists and no filesystem effect occurred
    async with session_factory() as session:
        tool_call_count = await session.scalar(
            select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)
        )
        intent_count = await session.scalar(
            select(func.count())
            .select_from(OperationIntentRecord)
            .where(OperationIntentRecord.run_id == run_id)
        )
        assert tool_call_count == 0
        assert intent_count == 0

        event_count = await session.scalar(
            select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)
        )
        assert event_count == 0

    assert writer.call_count == 0
    assert not (Path(worktree_path) / "failed.py").exists()


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Concurrent identical invocations yield one effect and terminal evidence
# ---------------------------------------------------------------------------


async def test_concurrent_identical_invocations_produce_one_effect_and_terminal_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Prove concurrent identical write requests deduplicate deterministically.

    The first writer is deterministically blocked before effect execution. The
    second caller enters admission and executes against the still-pending intent
    persisted in PostgreSQL before the writer is released. Exactly one effect,
    one tool call, and one terminal event lineage are produced.
    """
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
    operation_repo = PostgresOperationRepository(session_factory)
    executor_spy = _SpyOperationExecutor(operation_repo)

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
        custom_executor=executor_spy,
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "concurrent.txt", "content": "concurrent payload\n"},
    )

    task1: asyncio.Task[ToolResult] | None = None
    task2: asyncio.Task[ToolResult] | None = None
    try:
        # Launch first writer task
        task1 = asyncio.create_task(service.invoke(context, request))

        # Deterministically wait until first writer has entered write_file and blocked
        await asyncio.wait_for(
            asyncio.to_thread(writer.write_started_event.wait, 5.0),
            timeout=6.0,
        )
        assert writer.write_started_event.is_set(), "first writer failed to enter blocked state"
        assert not writer.write_proceed_event.is_set(), (
            "first writer proceed event should remain unset"
        )

        # Launch second invocation while first writer effect is still blocked
        task2 = asyncio.create_task(service.invoke(context, request))

        # Wait until second caller reaches execute_admitted with the admitted intent
        await asyncio.wait_for(
            executor_spy.second_admitted_event.wait(),
            timeout=5.0,
        )
        assert executor_spy.second_admitted_event.is_set(), (
            "second caller did not reach execute_admitted"
        )

        # While first writer effect is STILL blocked:
        assert not writer.write_proceed_event.is_set(), (
            "writer must remain blocked while second caller admits"
        )
        assert writer.call_count == 0, "no write effect may occur before proceed event is set"

        # Prove second caller reaches the same persisted still-pending intent:
        assert len(executor_spy.admitted_intents) == 2
        first_intent = executor_spy.admitted_intents[0]
        second_intent = executor_spy.admitted_intents[1]
        assert second_intent.id == first_intent.id
        assert second_intent.is_new is False
        assert second_intent.status is OperationStatus.PENDING

        # Query independent PostgreSQL session: intent is persisted and still PENDING, call is RUNNING
        async with session_factory() as probe_session:
            db_intents = (
                (
                    await probe_session.execute(
                        select(OperationIntentRecord).where(OperationIntentRecord.run_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(db_intents) == 1
            assert db_intents[0].id == second_intent.id
            assert db_intents[0].status == "PENDING"

            db_calls = (
                (await probe_session.execute(select(ToolCall).where(ToolCall.run_id == run_id)))
                .scalars()
                .all()
            )
            assert len(db_calls) == 1
            assert db_calls[0].status == "RUNNING"

        # Now allow the blocked deterministic writer to proceed
        writer.write_proceed_event.set()

        # Both invocations must complete successfully
        results = await asyncio.wait_for(
            asyncio.gather(task1, task2),
            timeout=10.0,
        )
    finally:
        # Guarantee release and cancellation/join of all worker tasks in try/finally
        if writer.write_proceed_event is not None:
            writer.write_proceed_event.set()
        for t in (task1, task2):
            if t is not None and not t.done():
                t.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(t), timeout=5.0)

    # Both invocations report success
    assert all(r.status is ToolCallStatus.SUCCEEDED for r in results)
    assert results[0].tool_call_id == results[1].tool_call_id
    assert results[0].tool_call_id == context.invocation_id
    assert results[0].operation_intent_id == results[1].operation_intent_id

    # Exactly ONE write effect occurred
    assert writer.call_count == 1

    # Exactly one ToolCall, one OperationIntent, and one Artifact row in PostgreSQL
    async with session_factory() as session:
        calls = (
            (await session.execute(select(ToolCall).where(ToolCall.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(calls) == 1
        assert calls[0].status == "SUCCEEDED"

        intents = (
            (
                await session.execute(
                    select(OperationIntentRecord).where(OperationIntentRecord.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(intents) == 1
        assert intents[0].status == "SUCCEEDED"

        lineages = (
            (await session.execute(select(ArtifactLineage).where(ArtifactLineage.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(lineages) == 1
        assert lineages[0].producer_kind == "controlled_tool"

        artifacts = (
            (await session.execute(select(Artifact).where(Artifact.id == lineages[0].artifact_id)))
            .scalars()
            .all()
        )
        assert len(artifacts) == 1

        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool_call.completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["status"] == "succeeded"
        assert events[0].payload["authorized"] is True
        assert events[0].payload["resource_id"] == context.worktree_id


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Terminal replay doesn't repeat effect/budget & cannot cross context
# ---------------------------------------------------------------------------


async def test_terminal_replay_does_not_repeat_effect_or_budget_and_cannot_cross_context(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Seed with max_tool_calls = 1
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
    ) = await _seed_test_database(session_factory, tmp_path, max_tool_calls=1)

    writer = _ControlledWriter(Path(worktree_path))
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")

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
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "replay.txt", "content": "replay content\n"},
    )

    # 1. First execution uses budget 1 of 1
    res1 = await service.invoke(context, request)
    assert res1.status is ToolCallStatus.SUCCEEDED
    assert writer.call_count == 1

    # 2. Terminal replay with exact identical context & request succeeds without consuming budget
    res2 = await service.invoke(context, request)
    assert res2.status is ToolCallStatus.SUCCEEDED
    assert res2.tool_call_id == res1.tool_call_id
    assert res2.operation_intent_id == res1.operation_intent_id
    assert writer.call_count == 1  # No extra writer call!

    # 3. Budget exact boundary: replay succeeds without consuming budget, but a new write exhausts budget
    new_request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "other.txt", "content": "other content\n"},
    )
    budget_denied = await service.invoke(replace(context, invocation_id=uuid4()), new_request)
    assert budget_denied.status is ToolCallStatus.DENIED
    assert budget_denied.error is not None
    assert budget_denied.error.code is ToolErrorCode.BUDGET_EXCEEDED
    assert writer.call_count == 1  # Writer was not called for denied request!

    async with session_factory() as session:
        events = (
            (
                await session.execute(
                    select(RunEvent)
                    .where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool_call.completed",
                    )
                    .order_by(RunEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2
        assert events[0].payload["status"] == "succeeded"
        assert events[0].payload["authorized"] is True
        assert events[0].payload["resource_id"] == context.worktree_id
        assert events[1].payload["status"] == "denied"
        assert events[1].payload["authorized"] is False
        assert events[1].payload["resource_id"] == context.worktree_id

    other_exec_id = uuid4()
    cross_exec_context = replace(context, agent_execution_id=other_exec_id)

    # 4. Attempt replay crossing execution, step, role, or policy fails closed with ToolInvocationError
    other_step_id = uuid4()
    async with session_factory() as session, session.begin():
        other_step = Step(
            id=other_step_id,
            run_id=run_id,
            kind="remediate",
            attempt=1,
            status="RUNNING",
        )
        session.add(other_step)

    cross_step_context = replace(context, step_id=other_step_id)

    async with PostgresUnitOfWork(session_factory) as uow:
        persisted_record = await uow.tool_calls.get(res1.tool_call_id)

    # Note: Direct _write_replay_result checks below are helper-level unit checks
    # on the replay builder function, not end-to-end service proof.
    # Valid replay returns ToolResult without errors
    async with PostgresUnitOfWork(session_factory) as uow:
        replay_res = await _write_replay_result(
            persisted_record,
            context,
            persisted_record.normalized_arguments,
            _write_request_digest(request),
            artifacts=uow.artifacts,
            artifact_store=artifact_store,
        )
    assert replay_res.status is ToolCallStatus.SUCCEEDED
    assert replay_res.tool_call_id == res1.tool_call_id

    # Attempting to replay with crossed agent_execution_id raises ToolInvocationError
    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            persisted_record,
            cross_exec_context,
            persisted_record.normalized_arguments,
            _write_request_digest(request),
            artifacts=uow.artifacts,
            artifact_store=artifact_store,
        )

    # Attempting to replay with crossed step_id raises ToolInvocationError
    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            persisted_record,
            cross_step_context,
            persisted_record.normalized_arguments,
            _write_request_digest(request),
            artifacts=uow.artifacts,
            artifact_store=artifact_store,
        )

    # Attempting to replay with crossed role raises ToolInvocationError
    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            persisted_record,
            replace(context, role=AgentRole.REVIEWER),
            persisted_record.normalized_arguments,
            _write_request_digest(request),
            artifacts=uow.artifacts,
            artifact_store=artifact_store,
        )

    # Attempting to replay with crossed policy_version raises ToolInvocationError
    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            persisted_record,
            replace(context, policy_version=99),
            persisted_record.normalized_arguments,
            _write_request_digest(request),
            artifacts=uow.artifacts,
            artifact_store=artifact_store,
        )

    # Attempting to replay with tampered normalized arguments raises ToolInvocationError
    tampered_args = dict(persisted_record.normalized_arguments)
    tampered_args["path"] = "tampered.txt"
    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            persisted_record,
            context,
            tampered_args,
            _write_request_digest(request),
            artifacts=uow.artifacts,
            artifact_store=artifact_store,
        )


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Pre-admission denial never creates intent
# ---------------------------------------------------------------------------


async def test_pre_admission_denial_never_creates_intent_or_writer_effect(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")

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
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)

    # Escape path request
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "../escaped.txt", "content": "malicious\n"},
    )

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_REQUEST
    assert writer.call_count == 0

    # In database: ToolCall is recorded as DENIED, but NO OperationIntentRecord is created
    async with session_factory() as session:
        calls = (
            (await session.execute(select(ToolCall).where(ToolCall.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(calls) == 1
        assert calls[0].status == "DENIED"
        assert calls[0].authorized is False

        intent_count = await session.scalar(
            select(func.count())
            .select_from(OperationIntentRecord)
            .where(OperationIntentRecord.run_id == run_id)
        )
        assert intent_count == 0

        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool_call.completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["status"] == "denied"
        assert events[0].payload["authorized"] is False
        assert events[0].payload["resource_id"] == context.worktree_id


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Post-effect failure retains honest recoverable state
# ---------------------------------------------------------------------------


async def test_post_effect_failure_retains_honest_recoverable_state(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify post-effect failure leaves honest unfinalized state in PostgreSQL.

    Note: This test only proves honest RUNNING + SUCCEEDED state persistence (the
    unfinalized ToolCall remains in RUNNING and OperationIntent remains SUCCEEDED
    without fabricating premature success or rolling back the observed effect),
    not complete crash-recovery or cancellation, which remain separate.
    """
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
    # Simulate artifact verification failure after writer effect has executed
    artifact_store.verify_returns = False

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
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "honest_recovery.txt", "content": "important effect\n"},
    )

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)

    # The writer effect did take place on disk
    assert writer.call_count == 1
    assert (Path(worktree_path) / "honest_recovery.txt").exists()

    # In PostgreSQL: ToolCall remains in RUNNING state (honest unfinalized evidence, not fabricated success)
    async with session_factory() as session:
        calls = (
            (await session.execute(select(ToolCall).where(ToolCall.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(calls) == 1
        assert calls[0].status == "RUNNING"
        assert calls[0].completed_at is None

        # OperationIntent was recorded and marked SUCCEEDED by OperationExecutor
        intents = (
            (
                await session.execute(
                    select(OperationIntentRecord).where(OperationIntentRecord.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(intents) == 1
        assert intents[0].status == "SUCCEEDED"

        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool_call.completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Exact canonical/digest-only normalization & canary exclusion
# ---------------------------------------------------------------------------


async def test_write_persists_canonical_normalized_arguments_and_excludes_canary_from_all_records(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify write normalizes path/digest arguments and excludes plaintext canary from all audit tables.

    The actual service is invoked with a valid normalization-sensitive path and a synthetic sensitive canary.
    We assert:
    - The actual target file contains the intended content (file effect).
    - Normalized arguments contain exact canonical path and digest/byte_count (digest-only).
    - Plaintext canary is absent from:
      1. ToolCall normalized_arguments and result_metadata.
      2. OperationIntentRecord request_payload and outcome.
      3. RunEvent payload.
      4. Stored Artifact content and Artifact metadata.
    """
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")

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
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)

    # Valid normalization-sensitive path supported by contract (forward-slash relative subpath)
    # and synthetic sensitive canary (never real credentials)
    canary_token = "CANARY_SECRET_ghp_0123456789abcdefghijklmnopqrstuvwxyz_test"
    canary_content = f"{canary_token}\n"
    canary_bytes = canary_content.encode("utf-8")
    canary_digest = hashlib.sha256(canary_bytes).hexdigest()
    target_path = "src/nested/module_canary.py"

    # Positive control: prove canary_token detects leaked content in JSON where canary_content vacuously passes
    assert canary_content not in json.dumps({"content": canary_content})
    assert canary_token in json.dumps({"content": canary_content})

    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": target_path, "content": canary_content},
    )

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.error is None

    # 1. Target file contains intended content (file effect is preserved and verified)
    target_file = Path(worktree_path) / target_path
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == canary_content

    # 2. Exact canonical/digest-only normalized arguments
    expected_normalized_args = {
        "path": target_path,
        "content_digest": canary_digest,
        "content_byte_count": len(canary_bytes),
    }

    # 3. Inspect actual stored evidence in PostgreSQL via real PG fixture
    async with session_factory() as session:
        # Check ToolCall row
        persisted_call = await session.get(ToolCall, result.tool_call_id)
        assert persisted_call is not None
        assert persisted_call.status == "SUCCEEDED"
        assert persisted_call.authorized is True
        assert persisted_call.normalized_arguments == expected_normalized_args
        assert "content" not in persisted_call.normalized_arguments
        # Plaintext canary absent from tool record metadata/args
        call_args_json = json.dumps(persisted_call.normalized_arguments)
        call_meta_json = json.dumps(persisted_call.result_metadata)
        assert canary_token not in call_args_json
        assert canary_token not in call_meta_json
        assert persisted_call.result_metadata["output_digest"] == canary_digest
        assert persisted_call.result_metadata["byte_count"] == len(canary_bytes)
        assert persisted_call.result_metadata["path"] == target_path

        # Check OperationIntentRecord row
        persisted_intent = await session.get(OperationIntentRecord, result.operation_intent_id)
        assert persisted_intent is not None
        assert persisted_intent.status == "SUCCEEDED"
        assert persisted_intent.request_payload["path"] == target_path
        assert persisted_intent.request_payload["content_digest"] == canary_digest
        assert persisted_intent.request_payload["content_byte_count"] == len(canary_bytes)
        assert "content" not in persisted_intent.request_payload
        # Plaintext canary absent from operation payload
        intent_req_json = json.dumps(persisted_intent.request_payload)
        assert canary_token not in intent_req_json
        if persisted_intent.outcome_payload is not None:
            assert canary_token not in json.dumps(persisted_intent.outcome_payload)

        # Check RunEvent row
        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool_call.completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.payload["resource_id"] == context.worktree_id
        assert event.payload["status"] == "succeeded"
        # Plaintext canary absent from causal event
        event_payload_json = json.dumps(event.payload)
        assert canary_token not in event_payload_json

        # Check ArtifactLineage and Artifact rows
        lineages = (
            (
                await session.execute(
                    select(ArtifactLineage).where(
                        ArtifactLineage.run_id == run_id,
                        ArtifactLineage.producer_id == result.tool_call_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(lineages) == 1
        artifact = await session.get(Artifact, lineages[0].artifact_id)
        assert artifact is not None
        assert artifact.digest == result.artifact_digests[0]
        # Plaintext canary absent from artifact metadata
        artifact_meta_json = json.dumps(artifact.artifact_metadata)
        assert canary_token not in artifact_meta_json

        # Check stored artifact bytes
        stored_bytes = artifact_store.stored[artifact.digest]
        assert canary_token.encode("utf-8") not in stored_bytes
        artifact_doc = json.loads(stored_bytes.decode("utf-8"))
        assert canary_token not in json.dumps(artifact_doc)
        assert artifact_doc["result"]["output_digest"] == canary_digest


async def test_distinct_invocation_ids_with_identical_content_create_distinct_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
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
    )
    first_id = UUID("11111111-1111-4111-8111-111111111111")
    second_id = UUID("22222222-2222-4222-8222-222222222222")
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "same.txt", "content": "identical content\n"},
    )

    first = await service.invoke(replace(context, invocation_id=first_id), request)
    second = await service.invoke(replace(context, invocation_id=second_id), request)

    assert first.status is ToolCallStatus.SUCCEEDED
    assert second.status is ToolCallStatus.SUCCEEDED
    assert first.tool_call_id == first_id
    assert second.tool_call_id == second_id
    assert first.operation_intent_id != second.operation_intent_id
    assert first.artifact_digests != second.artifact_digests
    assert writer.call_count == 2

    async with session_factory() as session:
        calls = (
            (
                await session.execute(
                    select(ToolCall)
                    .where(ToolCall.run_id == run_id)
                    .order_by(ToolCall.started_at, ToolCall.id)
                )
            )
            .scalars()
            .all()
        )
        assert {call.id for call in calls} == {first_id, second_id}
        assert len(calls) == 2
        intents = (
            (
                await session.execute(
                    select(OperationIntentRecord).where(OperationIntentRecord.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(intents) == 2
        assert {intent.id for intent in intents} == {
            first.operation_intent_id,
            second.operation_intent_id,
        }

        lineages = (
            (await session.execute(select(ArtifactLineage).where(ArtifactLineage.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(lineages) == 2
        assert {lineage.producer_id for lineage in lineages} == {first_id, second_id}

        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool_call.completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2
        assert {event.payload["tool_call_id"] for event in events} == {
            str(first_id),
            str(second_id),
        }

    async with PostgresUnitOfWork(session_factory) as uow:
        first_record = await uow.tool_calls.get(first_id)
        second_record = await uow.tool_calls.get(second_id)
    assert first_record.request_digest == second_record.request_digest
    assert first_record.resource_id == second_record.resource_id == context.worktree_id
    assert first_record.invocation_schema_version == 1
    assert second_record.invocation_schema_version == 1
    assert first_record.operation_intent_id == first.operation_intent_id
    assert second_record.operation_intent_id == second.operation_intent_id
    assert first_record.artifact_digests == first.artifact_digests
    assert second_record.artifact_digests == second.artifact_digests


async def test_reusing_invocation_id_with_changed_secret_content_preserves_prior_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
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
    )
    invocation_id = UUID("33333333-3333-4333-8333-333333333333")
    context = replace(
        base_context,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=invocation_id,
    )
    first_request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "secret.txt", "content": "TOKEN=first-secret\n"},
    )
    changed_request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "secret.txt", "content": "TOKEN=second-secret\n"},
    )

    first = await service.invoke(context, first_request)
    assert first.status is ToolCallStatus.SUCCEEDED

    async with PostgresUnitOfWork(session_factory) as uow:
        original_call = await uow.tool_calls.get(invocation_id)
    original_digest = original_call.request_digest
    original_resource_id = original_call.resource_id
    original_schema_version = original_call.invocation_schema_version
    original_metadata = dict(original_call.result_metadata or {})
    original_artifacts = original_call.artifact_digests
    original_intent_id = original_call.operation_intent_id

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, changed_request)

    assert writer.call_count == 1
    assert (Path(worktree_path) / "secret.txt").read_text(encoding="utf-8") == (
        "TOKEN=first-secret\n"
    )
    async with session_factory() as session:
        calls = (
            (await session.execute(select(ToolCall).where(ToolCall.run_id == run_id)))
            .scalars()
            .all()
        )
        intents = (
            (
                await session.execute(
                    select(OperationIntentRecord).where(OperationIntentRecord.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool_call.completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        lineages = (
            (await session.execute(select(ArtifactLineage).where(ArtifactLineage.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(calls) == len(intents) == len(events) == len(lineages) == 1
        assert calls[0].id == invocation_id

    async with PostgresUnitOfWork(session_factory) as uow:
        persisted = await uow.tool_calls.get(invocation_id)
    assert persisted.request_digest == original_digest
    assert persisted.resource_id == original_resource_id == context.worktree_id
    assert persisted.invocation_schema_version == original_schema_version == 1
    assert persisted.result_metadata == original_metadata
    assert persisted.artifact_digests == original_artifacts
    assert persisted.operation_intent_id == original_intent_id


async def test_terminal_replay_ignores_later_target_mutation_and_returns_original_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
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
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "history.txt", "content": "original\n"},
    )
    first = await service.invoke(context, request)
    target = Path(worktree_path) / "history.txt"
    target.write_text("later legitimate mutation\n", encoding="utf-8")

    replay = await service.invoke(context, request)

    assert replay.status is ToolCallStatus.SUCCEEDED
    assert replay.tool_call_id == first.tool_call_id
    assert replay.operation_intent_id == first.operation_intent_id
    assert replay.artifact_digests == first.artifact_digests
    assert replay.metadata == first.metadata
    assert writer.call_count == 1
    assert target.read_text(encoding="utf-8") == "later legitimate mutation\n"

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OperationIntentRecord)
                .where(OperationIntentRecord.run_id == run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "tool_call.completed",
                )
            )
            == 1
        )


async def test_terminal_replay_rejects_corrupt_blob_even_when_store_verify_returns_true(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
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
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "corrupt.txt", "content": "original\n"},
    )
    first = await service.invoke(context, request)
    artifact_digest = first.artifact_digests[0]
    artifact_store.stored[artifact_digest] = b'{"valid_json":"wrong bytes"}'

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)

    assert artifact_store.verify_returns is True
    assert writer.call_count == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OperationIntentRecord)
                .where(OperationIntentRecord.run_id == run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "tool_call.completed",
                )
            )
            == 1
        )


async def test_terminal_replay_rejects_missing_blob_without_new_effect_or_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
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
    )
    context = replace(base_context, step_id=step_id, agent_execution_id=execution_id)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "missing.txt", "content": "original\n"},
    )
    first = await service.invoke(context, request)
    del artifact_store.stored[first.artifact_digests[0]]

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)

    assert writer.call_count == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OperationIntentRecord)
                .where(OperationIntentRecord.run_id == run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "tool_call.completed",
                )
            )
            == 1
        )


async def test_write_without_invocation_id_is_denied_without_intent_or_effect(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
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
    )
    context = replace(
        base_context,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=None,
    )
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "no-id.txt", "content": "must not write\n"},
    )

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_REQUEST
    assert writer.call_count == 0
    assert not (Path(worktree_path) / "no-id.txt").exists()
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OperationIntentRecord)
                .where(OperationIntentRecord.run_id == run_id)
            )
            == 0
        )


async def test_reused_invocation_id_from_other_current_execution_preserves_prior_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
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
    )
    invocation_id = UUID("44444444-4444-4444-8444-444444444444")
    context = replace(
        base_context,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=invocation_id,
    )
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "authority.txt", "content": "bound\n"},
    )
    first = await service.invoke(context, request)
    async with PostgresUnitOfWork(session_factory) as uow:
        original = await uow.tool_calls.get(invocation_id)

    other_execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=other_execution_id,
                run_id=run_id,
                step_id=step_id,
                role=AgentRole.DEVELOPER.value,
                instruction_version="1",
                provider="google",
                model="gemini-3.5-flash",
                status="RUNNING",
            )
        )

    with pytest.raises(ToolInvocationError):
        await service.invoke(
            replace(context, agent_execution_id=other_execution_id),
            request,
        )

    assert writer.call_count == 1
    async with PostgresUnitOfWork(session_factory) as uow:
        persisted = await uow.tool_calls.get(invocation_id)
    assert persisted.agent_execution_id == execution_id
    assert persisted.request_digest == original.request_digest
    assert persisted.resource_id == original.resource_id == context.worktree_id
    assert persisted.invocation_schema_version == original.invocation_schema_version == 1
    assert (
        persisted.operation_intent_id == original.operation_intent_id == first.operation_intent_id
    )
    assert persisted.artifact_digests == original.artifact_digests == first.artifact_digests
    assert persisted.result_metadata == original.result_metadata

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OperationIntentRecord)
                .where(OperationIntentRecord.run_id == run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "tool_call.completed",
                )
            )
            == 1
        )
