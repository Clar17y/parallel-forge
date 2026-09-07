"""Focused verification of structured Developer output against managed Git state."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import GitCandidateDiff, GitDiff, ManagedWorktree
from forge.application.services.developer_result import DeveloperResultVerifier
from forge.domain.agent import DeveloperOutput
from forge.domain.plan import PlanOutput
from forge.domain.resource import WorktreeIdentity

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


class FakeGit:
    def __init__(self, diff_text: str, *, truncated: bool = False) -> None:
        self.diff_text = diff_text
        self.truncated = truncated

    def head_sha(self, worktree: ManagedWorktree) -> str:
        return HEAD_SHA

    def is_ancestor(self, worktree: ManagedWorktree) -> bool:
        return True

    def diff(self, worktree: ManagedWorktree) -> GitDiff:
        return GitDiff(
            text=self.diff_text,
            original_byte_count=len(self.diff_text.encode()),
            truncated=self.truncated,
        )

    def candidate_diff(self, worktree: ManagedWorktree) -> GitCandidateDiff:
        diff = self.diff(worktree)
        path = diff.text.split(" a/", 1)[1].split(" b/", 1)[0]
        return GitCandidateDiff(
            head_sha=self.head_sha(worktree),
            diff=diff,
            changed_paths=(path,),
        )


def _worktree() -> ManagedWorktree:
    return ManagedWorktree(
        identity=WorktreeIdentity.for_developer(uuid4(), "feature/test"),
        path=Path("C:/managed/worktree"),
        base_sha=BASE_SHA,
    )


def _plan(*, dependencies: tuple[str, ...] = ()) -> PlanOutput:
    return PlanOutput(
        summary="plan",
        assumptions=(),
        affected_components=("orchestrator",),
        steps=("implement",),
        required_checks=("unit",),
        risks=("none",),
        security_considerations=(),
        dependency_changes=dependencies,
    )


def _output(
    *, paths: tuple[str, ...] = ("src/app.py",), deviations: tuple[str, ...] = ()
) -> DeveloperOutput:
    diff = f"diff --git a/{paths[0]} b/{paths[0]}\n+change\n"
    return DeveloperOutput(
        summary="done",
        changed_paths=paths,
        tests_added_or_changed=(),
        named_checks_run=("unit",),
        local_commit_sha=HEAD_SHA,
        diff_digest=hashlib.sha256(diff.encode()).hexdigest(),
        unresolved_concerns=(),
        plan_deviations=deviations,
    )


@pytest.mark.asyncio
async def test_verifies_head_ancestor_digest_and_changed_paths() -> None:
    diff = "diff --git a/src/app.py b/src/app.py\n+change\n"
    result = await DeveloperResultVerifier().verify(_output(), _plan(), FakeGit(diff), _worktree())

    assert result.accepted is True
    assert result.intervention_reason is None
    assert result.actual_changed_paths == ("src/app.py",)


@pytest.mark.asyncio
async def test_rejects_stale_or_unrelated_commit() -> None:
    output = _output()

    class StaleGit(FakeGit):
        def head_sha(self, worktree: ManagedWorktree) -> str:
            return "c" * 40

    result = await DeveloperResultVerifier().verify(
        output,
        _plan(),
        StaleGit("diff --git a/src/app.py b/src/app.py\n+change\n"),
        _worktree(),
    )

    assert result.accepted is False
    assert result.intervention_reason == "local_commit_sha_does_not_match_worktree_head"


@pytest.mark.asyncio
async def test_rejects_path_mismatch_and_plan_deviation() -> None:
    diff = "diff --git a/src/app.py b/src/app.py\n+change\n"
    output = _output(paths=("src/other.py",)).model_copy(
        update={"diff_digest": hashlib.sha256(diff.encode()).hexdigest()}
    )
    result = await DeveloperResultVerifier().verify(output, _plan(), FakeGit(diff), _worktree())
    assert result.intervention_reason == "changed_paths_do_not_match_diff"

    result = await DeveloperResultVerifier().verify(
        _output(deviations=("changed scope",)), _plan(), FakeGit(diff), _worktree()
    )
    assert result.intervention_reason == "plan_deviation_reported"


@pytest.mark.asyncio
async def test_reports_dependency_authorization_limitation_without_guessing() -> None:
    diff = "diff --git a/pyproject.toml b/pyproject.toml\n+change\n"
    result = await DeveloperResultVerifier().verify(
        _output(paths=("pyproject.toml",)), _plan(), FakeGit(diff), _worktree()
    )

    assert result.accepted is True
    assert result.dependency_authorization_limitation is not None


@pytest.mark.asyncio
async def test_rejects_truncated_candidate_diff() -> None:
    diff = "diff --git a/src/app.py b/src/app.py\n+change\n"
    result = await DeveloperResultVerifier().verify(
        _output(), _plan(), FakeGit(diff, truncated=True), _worktree()
    )

    assert result.accepted is False
    assert result.intervention_reason == "controlled_diff_is_truncated"
