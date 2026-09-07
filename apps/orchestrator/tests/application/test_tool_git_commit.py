"""Service-level admission and replay coverage for ``git.commit``."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from uuid import UUID

import pytest
from forge.application.ports.worktrees import ManagedWorktree, PreparedGitCommit, PublishedGitCommit
from forge.application.services.recovery import OperationExecutor
from forge.application.services.tools import ControlledToolService, ToolInvocationError
from forge.domain.operation import OperationStatus
from forge.domain.resource import WorktreeIdentity
from forge.domain.tool import ToolCallStatus, ToolName, ToolRequest
from forge.persistence.models import Run
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import update

from apps.orchestrator.tests.persistence.test_tool_write_invocation import (
    _ControlledArtifactStore,
    _seed_test_database,
)
from apps.orchestrator.tests.tools.test_git import _controlled, _git


class _CommitGit:
    def __init__(self, worktree: ManagedWorktree) -> None:
        self.worktree = worktree
        self.prepares = 0
        self.publishes = 0

    def head_sha(self, worktree: ManagedWorktree) -> str:
        assert worktree == self.worktree
        return "a" * 40

    def prepare_commit(self, worktree: ManagedWorktree, message: str) -> PreparedGitCommit:
        self.prepares += 1
        return PreparedGitCommit(
            worktree_identity=worktree.identity,
            previous_sha="a" * 40,
            tree_sha="b" * 40,
            message=message,
        )

    def commit_prepared(
        self, worktree: ManagedWorktree, prepared: PreparedGitCommit
    ) -> PublishedGitCommit:
        self.publishes += 1
        return PublishedGitCommit(
            worktree_identity=worktree.identity,
            previous_sha=prepared.previous_sha,
            tree_sha=prepared.tree_sha,
            new_sha="c" * 40,
            message=prepared.message,
        )

    def inspect_prepared_commit(
        self, worktree: ManagedWorktree, prepared: PreparedGitCommit
    ) -> PublishedGitCommit | None:
        return None


class _BlockedCommitGit(_CommitGit):
    def __init__(self, worktree: ManagedWorktree) -> None:
        super().__init__(worktree)
        self.entered = threading.Event()
        self.release = threading.Event()

    def prepare_commit(self, worktree: ManagedWorktree, message: str) -> PreparedGitCommit:
        self.entered.set()
        assert self.release.wait(5)
        return super().prepare_commit(worktree, message)


async def test_git_phase_failure_rolls_back_publication_admission(
    session_factory: object, tmp_path: Path
) -> None:
    from forge.domain.actor import AgentRole
    from forge.domain.tool import ToolAuthorizationContext

    project_id, run_id, step_id, execution_id, base_sha, branch, _, path, _ = (
        await _seed_test_database(session_factory, tmp_path)  # type: ignore[arg-type]
    )
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = ManagedWorktree(identity=identity, path=Path(path), base_sha=base_sha)
    git = _CommitGit(worktree)
    call_id = UUID("f1111111-1111-4111-8111-111111111111")
    transition_attempted = False

    class FailingPhaseUow(PostgresUnitOfWork):
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            work = await super().__aenter__()

            async def fail_complete(*args, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal transition_attempted
                publication = await work.operations.get_by_idempotency_key(
                    f"git.commit:{call_id}:publish"
                )
                assert publication is not None
                transition_attempted = True
                raise RuntimeError("private driver failure must not cross tool boundary")

            work.operations.complete = fail_complete  # type: ignore[method-assign]
            return work

    service = ControlledToolService(
        lambda: FailingPhaseUow(session_factory),  # type: ignore[arg-type]
        controlled_git=git,
        worktree=worktree,
        artifact_store=_ControlledArtifactStore(tmp_path / "artifacts"),
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),  # type: ignore[arg-type]
    )
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=call_id,
    )
    with pytest.raises(ToolInvocationError, match="^controlled tool invocation failed$"):
        await service.invoke(
            context, ToolRequest(name=ToolName.GIT_COMMIT, arguments={"message": "feat: rollback"})
        )
    assert transition_attempted
    assert (git.prepares, git.publishes) == (1, 0)
    async with PostgresUnitOfWork(session_factory) as work:  # type: ignore[arg-type]
        assert await work.operations.get_by_idempotency_key(f"git.commit:{call_id}:publish") is None
        record = await work.tool_calls.get(call_id)
        assert record.status is ToolCallStatus.RUNNING
        assert not record.artifact_digests
        preparation = await work.operations.get(record.operation_intent_id)
        assert preparation.status is OperationStatus.PENDING
        events = await work.events.list_after(run_id, 0)
        assert not any(event.payload.get("tool_call_id") == str(call_id) for event in events)


async def test_git_service_publishes_real_managed_commit_and_replays_after_later_edit(
    session_factory: object, tmp_path: Path
) -> None:
    from forge.domain.actor import AgentRole
    from forge.domain.tool import ToolAuthorizationContext

    project_id, run_id, step_id, execution_id, _, branch, repository_path, _, _ = (
        await _seed_test_database(session_factory, tmp_path)  # type: ignore[arg-type]
    )
    repository = Path(repository_path)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Forge Test")
    _git(repository, "config", "user.email", "forge@example.test")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("initial\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    _git(repository, "add", "README.md", ".gitignore")
    _git(repository, "commit", "-m", "initial")
    git = _controlled(repository, tmp_path / "git-state")
    base_sha = git.resolve_default_base_sha()
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = git.create_worktree(identity, base_sha)
    async with session_factory() as session:  # type: ignore[operator]
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(base_sha=base_sha, worktree_path=str(worktree.path))
        )
        await session.commit()
    (worktree.path / "README.md").write_text("implemented\n", encoding="utf-8")
    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory),  # type: ignore[arg-type]
        controlled_git=git,
        worktree=worktree,
        artifact_store=_ControlledArtifactStore(tmp_path / "artifacts"),
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),  # type: ignore[arg-type]
    )
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=UUID("f1111111-1111-4111-8111-111111111111"),
    )
    request = ToolRequest(
        name=ToolName.GIT_COMMIT, arguments={"message": "feat: real managed commit"}
    )
    result = await service.invoke(context, request)
    assert result.status is ToolCallStatus.SUCCEEDED, result.error
    committed_head = git.head_sha(worktree)
    assert committed_head != base_sha
    assert result.metadata["new_sha"] == committed_head
    assert result.metadata["previous_sha"] == base_sha
    (worktree.path / "README.md").write_text("later uncommitted edit\n", encoding="utf-8")
    replay = await service.invoke(context, request)
    assert replay.artifact_digests == result.artifact_digests
    assert git.head_sha(worktree) == committed_head
    assert (worktree.path / "README.md").read_text(encoding="utf-8") == "later uncommitted edit\n"


async def test_git_commit_is_two_phase_and_terminal_replay_does_not_repeat_effect(
    session_factory: object, tmp_path: Path
) -> None:
    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch,
        _repo,
        worktree_path,
        _,
    ) = await _seed_test_database(  # type: ignore[arg-type]
        session_factory, tmp_path
    )
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = ManagedWorktree(identity=identity, path=Path(worktree_path), base_sha=base_sha)
    git = _CommitGit(worktree)
    artifact_store = _ControlledArtifactStore(tmp_path / "artifacts")
    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory),  # type: ignore[arg-type]
        controlled_git=git,
        worktree=worktree,
        artifact_store=artifact_store,
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),  # type: ignore[arg-type]
    )
    from forge.domain.actor import AgentRole
    from forge.domain.tool import ToolAuthorizationContext

    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=UUID("f1111111-1111-4111-8111-111111111111"),
    )
    request = ToolRequest(name=ToolName.GIT_COMMIT, arguments={"message": "feat: durable commit"})

    first = await service.invoke(context, request)
    assert first.status is ToolCallStatus.SUCCEEDED, first.error
    assert first.operation_intent_id is not None
    assert first.metadata["publication_intent_id"]
    assert (git.prepares, git.publishes) == (1, 1)
    async with PostgresUnitOfWork(session_factory) as work:  # type: ignore[arg-type]
        publication = await work.operations.get(UUID(str(first.metadata["publication_intent_id"])))
        assert publication.status is OperationStatus.SUCCEEDED
        assert publication.execution_owner is None
        assert publication.outcome is not None
        assert publication.outcome["new_sha"] == first.metadata["new_sha"]

    original_artifact = artifact_store.stored[first.artifact_digests[0]]
    artifact_store.stored[first.artifact_digests[0]] = b"{}"
    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)
    artifact_store.stored[first.artifact_digests[0]] = original_artifact

    replay = await service.invoke(context, request)
    assert replay.tool_call_id == first.tool_call_id
    assert replay.operation_intent_id == first.operation_intent_id
    assert (git.prepares, git.publishes) == (1, 1)

    with pytest.raises(ToolInvocationError):
        await service.invoke(
            context, ToolRequest(name=ToolName.GIT_COMMIT, arguments={"message": "changed"})
        )


async def test_git_commit_rejects_multiline_before_git_admission(
    session_factory: object, tmp_path: Path
) -> None:
    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch,
        _repo,
        worktree_path,
        _,
    ) = await _seed_test_database(  # type: ignore[arg-type]
        session_factory, tmp_path
    )
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = ManagedWorktree(identity=identity, path=Path(worktree_path), base_sha=base_sha)
    git = _CommitGit(worktree)
    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory), controlled_git=git, worktree=worktree
    )  # type: ignore[arg-type]
    from forge.domain.actor import AgentRole
    from forge.domain.tool import ToolAuthorizationContext

    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=UUID("f1111111-1111-4111-8111-111111111111"),
    )
    result = await service.invoke(
        context, ToolRequest(name=ToolName.GIT_COMMIT, arguments={"message": "bad\nmessage"})
    )
    assert result.status is ToolCallStatus.DENIED
    assert (
        result.error is not None
        and result.error.message == "commit message contains prohibited content"
    )
    assert (git.prepares, git.publishes) == (0, 0)


async def test_two_services_observe_one_committed_git_commit_owner(
    session_factory: object, tmp_path: Path
) -> None:
    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch,
        _repo,
        worktree_path,
        _,
    ) = await _seed_test_database(session_factory, tmp_path)  # type: ignore[arg-type]
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = ManagedWorktree(identity=identity, path=Path(worktree_path), base_sha=base_sha)
    git = _BlockedCommitGit(worktree)
    store = _ControlledArtifactStore(tmp_path / "artifacts")

    seen_running = asyncio.Event()

    class ObservingUow(PostgresUnitOfWork):
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            work = await super().__aenter__()
            original = work.tool_calls.find

            async def find(call_id):  # type: ignore[no-untyped-def]
                record = await original(call_id)
                if record is not None and record.status is ToolCallStatus.RUNNING:
                    seen_running.set()
                return record

            work.tool_calls.find = find  # type: ignore[method-assign]
            return work

    def make_service(*, observing: bool = False) -> ControlledToolService:
        return ControlledToolService(
            (lambda: ObservingUow(session_factory))
            if observing
            else (lambda: PostgresUnitOfWork(session_factory)),
            controlled_git=git,
            worktree=worktree,
            artifact_store=store,
            operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
        )  # type: ignore[arg-type]

    from forge.domain.actor import AgentRole
    from forge.domain.tool import ToolAuthorizationContext

    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=UUID("f1111111-1111-4111-8111-111111111111"),
    )
    request = ToolRequest(name=ToolName.GIT_COMMIT, arguments={"message": "feat: one owner"})
    first_task = asyncio.create_task(make_service().invoke(context, request))
    await asyncio.wait_for(asyncio.to_thread(git.entered.wait), timeout=2)
    second_task = asyncio.create_task(make_service(observing=True).invoke(context, request))
    await asyncio.wait_for(seen_running.wait(), timeout=2)
    assert git.prepares == 0 and git.publishes == 0
    try:
        # The original owner's lease remains valid during a slow preparation.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_task), timeout=3)
    finally:
        git.release.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
    first, second = await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=10)
    assert first.tool_call_id == second.tool_call_id
    assert first.operation_intent_id == second.operation_intent_id
    assert (git.prepares, git.publishes) == (1, 1)


async def test_cross_process_git_commit_observer_backs_off_between_receipt_reads(
    session_factory: object, tmp_path: Path
) -> None:
    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch,
        _repo,
        worktree_path,
        _,
    ) = await _seed_test_database(session_factory, tmp_path)  # type: ignore[arg-type]
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = ManagedWorktree(identity=identity, path=Path(worktree_path), base_sha=base_sha)
    git = _BlockedCommitGit(worktree)
    store = _ControlledArtifactStore(tmp_path / "artifacts")
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

    def service(factory) -> ControlledToolService:  # type: ignore[no-untyped-def]
        return ControlledToolService(
            factory,
            controlled_git=git,
            worktree=worktree,
            artifact_store=store,
            operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
        )  # type: ignore[arg-type]

    from forge.domain.actor import AgentRole
    from forge.domain.tool import ToolAuthorizationContext

    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=UUID("f1111111-1111-4111-8111-111111111111"),
    )
    request = ToolRequest(name=ToolName.GIT_COMMIT, arguments={"message": "feat: one owner"})
    owner = asyncio.create_task(
        service(lambda: PostgresUnitOfWork(session_factory)).invoke(context, request)
    )
    await asyncio.wait_for(asyncio.to_thread(git.entered.wait), timeout=2)
    duplicate = asyncio.create_task(
        service(lambda: CountingUow(session_factory)).invoke(context, request)
    )
    try:
        await asyncio.sleep(1.2)
        # A 50 ms fixed poll would perform about 24 reads here.  Backoff keeps
        # this cross-process observer from repeatedly opening transaction UoWs.
        assert receipt_reads <= 6
    finally:
        git.release.set()
    first, replay = await asyncio.wait_for(asyncio.gather(owner, duplicate), timeout=10)
    assert replay.tool_call_id == first.tool_call_id
    assert (git.prepares, git.publishes) == (1, 1)


@pytest.mark.parametrize("cancel_run", [False, True])
async def test_cancelled_git_commit_caller_waits_for_admitted_owner(
    session_factory: object, tmp_path: Path, cancel_run: bool
) -> None:
    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch,
        _repo,
        worktree_path,
        _,
    ) = await _seed_test_database(session_factory, tmp_path)  # type: ignore[arg-type]
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = ManagedWorktree(identity=identity, path=Path(worktree_path), base_sha=base_sha)
    git = _BlockedCommitGit(worktree)
    store = _ControlledArtifactStore(tmp_path / "artifacts")
    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory),
        controlled_git=git,
        worktree=worktree,
        artifact_store=store,
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
    )  # type: ignore[arg-type]
    from forge.domain.actor import AgentRole
    from forge.domain.tool import ToolAuthorizationContext

    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run_id,
        worktree_id=identity.worktree_name,
        policy_version=1,
        step_id=step_id,
        agent_execution_id=execution_id,
        invocation_id=UUID("f1111111-1111-4111-8111-111111111111"),
    )
    request = ToolRequest(name=ToolName.GIT_COMMIT, arguments={"message": "feat: cancel"})
    caller = asyncio.create_task(service.invoke(context, request))
    await asyncio.wait_for(asyncio.to_thread(git.entered.wait), timeout=2)
    if cancel_run:
        async with session_factory() as session:  # type: ignore[operator]
            await session.execute(update(Run).where(Run.id == run_id).values(state="CANCELLED"))
            await session.commit()
    else:
        caller.cancel()
        caller.cancel()
    assert not caller.done()
    git.release.set()
    if cancel_run:
        result = await asyncio.wait_for(caller, timeout=10)
        assert result.status is ToolCallStatus.CANCELLED
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=10)
    assert (git.prepares, git.publishes) == (1, 0 if cancel_run else 1)
    async with PostgresUnitOfWork(session_factory) as work:  # type: ignore[arg-type]
        record = await work.tool_calls.get(context.invocation_id)
        assert record.status is (
            ToolCallStatus.CANCELLED if cancel_run else ToolCallStatus.SUCCEEDED
        )
        assert len(record.artifact_digests) == 1
        assert await store.verify(record.artifact_digests[0])
        events = await work.events.list_after(run_id, 0)
        assert sum(event.payload.get("tool_call_id") == str(record.id) for event in events) == 1
        assert await work.tool_calls.count_for_execution(execution_id) == 1
        if cancel_run:
            assert (
                await work.operations.get_by_idempotency_key(f"git.commit:{record.id}:publish")
                is None
            )
            preparation = await work.operations.get(record.operation_intent_id)
            assert preparation.status is OperationStatus.SUCCEEDED
    replay = await service.invoke(context, request)
    assert replay.status is record.status
    assert replay.artifact_digests == record.artifact_digests
    assert (git.prepares, git.publishes) == (1, 0 if cancel_run else 1)
