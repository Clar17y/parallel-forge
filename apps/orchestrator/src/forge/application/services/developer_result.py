"""Verify a Developer's claimed local commit and diff evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from forge.application.ports.worktrees import (
    ControlledGitPort,
    GitCandidateDiff,
    GitCandidateFile,
    ManagedWorktree,
)
from forge.application.services.dependency_scope import (
    DependencyDelta,
    DependencyScopeError,
    dependency_deltas,
    verify_dependency_declarations,
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

    def candidate_file(self, worktree: ManagedWorktree, path: str) -> GitCandidateFile: ...


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

        dependency_paths = _dependency_paths(paths)
        unsupported = _unsupported_dependency_path(paths)
        if unsupported is not None:
            return DeveloperResultVerification(False, unsupported, head_sha, actual_digest, paths)
        deltas: list[DependencyDelta] = []
        for path in dependency_paths:
            try:
                snapshot = git.candidate_file(worktree, path)
                if snapshot.path != path or snapshot.head_sha != head_sha:
                    return DeveloperResultVerification(
                        False, "candidate_file_head_drifted", head_sha, actual_digest, paths
                    )
                deltas.extend(dependency_deltas(snapshot))
            except DependencyScopeError:
                return DeveloperResultVerification(
                    False,
                    "dependency_manifest_is_unsupported_or_ambiguous",
                    head_sha,
                    actual_digest,
                    paths,
                )
        dependency_reason = verify_dependency_declarations(tuple(deltas), plan.dependency_changes)
        if dependency_reason is not None:
            return DeveloperResultVerification(
                False, dependency_reason, head_sha, actual_digest, paths
            )
        return DeveloperResultVerification(True, None, head_sha, actual_digest, paths)


_DEPENDENCY_NAMES = frozenset({"pyproject.toml", "package.json"})
_UNSUPPORTED_DEPENDENCY_NAMES = frozenset(
    {
        "uv.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "pipfile.lock",
        ".npmrc",
    }
)


def _dependency_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(path for path in paths if path.rsplit("/", 1)[-1] in _DEPENDENCY_NAMES)


def _unsupported_dependency_path(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        name = path.rsplit("/", 1)[-1].casefold()
        if name in _DEPENDENCY_NAMES and name != path.rsplit("/", 1)[-1]:
            return "dependency_manifest_format_is_unsupported"
        if name in _UNSUPPORTED_DEPENDENCY_NAMES or name.startswith("constraints"):
            return "dependency_manifest_format_is_unsupported"
        if name.startswith("requirements") and name.endswith((".txt", ".in")):
            return "dependency_manifest_format_is_unsupported"
        if "/requirements/" in f"/{path.casefold()}/":
            return "dependency_manifest_format_is_unsupported"
    return None


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
