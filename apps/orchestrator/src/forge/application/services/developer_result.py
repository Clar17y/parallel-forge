"""Verify a Developer's claimed local commit and diff evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from forge.application.ports.worktrees import (
    ControlledGitPort,
    GitCandidateDiff,
    ManagedWorktree,
)
from forge.domain.agent import DeveloperOutput
from forge.domain.plan import PlanOutput


class DeveloperResultError(RuntimeError):
    """Raised only when the verifier is called with an invalid boundary object."""


@dataclass(frozen=True, slots=True)
class DeveloperResultVerification:
    """Bounded, non-persistent evidence from one Developer result check."""

    accepted: bool
    intervention_reason: str | None
    actual_head_sha: str | None
    actual_diff_digest: str | None
    actual_changed_paths: tuple[str, ...]
    dependency_authorization_limitation: str | None = None


class _GitReadPort(Protocol):
    def candidate_diff(self, worktree: ManagedWorktree) -> GitCandidateDiff: ...


class DeveloperResultVerifier:
    """Perform read-only checks needed before accepting Developer output."""

    async def verify(
        self,
        output: DeveloperOutput,
        plan: PlanOutput,
        git: _GitReadPort,
        worktree: ManagedWorktree,
    ) -> DeveloperResultVerification:
        if not isinstance(output, DeveloperOutput) or not isinstance(plan, PlanOutput):
            raise DeveloperResultError("developer result boundary objects are invalid")
        if not isinstance(worktree, ManagedWorktree):
            raise DeveloperResultError("developer result worktree is invalid")
        if not callable(getattr(git, "candidate_diff", None)):
            raise DeveloperResultError("developer result Git port is invalid")

        candidate = git.candidate_diff(worktree)
        if not isinstance(candidate, GitCandidateDiff):
            raise DeveloperResultError("developer result candidate diff is invalid")
        head_sha = candidate.head_sha
        if output.local_commit_sha != head_sha:
            return DeveloperResultVerification(
                False,
                "local_commit_sha_does_not_match_worktree_head",
                head_sha,
                None,
                (),
            )
        diff = candidate.diff
        if diff.truncated:
            return DeveloperResultVerification(
                False, "controlled_diff_is_truncated", head_sha, None, ()
            )
        diff_bytes = diff.text.encode("utf-8")
        actual_digest = hashlib.sha256(diff_bytes).hexdigest()
        paths = candidate.changed_paths
        if output.diff_digest != actual_digest:
            return DeveloperResultVerification(
                False, "diff_digest_does_not_match_worktree", head_sha, actual_digest, paths
            )
        if output.changed_paths != paths:
            return DeveloperResultVerification(
                False, "changed_paths_do_not_match_diff", head_sha, actual_digest, paths
            )
        if output.plan_deviations:
            return DeveloperResultVerification(
                False, "plan_deviation_reported", head_sha, actual_digest, paths
            )

        # PlanOutput stores dependency descriptions, but no authorized path or
        # package identity. Keep this limitation explicit for the orchestrator.
        limitation = "dependency authorization is not represented by the PlanOutput contract"
        return DeveloperResultVerification(True, None, head_sha, actual_digest, paths, limitation)


async def verify_developer_output(
    output: DeveloperOutput,
    *,
    git: ControlledGitPort,
    worktree: ManagedWorktree,
    approved_plan: PlanOutput,
) -> DeveloperResultVerification:
    """Stable function API for Developer execution orchestration."""

    return await DeveloperResultVerifier().verify(output, approved_plan, git, worktree)


__all__ = [
    "DeveloperResultError",
    "DeveloperResultVerification",
    "DeveloperResultVerifier",
    "verify_developer_output",
]
