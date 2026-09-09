"""Branch deletion requires durable proof of Forge resource ownership."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from forge.domain.event import RunEvent
from forge.domain.operation import OperationIntent, OperationStatus
from forge.domain.policy import DatabaseProvisioningPolicy, ProjectPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.tools.worktree import (
    _request,
    _teardown_checkpoint_payload,
    _teardown_request,
    _worktree_outcome,
)


@pytest.mark.asyncio
async def test_startup_branch_adapter_cannot_invoke_effects():
    from forge.application.services.recovery import RecoveryError
    from forge.worker.branch_runtime import BranchRemovalRuntime

    factory, git, executor = Mock(), Mock(), Mock()
    adapter = BranchRemovalRuntime(factory, git, executor).recovery_adapter()
    with pytest.raises(RecoveryError, match="cannot invoke"):
        await adapter.invoke(None)
    factory.assert_not_called()
    git.assert_not_called()
    executor.execute.assert_not_called()


def ownership_fixture(tmp_path, *, enabled=False, state=RunState.CANCELLED):
    run = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=state,
        policy_version=1,
        branch_name="forge/task",
        base_ref="main",
        base_sha="a" * 40,
        database_state=ResourceState.REMOVED if enabled else ResourceState.DISABLED,
    )
    policy = ProjectPolicy(
        id=run.project_id,
        version=1,
        repository_path=str(tmp_path),
        github_repository="owner/repo",
        default_branch="main",
        database=DatabaseProvisioningPolicy(
            enabled=enabled, admin_url_secret_reference="secret://test/admin" if enabled else None
        ),
    )
    identity = WorktreeIdentity.for_run(run.project_id, run.id, run.branch_name, enabled)
    receipts = {}
    for request in (_request(run, identity, policy), _teardown_request(run, identity, policy)):
        outcome = _worktree_outcome(request, identity)
        receipts[request.idempotency_key] = OperationIntent(
            run_id=run.id,
            kind=request.kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
            status=OperationStatus.SUCCEEDED,
            completed_at=datetime.now(UTC),
            outcome=outcome.payload,
            outcome_schema_version=1,
            remote_resource_id=outcome.remote_resource_id,
        )
    removal = receipts[request.idempotency_key]
    events = [
        RunEvent(
            run_id=run.id,
            run_version=run.version,
            event_type="resource.worktree_removed",
            payload=_teardown_checkpoint_payload(
                request,
                removal.id,
                target_state=ResourceState.ACTIVE if enabled else ResourceState.DISABLED,
            ),
        )
    ]
    work = SimpleNamespace(
        operations=SimpleNamespace(get_by_idempotency_key=AsyncMock(side_effect=receipts.get))
    )
    return run, policy, identity, receipts, events, work


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["missing", "pending", "foreign"])
async def test_branch_preflight_rejects_unowned_branch_before_git(tmp_path, proof):
    from forge.application.handlers.teardown import TeardownCommandRejected
    from forge.worker.branch_runtime import BranchRemovalRuntime

    run, policy, identity, receipts, _, work = ownership_fixture(tmp_path)
    key = _request(run, identity, policy).idempotency_key
    if proof == "missing":
        del receipts[key]
    elif proof == "pending":
        receipts[key] = replace(
            receipts[key],
            status=OperationStatus.PENDING,
            outcome=None,
            completed_at=None,
            remote_resource_id=None,
            outcome_schema_version=None,
        )
    else:
        receipts[key] = replace(receipts[key], request_digest="f" * 64)
    work.projects = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                canonical_path=policy.repository_path,
                github_repository=policy.github_repository,
                default_branch=policy.default_branch,
            )
        )
    )
    git = Mock()
    with pytest.raises(TeardownCommandRejected, match="ownership"):
        await BranchRemovalRuntime(Mock(), git, Mock()).observe_head(run, policy, work)
    git.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof", ["valid", "missing_receipt", "missing_checkpoint", "foreign_database"]
)
async def test_enabled_database_requires_exact_removal_receipt(tmp_path, proof):
    from forge.application.handlers.teardown import TeardownCommandRejected
    from forge.tools.database import DatabaseProvisioner
    from forge.tools.database import _request as database_request
    from forge.worker.branch_runtime import require_owned_removed_worktree

    run, policy, identity, receipts, events, work = ownership_fixture(tmp_path, enabled=True)
    request = database_request(identity, policy.version, "database.teardown", ResourceState.REMOVED)
    outcome = DatabaseProvisioner._outcome(
        state=ResourceState.REMOVED, identity=identity, secret_id=None
    )
    if proof != "missing_receipt":
        receipts[request.idempotency_key] = OperationIntent(
            run_id=run.id,
            kind=request.kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
            status=OperationStatus.SUCCEEDED,
            completed_at=datetime.now(UTC),
            outcome=outcome.payload
            if proof != "foreign_database"
            else {**outcome.payload, "database_name": "foreign"},
            outcome_schema_version=1,
        )
    if proof != "missing_checkpoint":
        events.append(
            replace(
                events[0],
                event_type="resource.database_removed",
                payload={**events[0].payload, "database_state": ResourceState.REMOVED.value},
            )
        )
    if proof == "valid":
        assert await require_owned_removed_worktree(run, policy, events, work) == identity
    else:
        with pytest.raises(TeardownCommandRejected, match="ownership"):
            await require_owned_removed_worktree(run, policy, events, work)


@pytest.mark.asyncio
async def test_owned_removed_worktree_allows_quiescent_intervention_run(tmp_path):
    from forge.worker.branch_runtime import require_owned_removed_worktree

    run, policy, identity, _, events, work = ownership_fixture(
        tmp_path, state=RunState.AWAITING_HUMAN_INTERVENTION
    )
    assert await require_owned_removed_worktree(run, policy, events, work) == identity


@pytest.mark.asyncio
async def test_owned_removed_worktree_requires_creation_receipt(tmp_path):
    from forge.application.handlers.teardown import TeardownCommandRejected
    from forge.worker.branch_runtime import require_owned_removed_worktree

    run, policy, identity, receipts, events, work = ownership_fixture(tmp_path)
    del receipts[_request(run, identity, policy).idempotency_key]
    with pytest.raises(TeardownCommandRejected, match="ownership"):
        await require_owned_removed_worktree(run, policy, events, work)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        None,
        "foreign_receipt",
        "wrong_outcome",
        "no_checkpoint",
        "duplicate_checkpoint",
        "operator_checkpoint",
        "base_branch",
        "checkpoint_boolean_version",
    ],
)
async def test_owned_removed_worktree_binds_receipt_and_checkpoint(tmp_path, corruption):
    from forge.application.handlers.teardown import TeardownCommandRejected
    from forge.worker.branch_runtime import require_owned_removed_worktree

    run, policy, identity, receipts, events, work = ownership_fixture(tmp_path)
    key = _request(run, identity, policy).idempotency_key
    if corruption == "foreign_receipt":
        receipts[key] = replace(receipts[key], run_id=uuid4())
    elif corruption == "wrong_outcome":
        receipts[key] = replace(
            receipts[key], outcome={"worktree_name": "foreign", "base_sha": run.base_sha}
        )
    elif corruption == "no_checkpoint":
        events.clear()
    elif corruption == "duplicate_checkpoint":
        events.append(events[0])
    elif corruption == "operator_checkpoint":
        events[0] = replace(events[0], actor_class="operator", actor_id=uuid4())
    elif corruption == "base_branch":
        run = replace(run, base_ref="refs/heads/forge/task")
    elif corruption == "checkpoint_boolean_version":
        events[0] = replace(events[0], payload={**events[0].payload, "policy_version": True})
    if corruption is None:
        assert await require_owned_removed_worktree(run, policy, events, work) == identity
    else:
        with pytest.raises(TeardownCommandRejected, match="ownership"):
            await require_owned_removed_worktree(run, policy, events, work)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [None, "head", "actor", "confirmation", "command", "quiescence", "advanced_version"],
)
async def test_branch_source_requires_exact_operator_admission(tmp_path, corruption):
    from forge.application.handlers.teardown import TeardownCommandRejected, _binding
    from forge.application.ports.runs import RunQuiescence
    from forge.domain.command import CommandEnvelope, CommandStatus
    from forge.domain.teardown import teardown_confirmation, teardown_identity
    from forge.tools.branch_removal import BranchRemovalBinding
    from forge.worker.branch_runtime import require_branch_admission

    run, policy, identity, _receipts, events, work = ownership_fixture(tmp_path)
    command = CommandEnvelope(
        id=uuid4(),
        status=CommandStatus.PENDING,
        payload_schema_version=1,
        attempt=0,
        available_at=datetime.now(UTC),
        lease_owner=None,
        lease_expires_at=None,
        run_id=run.id,
        command_type="teardown_run_resources",
        idempotency_key="remove",
        expected_run_version=run.version,
        actor_id=uuid4(),
        payload={
            "delete_branch": True,
            "confirm_branch_name": run.branch_name,
            "confirm_resource_identity": teardown_confirmation(run),
        },
    )
    binding = BranchRemovalBinding(
        run_id=run.id,
        project_id=run.project_id,
        source_command_id=command.id,
        policy_version=policy.version,
        branch=identity.branch,
        database_enabled=False,
        worktree_name=identity.worktree_name,
        base_sha=run.base_sha,
        expected_head="b" * 40,
    )
    admission = RunEvent(
        run_id=run.id,
        run_version=run.version,
        event_type="resource.teardown_admitted",
        actor_class="operator",
        actor_id=command.actor_id,
        payload={
            "source_command_id": str(command.id),
            "command_digest": _binding(command),
            "confirmation": teardown_confirmation(run),
            "identity": teardown_identity(run),
            "state": run.state.value,
            "branch_expected_head": binding.expected_head,
        },
    )
    if corruption == "head":
        admission = replace(
            admission, payload={**admission.payload, "branch_expected_head": "c" * 40}
        )
    elif corruption == "actor":
        admission = replace(admission, actor_id=uuid4())
    elif corruption == "confirmation":
        admission = replace(admission, payload={**admission.payload, "confirmation": "wrong"})
    elif corruption == "command":
        command = replace(command, payload={**command.payload, "delete_branch": False})
    events.append(admission)
    if corruption == "advanced_version":
        run = replace(run, version=run.version + 2)
    work.commands = SimpleNamespace(get=AsyncMock(return_value=command))
    work.runs = SimpleNamespace(
        prove_quiescent=AsyncMock(
            return_value=RunQuiescence(int(corruption == "quiescence"), 0, 0, 0, 1)
        )
    )
    if corruption in (None, "advanced_version"):
        assert (
            await require_branch_admission(
                run, policy, binding, events, work, own_unresolved_operation=True
            )
            == command
        )
    else:
        with pytest.raises(TeardownCommandRejected):
            await require_branch_admission(
                run, policy, binding, events, work, own_unresolved_operation=True
            )


@pytest.mark.asyncio
async def test_completed_branch_replay_cannot_create_missing_operation(tmp_path):
    from contextlib import asynccontextmanager

    from forge.application.handlers.teardown import TeardownCommandRejected
    from forge.application.ports.worktrees import ManagedWorktree
    from forge.worker.branch_runtime import BranchRemovalRuntime

    run, policy, identity, _receipts, _events, work = ownership_fixture(tmp_path)
    work.runs = SimpleNamespace(get_for_update=AsyncMock(return_value=run))
    work.operations.get_by_idempotency_key = AsyncMock(return_value=None)
    command = SimpleNamespace(run_id=run.id, id=uuid4())
    executor = SimpleNamespace(execute=AsyncMock())
    git = SimpleNamespace(
        expected_worktree=lambda *_: ManagedWorktree(
            identity=identity, path=Path(tmp_path / identity.worktree_name), base_sha=run.base_sha
        )
    )

    @asynccontextmanager
    async def factory():
        yield work

    runtime = BranchRemovalRuntime(factory, lambda _: git, executor)
    with pytest.raises(TeardownCommandRejected, match="receipt"):
        await runtime.validate_completed(command, policy, "b" * 40)
    executor.execute.assert_not_awaited()
