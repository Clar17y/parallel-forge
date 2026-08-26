"""Synchronous, bounded repository-tool application contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from forge.domain.artifact import validate_artifact_digest
from forge.domain.policy import ProjectPolicy

from .worktrees import ControlledGitPort, ManagedWorktree

MAX_REPOSITORY_WRITE_BYTES = 1024 * 1024


class RepositoryError(RuntimeError):
    """Base class for bounded repository-tool failures."""


class RepositoryAccessDenied(RepositoryError):
    """The requested repository resource is unavailable or not permitted."""


class PathEscape(RepositoryAccessDenied):
    """A repository-relative path failed the containment boundary."""


class RepositoryEncodingError(RepositoryError):
    """A repository file or process stream is not valid inspectable text."""


class BinaryRepositoryFile(RepositoryEncodingError):
    """A repository file contains binary data and is not readable as text."""


class ProcessExecutionError(RepositoryError):
    """A bounded local process invocation could not be completed safely."""

    def __init__(self) -> None:
        super().__init__("repository process execution failed")


@dataclass(frozen=True, slots=True, kw_only=True)
class RepositoryEntry:
    """One normalized repository entry returned by a future reader."""

    path: str
    kind: str
    byte_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FileRead:
    """One bounded UTF-8 repository file result."""

    path: str
    content: str
    original_byte_count: int
    truncated: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class FileWrite:
    """Digest-only evidence for one exact repository file publication."""

    path: str
    output_digest: str
    byte_count: int
    previous_digest: str | None = None
    created: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or self.path == ".":
            raise ValueError("file write path must be repository-relative")
        validate_artifact_digest(self.output_digest)
        if self.previous_digest is not None:
            validate_artifact_digest(self.previous_digest)
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("file write byte count must be nonnegative")
        if type(self.created) is not bool:
            raise TypeError("file write creation marker must be boolean")
        if self.created != (self.previous_digest is None):
            raise ValueError("file write creation evidence is inconsistent")


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchMatch:
    """One bounded literal-search match."""

    path: str
    line_number: int
    line_text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InstructionDocument:
    """Repository instruction content, always explicitly untrusted."""

    path: str
    content: str
    original_byte_count: int
    truncated: bool
    untrusted_repository_content: Literal[True] = field(default=True, init=False)

    def __post_init__(self) -> None:
        if self.untrusted_repository_content is not True:
            raise ValueError("repository instructions are always untrusted")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessResult:
    """A bounded process result with truthful per-stream metadata."""

    return_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    stdout_original_byte_count: int
    stderr_original_byte_count: int
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def exit_code(self) -> int | None:
        """Compatibility spelling for callers that name the code ``exit``."""

        return self.return_code


class RepositoryRoot(Protocol):
    """Canonical containment hooks required by every repository reader."""

    @property
    def path(self) -> Path: ...

    def contains(self, value: str, *, allow_root: bool = False) -> bool: ...

    def normalize(self, value: str, *, allow_root: bool = False) -> str: ...


class RepositoryReader(Protocol):
    """Synchronous read-only repository tool boundary."""

    @property
    def root(self) -> RepositoryRoot: ...

    def list_files(self, path: str = ".") -> Sequence[RepositoryEntry]: ...

    def read_file(self, path: str) -> FileRead: ...

    def search(self, literal: str, path: str = ".") -> Sequence[SearchMatch]: ...

    def read_instructions(self, target_path: str = ".") -> Sequence[InstructionDocument]: ...


class RepositoryWriter(Protocol):
    """Synchronous capability-bound repository file mutation boundary."""

    def is_bound_to(
        self,
        controlled_git: ControlledGitPort,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> bool: ...

    def write_file(self, path: str, content: str) -> FileWrite: ...

    def inspect_file(self, path: str, expected_digest: str) -> FileWrite | None: ...


class ProcessRunner(Protocol):
    """Synchronous no-shell process execution boundary."""

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        environment: Mapping[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult: ...


__all__ = [
    "MAX_REPOSITORY_WRITE_BYTES",
    "BinaryRepositoryFile",
    "FileRead",
    "FileWrite",
    "InstructionDocument",
    "PathEscape",
    "ProcessExecutionError",
    "ProcessResult",
    "ProcessRunner",
    "RepositoryAccessDenied",
    "RepositoryEncodingError",
    "RepositoryEntry",
    "RepositoryError",
    "RepositoryReader",
    "RepositoryRoot",
    "RepositoryWriter",
    "SearchMatch",
]
