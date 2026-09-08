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
    drift = branch_removal_request(
        handle, policy_version=1, source_command_id=command_id, expected_head="c" * 40
    )
    with pytest.raises(IdempotencyConflict):
        await executor.execute(drift, BranchRemovalAdapter(drift, git, validate))
    assert [call for call in git.calls if call[0] == "delete"] == [("delete", "b" * 40)]


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
