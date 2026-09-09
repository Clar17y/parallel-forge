"""PostgreSQL intent-before-effect and replay evidence for branch removal."""

from uuid import uuid4

import pytest
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.recovery import OperationExecutor
from forge.domain.operation import OperationStatus
from forge.domain.resource import WorktreeIdentity
from forge.persistence.repositories.operations import IdempotencyConflict
from forge.tools.branch_removal import (
    BranchRemovalAdapter,
    BranchRemovalError,
    branch_removal_request,
)

from apps.orchestrator.tests.tools.test_branch_removal import Git


@pytest.mark.integration
@pytest.mark.parametrize("rejection", ["git", "source"])
async def test_conclusive_git_refusal_does_not_leave_unresolved_intent(
    operation_repository,
    persisted_run,
    tmp_path,
    rejection,
):
    from forge.domain.branch_removal import BranchSourceRejected
    from forge.tools.git import ControlledGitError

    identity = WorktreeIdentity.for_run(
        persisted_run.project_id, persisted_run.id, "forge/refused", False
    )
    handle = ManagedWorktree(
        identity=identity, path=tmp_path / identity.worktree_name, base_sha="a" * 40
    )
    request = branch_removal_request(
        handle, policy_version=1, source_command_id=uuid4(), expected_head="b" * 40
    )
    git = Git(handle)

    async def validate(intent):
        if rejection == "source":
            raise BranchSourceRejected("private authority diagnostic")
        return handle

    def refuse(handle, head):
        raise ControlledGitError()

    git.delete_retained_branch = refuse
    outcome = await OperationExecutor(operation_repository).execute(
        request, BranchRemovalAdapter(request, git, validate)
    )
    assert outcome.status is OperationStatus.FAILED
    stored = await operation_repository.get_by_idempotency_key(request.idempotency_key)
    assert stored.status is OperationStatus.FAILED
    assert await operation_repository.list_unresolved() == []
    assert git.calls == ([("inspect", "b" * 40)] if rejection == "git" else [])


@pytest.mark.integration
async def test_branch_only_projection_requires_successful_exact_creation(
    session_factory,
    operation_repository,
    persisted_run,
    tmp_path,
):
    from dataclasses import replace

    from forge.domain.operation import OperationOutcome
    from forge.domain.policy import ProjectPolicy
    from forge.domain.worktree_operation import worktree_creation_request
    from forge.persistence.queries.dashboard import _owned_retained_branch

    run = replace(persisted_run, branch_name="forge/owned", base_ref="main", base_sha="a" * 40)
    policy = ProjectPolicy(
        id=run.project_id,
        version=1,
        repository_path=str(tmp_path),
        github_repository="owner/repo",
        default_branch="main",
    )
    identity = WorktreeIdentity.for_run(run.project_id, run.id, run.branch_name, False)
    request = worktree_creation_request(run, identity, policy)
    async with session_factory() as session:
        assert not await _owned_retained_branch(session, run, policy)
    intent = await operation_repository.begin(
        run_id=run.id,
        operation_type=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
        execution_owner="test",
        execution_lease_seconds=60,
    )
    async with session_factory() as session:
        assert not await _owned_retained_branch(session, run, policy)
    await operation_repository.complete(
        intent.id,
        OperationOutcome(
            remote_resource_id=identity.worktree_name,
            payload={"worktree_name": identity.worktree_name, "base_sha": run.base_sha},
        ),
        owner_id="test",
    )
    async with session_factory() as session:
        assert await _owned_retained_branch(session, run, policy)
        assert not await _owned_retained_branch(session, replace(run, base_sha="c" * 40), policy)
        assert not await _owned_retained_branch(
            session, replace(run, base_ref="refs/heads/forge/owned"), policy
        )


@pytest.mark.integration
@pytest.mark.parametrize("interrupt_after_effect", [False, True])
async def test_branch_removal_persists_original_head_and_never_repeats_effect(
    operation_repository,
    persisted_run,
    tmp_path,
    interrupt_after_effect,
):
    identity = WorktreeIdentity.for_run(
        persisted_run.project_id, persisted_run.id, "forge/exact", False
    )
    handle = ManagedWorktree(
        identity=identity, path=tmp_path / identity.worktree_name, base_sha="a" * 40
    )
    command_id = uuid4()
    request = branch_removal_request(
        handle, policy_version=1, source_command_id=command_id, expected_head="b" * 40
    )
    git = Git(handle)
    validations = []

    async def validate(intent):
        stored = await operation_repository.get(intent.id)
        assert stored.request_digest == request.request_digest
        assert stored.request_payload == request.request_payload
        assert stored.execution_owner is not None
        validations.append(stored.id)
        return handle

    adapter = BranchRemovalAdapter(request, git, validate)
    executor = OperationExecutor(operation_repository)
    if interrupt_after_effect:
        original = git.delete_retained_branch

        def interrupted(handle, head):
            original(handle, head)
            raise RuntimeError("private diagnostic after effect")

        git.delete_retained_branch = interrupted
        with pytest.raises(BranchRemovalError):
            await executor.execute(request, adapter)
        partial = await operation_repository.get_by_idempotency_key(request.idempotency_key)
        assert partial.status is OperationStatus.NEEDS_RECONCILIATION
        assert "private diagnostic" not in partial.error
    result = await executor.execute(request, adapter)
    repeated = await executor.execute(request, adapter)
    assert result == repeated
    assert result.status is OperationStatus.SUCCEEDED
    assert [call for call in git.calls if call[0] == "delete"] == [("delete", "b" * 40)]
    assert len(set(validations)) == 1
    stored = await operation_repository.get_by_idempotency_key(request.idempotency_key)
    assert stored.request_payload["expected_head"] == "b" * 40
    assert stored.outcome["request_digest"] == request.request_digest
    # A successful durable receipt is authoritative even if Git later becomes
    # unavailable or an external checkout appears. Never inspect/restore again.
    from unittest.mock import AsyncMock, Mock

    from forge.application.services.recovery import RecoveryService

    inaccessible = BranchRemovalAdapter(
        request,
        Mock(side_effect=AssertionError("Git must not be consulted")),
        AsyncMock(side_effect=AssertionError("source must not be revalidated")),
    )
    assert await executor.execute(request, inaccessible) == result
    assert await executor.execute_admitted(stored, inaccessible) == result
    recovered = await RecoveryService(operation_repository).reconcile(stored.id, inaccessible)
    assert recovered.status is OperationStatus.SUCCEEDED
    drift = branch_removal_request(
        handle, policy_version=1, source_command_id=command_id, expected_head="c" * 40
    )
    with pytest.raises(IdempotencyConflict):
        await executor.execute(drift, BranchRemovalAdapter(drift, git, validate))
    assert [call for call in git.calls if call[0] == "delete"] == [("delete", "b" * 40)]


@pytest.mark.integration
async def test_uncertain_branch_is_quarantined_until_observation_settles_it(
    session_factory,
    operation_repository,
    command_repository,
    persisted_run,
    tmp_path,
):
    import asyncio

    from forge.application.services.recovery import RecoveryService
    from forge.domain.run import RunState
    from forge.persistence.repositories.recovery import PostgresRecoveryBarrier
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from forge.tools.git import ControlledGitError
    from forge.worker.startup import run_startup_recovery
    from forge.worker.startup_intervention import StartupInterventionRecovery

    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.transition(
            persisted_run.id, 0, RunState.PLANNING, "test.planning", {}
        )
        run = await work.runs.transition(run.id, run.version, RunState.CANCELLED, "test.cancel", {})
        await work.commit()
    identity = WorktreeIdentity.for_run(run.project_id, run.id, "forge/uncertain", False)
    handle = ManagedWorktree(
        identity=identity, path=tmp_path / identity.worktree_name, base_sha="a" * 40
    )
    command = await command_repository.enqueue(
        run_id=run.id,
        command_type="teardown_run_resources",
        idempotency_key="teardown-source",
        expected_run_version=run.version,
        payload={},
        actor_id=uuid4(),
    )
    request = branch_removal_request(
        handle, policy_version=1, source_command_id=command.id, expected_head="b" * 40
    )
    git = Git(handle)
    inspection_available = False
    original_inspect = git.inspect_retained_branch_deletion

    def inspect(handle, head):
        if not inspection_available:
            raise ControlledGitError()
        return original_inspect(handle, head)

    git.inspect_retained_branch_deletion = inspect

    async def validate(intent):
        return handle

    adapter = BranchRemovalAdapter(request, git, validate)
    with pytest.raises(BranchRemovalError):
        await OperationExecutor(operation_repository).execute(request, adapter)
    assert git.calls == [("delete", "b" * 40)]
    recovery = StartupInterventionRecovery(session_factory)

    async def reconcile():
        await recovery.wait_for_owners()
        await RecoveryService(operation_repository).reconcile_all(
            {request.kind: adapter}, allow_unresolved=True
        )
        await recovery.quarantine()

    barrier = PostgresRecoveryBarrier(session_factory)
    assert await run_startup_recovery(barrier, reconcile, asyncio.Event())
    assert await command_repository.claim_next(worker_id="blocked", lease_seconds=30) is None
    assert len(await operation_repository.list_unresolved()) == 1
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.runs.get(run.id) == run
        events = await work.events.list_after(run.id, 0)
        assert sum(e.event_type == "run.recovery_intervention" for e in events) == 1
    inspection_available = True
    assert await run_startup_recovery(barrier, reconcile, asyncio.Event())
    assert await operation_repository.list_unresolved() == []
    claimed = await command_repository.claim_next(worker_id="settled", lease_seconds=30)
    assert claimed is not None and claimed.id == command.id
    assert git.calls == [("delete", "b" * 40), ("inspect", "b" * 40)]


@pytest.mark.integration
@pytest.mark.parametrize("startup", [False, True])
async def test_uncertain_present_branch_is_terminal_without_blind_deletion(
    operation_repository,
    persisted_run,
    tmp_path,
    startup,
):
    from forge.application.services.recovery import RecoveryError, RecoveryService

    identity = WorktreeIdentity.for_run(
        persisted_run.project_id, persisted_run.id, "forge/exact", False
    )
    handle = ManagedWorktree(
        identity=identity, path=tmp_path / identity.worktree_name, base_sha="a" * 40
    )
    request = branch_removal_request(
        handle, policy_version=1, source_command_id=uuid4(), expected_head="b" * 40
    )
    intent = await operation_repository.begin(
        run_id=request.run_id,
        operation_type=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
    )
    git = Git(handle)

    async def validate(_intent):
        return handle

    adapter = BranchRemovalAdapter(request, git, validate)
    executor = OperationExecutor(operation_repository)
    result = (
        await RecoveryService(operation_repository).reconcile(intent.id, adapter)
        if startup
        else await executor.execute(request, adapter)
    )
    assert result.status is OperationStatus.FAILED
    assert git.calls == [("inspect", "b" * 40)]
    assert (await operation_repository.get(intent.id)).status is OperationStatus.FAILED
    with pytest.raises(RecoveryError):
        await executor.execute(request, adapter)
    assert git.calls == [("inspect", "b" * 40)]
