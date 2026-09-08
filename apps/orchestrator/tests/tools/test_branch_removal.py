"""Frozen branch deletion requests and observation-only recovery."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.operation import OperationIntent, OperationStatus
from forge.domain.resource import WorktreeIdentity


class Git:
    def __init__(self, handle):
        self.handle = handle
        self.absent = False
        self.calls = []

    def expected_worktree(self, identity, base_sha):
        return ManagedWorktree(identity=identity, path=self.handle.path, base_sha=base_sha)

    def delete_retained_branch(self, handle, expected_head):
        assert handle == self.handle
        self.calls.append(("delete", expected_head))
        self.absent = True

    def inspect_retained_branch_deletion(self, handle, expected_head):
        assert handle == self.handle
        self.calls.append(("inspect", expected_head))
        return self.absent


def binding(tmp_path):
    from forge.tools.branch_removal import BranchRemovalAdapter, branch_removal_request

    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "forge/exact", False)
    handle = ManagedWorktree(
        identity=identity, path=tmp_path / identity.worktree_name, base_sha="a" * 40
    )
    request = branch_removal_request(
        handle, policy_version=2, source_command_id=uuid4(), expected_head="b" * 40
    )
    intent = OperationIntent(
        run_id=request.run_id,
        kind=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
    )
    git = Git(handle)
    validate = AsyncMock(return_value=handle)
    return request, intent, git, validate, BranchRemovalAdapter(request, git, validate)


@pytest.mark.asyncio
async def test_branch_removal_uses_frozen_request_and_exact_receipt(tmp_path: Path):
    request, intent, git, validate, adapter = binding(tmp_path)
    outcome = await adapter.invoke(intent)
    validate.assert_awaited_once_with(intent)
    assert git.calls == [("delete", "b" * 40), ("inspect", "b" * 40)]
    assert outcome.status is OperationStatus.SUCCEEDED
    assert outcome.payload["request_digest"] == request.request_digest
    assert outcome.payload["source_command_id"] == request.request_payload["source_command_id"]
    assert outcome.payload["expected_head"] == "b" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["run", "digest", "payload", "kind", "key"])
async def test_substituted_intent_never_reaches_source_or_git(tmp_path, change):
    from forge.tools.branch_removal import BranchRemovalError

    _request, intent, git, validate, adapter = binding(tmp_path)
    changes = {
        "run": {"run_id": uuid4()},
        "digest": {"request_digest": "e" * 64},
        "payload": {"request_payload": {**intent.request_payload, "expected_head": "c" * 40}},
        "kind": {"kind": "different"},
        "key": {"idempotency_key": "different"},
    }
    with pytest.raises(BranchRemovalError, match="differs"):
        await adapter.invoke(replace(intent, **changes[change]))
    validate.assert_not_awaited()
    assert git.calls == []


@pytest.mark.asyncio
async def test_source_rejection_is_redacted_before_git(tmp_path):
    from forge.tools.branch_removal import BranchRemovalError

    _, intent, git, validate, adapter = binding(tmp_path)
    validate.side_effect = RuntimeError("private source diagnostic")
    with pytest.raises(BranchRemovalError) as error:
        await adapter.invoke(intent)
    assert "private" not in str(error.value)
    assert git.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("absent", [False, True])
async def test_recovery_only_inspects_and_never_retries_deletion(tmp_path, absent):
    _, intent, git, _validate, adapter = binding(tmp_path)
    git.absent = absent
    outcome = await adapter.reconcile(intent)
    assert git.calls == [("inspect", "b" * 40)]
    assert outcome.status is (OperationStatus.SUCCEEDED if absent else OperationStatus.FAILED)
    assert outcome.payload["removed"] is absent
    if not absent:
        assert outcome.error == "branch remains; fresh confirmation required"


@pytest.mark.asyncio
async def test_wrong_source_handle_cannot_remove_branch(tmp_path):
    from forge.tools.branch_removal import BranchRemovalError

    _, intent, git, validate, adapter = binding(tmp_path)
    validate.return_value = replace(git.handle, path=tmp_path / "different")
    with pytest.raises(BranchRemovalError):
        await adapter.invoke(intent)
    assert git.calls == []


@pytest.mark.asyncio
async def test_cancellation_waits_for_atomic_git_activity_to_settle(tmp_path):
    import asyncio
    import threading

    _, intent, git, _validate, adapter = binding(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = git.delete_retained_branch

    def delayed(handle, expected):
        started.set()
        assert release.wait(5)
        original(handle, expected)

    git.delete_retained_branch = delayed
    task = asyncio.create_task(adapter.invoke(intent))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert git.absent is True
    outcome = await adapter.reconcile(intent)
    assert outcome.status is OperationStatus.SUCCEEDED
    assert len([call for call in git.calls if call[0] == "delete"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_version", True),
        ("expected_head", "private invalid head"),
        ("unexpected", "private"),
    ],
)
def test_invalid_request_is_closed_and_redacted(tmp_path, field, value):
    from forge.domain.operation import canonical_digest
    from forge.tools.branch_removal import BranchRemovalAdapter, BranchRemovalError

    request, _intent, git, validate, _adapter = binding(tmp_path)
    payload = {**request.request_payload, field: value}
    malformed = replace(request, request_payload=payload, request_digest=canonical_digest(payload))
    with pytest.raises(BranchRemovalError) as error:
        BranchRemovalAdapter(malformed, git, validate)
    assert "private" not in str(error.value)
    assert git.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("absent", [True, False])
async def test_initial_absence_never_authorizes_deletion_of_a_later_branch(tmp_path, absent):
    from forge.tools.branch_removal import BranchRemovalAdapter, branch_removal_request

    request, _intent, git, validate, _adapter = binding(tmp_path)
    request = branch_removal_request(
        git.handle, policy_version=2, source_command_id=uuid4(), expected_head=None
    )
    intent = OperationIntent(
        run_id=request.run_id,
        kind=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
    )
    git.absent = absent
    git.retained_branch_head = lambda handle: None if git.absent else "c" * 40
    adapter = BranchRemovalAdapter(request, git, validate)
    for method in (adapter.invoke, adapter.reconcile):
        outcome = await method(intent)
        assert outcome.status is (OperationStatus.SUCCEEDED if absent else OperationStatus.FAILED)
        assert outcome.payload["expected_head"] is None
    assert git.calls == []
