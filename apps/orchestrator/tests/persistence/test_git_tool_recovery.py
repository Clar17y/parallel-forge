"""Historical Git publication settlement after read-only operation recovery."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.adapters.git_commit import PublishGitCommitAdapter
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.recovery import OperationExecutor, RecoveryService
from forge.application.services.tool_recovery import ToolRecoveryDisposition, ToolRecoveryService
from forge.application.services.tools import ControlledToolService, ToolInvocationError
from forge.domain.actor import AgentRole
from forge.domain.operation import OperationStatus, canonical_digest
from forge.domain.resource import WorktreeIdentity
from forge.domain.tool import ToolAuthorizationContext, ToolCallStatus, ToolName, ToolRequest
from forge.persistence.models import OperationIntent as OperationRow
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork

from apps.orchestrator.tests.application.test_tool_git_commit import _CommitGit
from apps.orchestrator.tests.persistence.test_tool_write_invocation import (
    _ControlledArtifactStore,
    _seed_test_database,
)


@pytest.mark.parametrize("corruption", [None, "publication_tree", "preparation_scope"])
async def test_reconciled_publication_restores_tool_audit_without_new_git_effect(
    session_factory, tmp_path, corruption
):
    project, run, step, execution, base, branch, _, path, _ = await _seed_test_database(
        session_factory, tmp_path
    )
    worktree = ManagedWorktree(
        identity=WorktreeIdentity.for_run(project, run, branch, False),
        path=Path(path),
        base_sha=base,
    )

    class Git(_CommitGit):
        inspections = 0

        def commit_prepared(self, worktree, prepared):
            self.published = super().commit_prepared(worktree, prepared)
            return self.published

        def inspect_prepared_commit(self, worktree, prepared):
            self.inspections += 1
            return self.published

    git = Git(worktree)
    store = _ControlledArtifactStore(tmp_path / "artifacts")
    operations = PostgresOperationRepository(session_factory)
    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory),
        controlled_git=git,
        worktree=worktree,
        artifact_store=store,
        operation_executor=OperationExecutor(operations),
    )
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=run,
        worktree_id=worktree.identity.worktree_name,
        policy_version=1,
        step_id=step,
        agent_execution_id=execution,
        invocation_id=uuid4(),
    )
    request = ToolRequest(
        name=ToolName.GIT_COMMIT, arguments={"message": "feat: retained publication"}
    )
    store.verify_returns = False
    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)
    store.verify_returns = True
    publication = await operations.get_by_idempotency_key(
        f"git.commit:{context.invocation_id}:publish"
    )
    assert publication.status is OperationStatus.PENDING
    async with session_factory() as session, session.begin():
        row = await session.get(OperationRow, publication.id)
        row.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    reconciled = await RecoveryService(operations).reconcile(
        publication.id, PublishGitCommitAdapter(git, worktree, operations)
    )
    assert reconciled.status is OperationStatus.SUCCEEDED
    recovery = ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), store)
    if corruption is not None:
        async with session_factory() as session, session.begin():
            if corruption == "publication_tree":
                row = await session.get(OperationRow, publication.id)
                row.outcome_payload = {**row.outcome_payload, "tree_sha": "f" * 40}
            else:
                row = await session.get(
                    OperationRow, reconciled.request_payload["preparation_intent_id"]
                )
                row.request_payload = {**row.request_payload, "step_id": str(uuid4())}
                row.request_digest = canonical_digest(row.request_payload)
    result = await recovery.recover_one(context.invocation_id)
    if corruption is not None:
        assert result.disposition is ToolRecoveryDisposition.INTERVENTION
        async with PostgresUnitOfWork(session_factory) as work:
            assert (
                await work.tool_calls.get(context.invocation_id)
            ).status is ToolCallStatus.RUNNING
            events = await work.events.list_after(run, 0)
            assert not any(
                event.payload.get("tool_call_id") == str(context.invocation_id) for event in events
            )
        assert (git.prepares, git.publishes, git.inspections) == (1, 1, 1)
        return
    assert result.disposition is ToolRecoveryDisposition.SETTLED
    assert (
        await recovery.recover_one(context.invocation_id)
    ).disposition is ToolRecoveryDisposition.TERMINAL
    replay = await service.invoke(context, request)
    assert replay.status is ToolCallStatus.SUCCEEDED
    assert (git.prepares, git.publishes, git.inspections) == (1, 1, 1)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(context.invocation_id)
        assert call.artifact_digests == replay.artifact_digests
        events = await work.events.list_after(run, 0)
        terminal = [event for event in events if event.payload.get("tool_call_id") == str(call.id)]
        assert len(terminal) == 1
        assert tuple(terminal[0].payload["artifact_digests"]) == call.artifact_digests
