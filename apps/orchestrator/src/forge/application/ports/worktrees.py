"""Immutable contracts for Forge-managed worktree operations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forge.domain.resource import WorktreeIdentity

_SHA = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedWorktree:
    """The exact path and base commit bound to one Forge worktree identity."""

    identity: WorktreeIdentity
    path: Path
    base_sha: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, WorktreeIdentity):
            raise TypeError("managed worktree identity must be a WorktreeIdentity")
        try:
            path = Path(os.fspath(self.path))
        except TypeError, ValueError:
            raise TypeError("managed worktree path must be a path") from None
        if not path.is_absolute() or not path.anchor:
            raise ValueError("managed worktree path must be absolute")
        if any(part in {".", ".."} for part in path.parts[1:]):
            raise ValueError("managed worktree path must be canonical")
        object.__setattr__(self, "path", path)
        if not isinstance(self.base_sha, str) or _SHA.fullmatch(self.base_sha) is None:
            raise ValueError("managed worktree base SHA must be lowercase hexadecimal")


@dataclass(frozen=True, slots=True, kw_only=True)
class GitOutput:
    """Bounded text returned by a controlled Git read operation."""

    text: str
    original_byte_count: int
    truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("Git output text must be a string")
        if type(self.original_byte_count) is not int or self.original_byte_count < 0:
            raise ValueError("Git output byte count must be nonnegative")
        if type(self.truncated) is not bool:
            raise TypeError("Git output truncation must be boolean")
        if self.original_byte_count < len(self.text.encode("utf-8")):
            raise ValueError("Git output byte count is smaller than returned text")

    @property
    def output(self) -> str:
        """Compatibility spelling for callers that call the text ``output``."""

        return self.text

    @property
    def stdout(self) -> str:
        """Compatibility spelling for callers that use process terminology."""

        return self.text


@dataclass(frozen=True, slots=True, kw_only=True)
class GitStatus(GitOutput):
    """Bounded porcelain status output."""


@dataclass(frozen=True, slots=True, kw_only=True)
class GitDiff(GitOutput):
    """Bounded binary-safe diff output."""


@dataclass(frozen=True, slots=True, kw_only=True)
class GitCommit:
    """The verified parent and result of one controlled local commit."""

    previous_sha: str
    new_sha: str

    def __post_init__(self) -> None:
        for value in (self.previous_sha, self.new_sha):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError("Git commit SHA must be lowercase hexadecimal")


class ControlledGitPort(Protocol):
    """Exact managed-worktree operations exposed to the application layer."""

    def create_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree: ...

    def remove_worktree(self, worktree: ManagedWorktree) -> None: ...

    def prune(self) -> None: ...

    def status(self, worktree: ManagedWorktree) -> GitStatus: ...

    def diff(self, worktree: ManagedWorktree) -> GitDiff: ...

    def branch_exists(self, worktree: ManagedWorktree) -> bool: ...

    def current_branch(self, worktree: ManagedWorktree) -> str: ...

    def head_sha(self, worktree: ManagedWorktree) -> str: ...

    def is_ancestor(self, worktree: ManagedWorktree) -> bool: ...

    def commit(self, worktree: ManagedWorktree, message: str) -> GitCommit: ...


# Keep the port aliases discoverable to later worktree lifecycle slices while
# exposing only the exact managed-worktree operations above.
ManagedWorktreePort = ControlledGitPort
GitPort = ControlledGitPort
GitStatusResult = GitStatus
GitDiffResult = GitDiff
GitCommitResult = GitCommit


__all__ = [
    "ControlledGitPort",
    "GitCommit",
    "GitCommitResult",
    "GitDiff",
    "GitDiffResult",
    "GitOutput",
    "GitPort",
    "GitStatus",
    "GitStatusResult",
    "ManagedWorktree",
    "ManagedWorktreePort",
]
