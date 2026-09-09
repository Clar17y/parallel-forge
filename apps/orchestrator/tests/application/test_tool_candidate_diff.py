"""Named candidate reads expose controller evidence without accepting Git arguments."""

import hashlib

import pytest
from forge.application.ports.worktrees import GitCandidateDiff, GitDiff, ManagedWorktree
from forge.domain.actor import AgentRole
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import ToolCallStatus, ToolName, ToolRequest

from apps.orchestrator.tests.application.test_tool_audit import (
    BASE_SHA,
    PROJECT_ID,
    RUN_ID,
    TASK_ID,
    _context,
    _Git,
    _service,
)

PATCH = "diff --git a/file.txt b/file.txt\n+candidate\n"


class CandidateGit(_Git):
    calls = 0

    def candidate_diff(self, worktree):
        assert worktree == self._worktree
        self.calls += 1
        return GitCandidateDiff(
            head_sha="b" * 40,
            diff=GitDiff(text=PATCH, original_byte_count=len(PATCH), truncated=False),
            changed_paths=("file.txt",),
        )

    def diff(self, worktree):
        assert worktree == self._worktree
        return GitDiff(text="working changes", original_byte_count=15, truncated=False)


def _case(tmp_path, role=AgentRole.DEVELOPER):
    path = tmp_path / "worktree"
    path.mkdir()
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, "forge/change", False)
    managed = ManagedWorktree(identity=identity, path=path, base_sha=BASE_SHA)
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.REVIEWING if role is AgentRole.REVIEWER else RunState.IMPLEMENTING,
        policy_version=1,
        branch_name=identity.branch,
        base_sha=BASE_SHA,
        worktree_path=str(path),
    )
    git = CandidateGit(tmp_path, managed)
    service, work, _reader = _service(
        tmp_path, run=run, controlled_git=git, managed_worktree=managed
    )
    return service, _context(role=role, worktree_id=identity.worktree_name), git, work


@pytest.mark.asyncio
@pytest.mark.parametrize("role", (AgentRole.DEVELOPER, AgentRole.REVIEWER))
async def test_candidate_scope_returns_exact_committed_digest_and_audits(tmp_path, role):
    service, context, git, work = _case(tmp_path, role)
    result = await service.invoke(
        context, ToolRequest(name=ToolName.GIT_DIFF, arguments={"scope": "candidate"})
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.metadata["head_sha"] == "b" * 40
    assert result.metadata["diff_digest"] == hashlib.sha256(PATCH.encode()).hexdigest()
    assert result.metadata["changed_paths"] == ("file.txt",)
    assert result.metadata["text"] == PATCH
    assert git.calls == 1
    assert work.tool_calls.records[-1].normalized_arguments == {"scope": "candidate"}


@pytest.mark.asyncio
async def test_default_diff_keeps_working_tree_behavior(tmp_path):
    service, context, git, _work = _case(tmp_path)
    result = await service.invoke(context, ToolRequest(name=ToolName.GIT_DIFF, arguments={}))
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.metadata["text"] == "working changes"
    assert git.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ({"scope": "HEAD"}, {"scope": "candidate", "ref": "main"}))
async def test_diff_rejects_uncontrolled_scope_or_reference(tmp_path, arguments):
    service, context, git, _work = _case(tmp_path)
    result = await service.invoke(context, ToolRequest(name=ToolName.GIT_DIFF, arguments=arguments))
    assert result.status is ToolCallStatus.DENIED
    assert git.calls == 0
