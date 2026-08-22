"""A narrow, noninteractive Git read boundary for managed worktrees."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path

from forge.application.ports.repository import ProcessResult
from forge.application.ports.worktrees import GitDiff, GitStatus, ManagedWorktree
from forge.domain.resource import WorktreeIdentity
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_METADATA_BYTES = 4096
_MAX_BRANCH_LENGTH = 255

_FORGE_NAME = "Forge"
_FORGE_EMAIL = "forge@example.test"


class ControlledGitError(RuntimeError):
    """A stable, redacted controlled-Git failure."""

    def __init__(self) -> None:
        super().__init__("controlled git operation failed")


class ControlledGit:
    """Invoke a fixed Git executable against one exact managed repository.

    The constructor owns all process and configuration controls.  Public methods
    accept only a Forge-created ``ManagedWorktree`` handle; no method accepts an
    argv fragment, path, ref, or Git configuration override from a caller.
    """

    def __init__(
        self,
        repository: CanonicalRoot,
        *,
        default_branch: str,
        state_root: str | os.PathLike[str],
        git_executable: str | os.PathLike[str],
        runner: ProcessRunner | None = None,
    ) -> None:
        if not isinstance(repository, CanonicalRoot):
            raise TypeError("controlled git requires a canonical repository root")
        _validate_branch(default_branch)
        self._repository = repository
        self._default_branch = default_branch
        self._managed_root = repository.path / ".worktrees"
        if os.path.lexists(self._managed_root):
            _reject_links(self._managed_root)
            if not self._managed_root.is_dir():
                raise ControlledGitError()

        self._git_executable = _resolve_git_executable(git_executable)
        self._state_root = _prepare_directory(Path(state_root))
        if _overlaps(self._state_root, repository.path):
            raise ControlledGitError()
        self._hooks_path = self._state_root / "hooks"
        self._global_config_path = self._state_root / "global.config"
        self._global_attributes_path = self._state_root / "global.attributes"
        _prepare_empty_directory(self._hooks_path)
        _prepare_empty_file(self._global_config_path)
        _prepare_empty_file(self._global_attributes_path)
        self._runner = runner or ProcessRunner(repository)

    def create_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        """Create and verify one exact managed worktree without cleanup guesses."""

        if not isinstance(identity, WorktreeIdentity):
            raise ControlledGitError()
        _validate_sha(base_sha)
        if _same_branch(identity.branch, self._default_branch):
            raise ControlledGitError()
        _validate_branch(identity.branch)
        self._scan_local_config(self._repository.path)
        self._verify_branch_format(identity.branch)
        resolved_base = self._parse_base_sha(base_sha)
        if resolved_base != base_sha:
            raise ControlledGitError()
        self._verify_managed_root_ignored()
        if self._branch_exists_at(self._repository.path, identity.branch):
            raise ControlledGitError()
        expected_metadata = self._registration_metadata(identity)
        if expected_metadata is not None:
            raise ControlledGitError()

        managed_root = _prepare_directory(self._managed_root)
        expected_path = managed_root / identity.worktree_name
        if _path_key(expected_path) != _path_key(self._managed_root / identity.worktree_name):
            raise ControlledGitError()
        if os.path.lexists(expected_path):
            _reject_links(expected_path)
            raise ControlledGitError()
        self._run(
            self._repository.path,
            (
                "worktree",
                "add",
                "-b",
                identity.branch,
                str(expected_path),
                base_sha,
            ),
        )

        handle = ManagedWorktree(identity=identity, path=expected_path, base_sha=base_sha)
        self._validate_handle(handle)
        if self.head_sha(handle) != base_sha or not self.is_ancestor(handle):
            raise ControlledGitError()
        return handle

    def remove_worktree(self, worktree: ManagedWorktree) -> None:
        """Remove one exact registered worktree and retain its branch."""

        identity, expected_path = self._validate_handle_shape(worktree)
        metadata = self._registration_metadata(identity)
        path_exists = os.path.lexists(expected_path)
        if not path_exists:
            if metadata is None:
                return
            self._verify_metadata_target(metadata, expected_path / ".git")
            self.prune()
            if self._registration_metadata(identity) is not None:
                raise ControlledGitError()
            return

        _reject_links(expected_path)
        if not expected_path.is_dir() or metadata is None:
            raise ControlledGitError()
        self._verify_registration(expected_path, identity)
        self._verify_current_branch(worktree)
        self._scan_local_config(expected_path)
        self._run(
            self._repository.path,
            ("worktree", "remove", "--force", "--", str(expected_path)),
        )
        if os.path.lexists(expected_path) or self._registration_metadata(identity) is not None:
            raise ControlledGitError()

    def prune(self) -> None:
        """Prune only stale Git worktree registration metadata."""

        self._scan_local_config(self._repository.path)
        self._run(self._repository.path, ("worktree", "prune", "--expire=now"))

    def status(self, worktree: ManagedWorktree) -> GitStatus:
        """Return bounded deterministic porcelain-v1 status output."""

        self._validate_handle(worktree, verify_branch=False)
        self._scan_local_config(worktree.path)
        self._verify_current_branch(worktree)
        result = self._run(
            worktree.path,
            ("status", "--porcelain=v1", "--branch", "--untracked-files=all", "-z", "--"),
        )
        return GitStatus(
            text=result.stdout,
            original_byte_count=_original_count(result, "stdout"),
            truncated=_truncated(result, "stdout"),
        )

    def diff(self, worktree: ManagedWorktree) -> GitDiff:
        """Return bounded binary-safe diff output against the worktree HEAD."""

        self._validate_handle(worktree, verify_branch=False)
        self._scan_local_config(worktree.path)
        self._verify_current_branch(worktree)
        result = self._run(
            worktree.path,
            (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--full-index",
                "--no-color",
                "HEAD",
                "--",
            ),
        )
        return GitDiff(
            text=result.stdout,
            original_byte_count=_original_count(result, "stdout"),
            truncated=_truncated(result, "stdout"),
        )

    def branch_exists(self, worktree: ManagedWorktree) -> bool:
        """Return whether the handle's exact branch exists locally."""

        self._validate_handle(worktree)
        result = self._run(
            worktree.path,
            ("show-ref", "--verify", "--quiet", f"refs/heads/{worktree.identity.branch}"),
            allow_return_codes=(0, 1),
        )
        return _return_code(result) == 0

    def current_branch(self, worktree: ManagedWorktree) -> str:
        """Return the handle's recorded branch after exact-path validation."""

        self._validate_handle(worktree, verify_branch=False)
        return self._verify_current_branch(worktree)

    def head_sha(self, worktree: ManagedWorktree) -> str:
        """Return the lowercase, complete HEAD commit SHA."""

        self._validate_handle(worktree)
        result = self._run(worktree.path, ("rev-parse", "--verify", "HEAD^{commit}"))
        return _parse_sha(result)

    def is_ancestor(self, worktree: ManagedWorktree) -> bool:
        """Return whether an exact commit is an ancestor of the handle's HEAD."""

        self._validate_handle(worktree)
        ancestor = worktree.base_sha
        result = self._run(
            worktree.path,
            ("merge-base", "--is-ancestor", ancestor, "HEAD"),
            allow_return_codes=(0, 1),
        )
        return _return_code(result) == 0

    def _validate_handle_shape(self, worktree: ManagedWorktree) -> tuple[WorktreeIdentity, Path]:
        if not isinstance(worktree, ManagedWorktree):
            raise ControlledGitError()
        identity = worktree.identity
        if not isinstance(identity, WorktreeIdentity):
            raise ControlledGitError()
        if _same_branch(identity.branch, self._default_branch):
            raise ControlledGitError()
        expected = self._managed_root / identity.worktree_name
        if _path_key(worktree.path) != _path_key(expected):
            raise ControlledGitError()
        _validate_sha(worktree.base_sha)
        return identity, expected

    def _validate_handle(self, worktree: ManagedWorktree, *, verify_branch: bool = True) -> None:
        identity, expected = self._validate_handle_shape(worktree)
        try:
            _reject_links(worktree.path)
            if not worktree.path.is_dir() or _path_key(worktree.path) != _path_key(expected):
                raise ControlledGitError()
            self._verify_registration(worktree.path, identity)
        except ControlledGitError:
            raise
        except OSError, RuntimeError, ValueError:
            raise ControlledGitError() from None
        if verify_branch:
            self._verify_current_branch(worktree)

    def _verify_current_branch(self, worktree: ManagedWorktree) -> str:
        result = self._run(worktree.path, ("branch", "--show-current"))
        branch = _parse_single_line(result)
        if branch != worktree.identity.branch:
            raise ControlledGitError()
        return branch

    def _scan_local_config(self, worktree: Path) -> None:
        """Refuse local settings that could execute repository-controlled code."""

        result = self._run(
            worktree,
            ("config", "--local", "--no-includes", "--name-only", "--null", "--list"),
        )
        if _truncated(result, "stdout") or _truncated(result, "stderr"):
            raise ControlledGitError()
        output = result.stdout
        if not isinstance(output, str) or "\ufffd" in output:
            raise ControlledGitError()
        keys = output.split("\x00")
        if keys and keys[-1] == "":
            keys.pop()
        if any(not key or _unsafe_local_key(key) for key in keys):
            raise ControlledGitError()

    def _run(
        self,
        worktree: Path,
        arguments: Sequence[str],
        *,
        allow_return_codes: tuple[int, ...] = (0,),
    ) -> ProcessResult:
        self._assert_trusted_state()
        argv = [*self._prefix(worktree), *arguments]
        environment = self._environment()
        cwd = str(worktree)
        try:
            result = self._runner.run_argv(
                argv,
                cwd=cwd,
                environment=environment,
            )
        except OSError, RuntimeError, TypeError, ValueError:
            raise ControlledGitError() from None
        if not _valid_result(result):
            raise ControlledGitError()
        return_code = _return_code(result)
        if return_code not in allow_return_codes:
            raise ControlledGitError()
        if _timed_out(result) or return_code is None:
            raise ControlledGitError()
        return result

    def _prefix(self, worktree: Path) -> tuple[str, ...]:
        return (
            str(self._git_executable),
            "-C",
            str(worktree),
            "--no-pager",
            "-c",
            f"core.hooksPath={self._hooks_path}",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "credential.helper=",
            "-c",
            "credential.interactive=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "diff.external=",
            "-c",
            f"core.attributesFile={self._global_attributes_path}",
            "-c",
            f"user.name={_FORGE_NAME}",
            "-c",
            f"user.email={_FORGE_EMAIL}",
        )

    def _environment(self) -> dict[str, str]:
        allowed_names = {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMP",
            "TEMP",
            "TMPDIR",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
        }
        environment: dict[str, str] = {}
        for key, value in os.environ.items():
            comparison = key.upper() if os.name == "nt" else key
            if comparison not in allowed_names:
                continue
            if not isinstance(value, str) or "\x00" in value:
                continue
            environment[key] = value
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": str(self._global_config_path),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "GIT_PAGER": "",
                "GIT_EDITOR": "",
            }
        )
        return environment

    def _assert_trusted_state(self) -> None:
        try:
            _reject_links(self._state_root)
            _reject_links(self._hooks_path)
            if not self._hooks_path.is_dir() or any(self._hooks_path.iterdir()):
                raise ControlledGitError()
            for path in (self._global_config_path, self._global_attributes_path):
                _reject_links(path)
                if not path.is_file() or path.stat().st_size != 0:
                    raise ControlledGitError()
        except ControlledGitError:
            raise
        except OSError, RuntimeError, ValueError:
            raise ControlledGitError() from None

    def _verify_branch_format(self, branch: str) -> None:
        self._run(self._repository.path, ("check-ref-format", "--branch", branch))

    def _parse_base_sha(self, base_sha: str) -> str:
        result = self._run(
            self._repository.path,
            ("rev-parse", "--verify", f"{base_sha}^{{commit}}"),
        )
        return _parse_sha(result)

    def _verify_managed_root_ignored(self) -> None:
        self._run(self._repository.path, ("check-ignore", "--quiet", "--", ".worktrees/"))

    def _branch_exists_at(self, worktree: Path, branch: str) -> bool:
        result = self._run(
            worktree,
            ("show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
            allow_return_codes=(0, 1),
        )
        return _return_code(result) == 0

    def _registration_metadata(self, identity: WorktreeIdentity) -> Path | None:
        git_directory = self._repository.path / ".git"
        if os.path.lexists(git_directory):
            _reject_links(git_directory)
            if not git_directory.is_dir():
                raise ControlledGitError()
        metadata_root = git_directory / "worktrees"
        if not os.path.lexists(metadata_root):
            return None
        _reject_links(metadata_root)
        if not metadata_root.is_dir():
            raise ControlledGitError()
        metadata = metadata_root / identity.worktree_name
        if not os.path.lexists(metadata):
            return None
        _reject_links(metadata)
        if not metadata.is_dir():
            raise ControlledGitError()
        return metadata

    def _verify_registration(self, worktree: Path, identity: WorktreeIdentity) -> None:
        git_marker = worktree / ".git"
        _reject_links(git_marker)
        if not git_marker.is_file():
            raise ControlledGitError()
        marker = _read_small_text(git_marker)
        if not marker.startswith("gitdir: ") or marker.count("\n") != 1:
            raise ControlledGitError()
        raw_metadata = marker.removesuffix("\n")[8:]
        metadata = Path(raw_metadata)
        if not metadata.is_absolute():
            metadata = worktree / metadata
        metadata = _canonical_no_links(metadata)
        expected = self._registration_metadata(identity)
        if expected is None or _path_key(metadata) != _path_key(expected):
            raise ControlledGitError()
        self._verify_metadata_target(metadata, git_marker)

    def _verify_metadata_target(self, metadata: Path, expected_target: Path) -> None:
        metadata_gitdir = metadata / "gitdir"
        _reject_links(metadata_gitdir)
        if not metadata_gitdir.is_file():
            raise ControlledGitError()
        registered_target = _read_small_text(metadata_gitdir).removesuffix("\n")
        target = Path(registered_target)
        if not target.is_absolute():
            target = metadata / target
        target = _canonical_no_links_allow_missing(target)
        expected_target = _canonical_no_links_allow_missing(expected_target)
        if _path_key(target) != _path_key(expected_target):
            raise ControlledGitError()


def _resolve_git_executable(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(os.fspath(value))
    except TypeError, ValueError:
        raise ControlledGitError() from None
    if not path.is_absolute():
        raise ControlledGitError()
    try:
        _reject_links(path)
        resolved = path.resolve(strict=True)
        metadata = os.stat(resolved, follow_symlinks=False)
    except OSError, RuntimeError, ValueError:
        raise ControlledGitError() from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ControlledGitError()
    return resolved


def _prepare_directory(path: Path) -> Path:
    if not path.is_absolute() or not path.anchor:
        raise ControlledGitError()
    try:
        _ensure_directory(path)
        _reject_links(path)
        return path.resolve(strict=True)
    except OSError, RuntimeError, ValueError:
        raise ControlledGitError() from None


def _ensure_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ControlledGitError()
        current = parent
    _reject_links(current)
    if not current.is_dir():
        raise ControlledGitError()
    for candidate in reversed(missing):
        candidate.mkdir()
        _reject_links(candidate)
        if not candidate.is_dir():
            raise ControlledGitError()


def _prepare_empty_directory(path: Path) -> None:
    if os.path.lexists(path):
        _reject_links(path)
        if not path.is_dir() or any(path.iterdir()):
            raise ControlledGitError()
        return
    path.mkdir()
    _reject_links(path)


def _prepare_empty_file(path: Path) -> None:
    if os.path.lexists(path):
        _reject_links(path)
        if not path.is_file() or path.stat().st_size != 0:
            raise ControlledGitError()
        return
    try:
        with path.open("xb"):
            pass
    except OSError, ValueError:
        raise ControlledGitError() from None
    _reject_links(path)


def _reject_links(path: Path) -> None:
    current = Path(path.anchor)
    if not current:
        raise ControlledGitError()
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError, ValueError:
            raise ControlledGitError() from None
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise ControlledGitError()


def _reject_existing_links(path: Path) -> None:
    current = Path(path.anchor)
    if not current:
        raise ControlledGitError()
    for component in path.parts[1:]:
        current /= component
        if not os.path.lexists(current):
            break
        try:
            metadata = os.lstat(current)
        except OSError, ValueError:
            raise ControlledGitError() from None
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise ControlledGitError()


def _canonical_no_links(path: Path) -> Path:
    _reject_links(path)
    return path.resolve(strict=True)


def _canonical_no_links_allow_missing(path: Path) -> Path:
    _reject_existing_links(path)
    return path.resolve(strict=False)


def _read_small_text(path: Path) -> str:
    try:
        value = path.read_bytes()
    except OSError, ValueError:
        raise ControlledGitError() from None
    if len(value) > _MAX_METADATA_BYTES:
        raise ControlledGitError()
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise ControlledGitError() from None


def _validate_branch(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_BRANCH_LENGTH
        or value.startswith(("-", "/"))
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or ".." in value
        or "@{" in value
        or value.endswith((".", "/"))
        or "//" in value
    ):
        raise ValueError("invalid branch")


def _same_branch(first: str, second: str) -> bool:
    return first.casefold() == second.casefold() if os.name == "nt" else first == second


def _validate_sha(value: object) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ControlledGitError()


def _path_key(path: Path) -> str:
    value = str(path).replace("\\", "/").rstrip("/")
    if not value:
        value = "/"
    return value.casefold() if os.name == "nt" else value


def _overlaps(first: Path, second: Path) -> bool:
    first_key = _path_key(first)
    second_key = _path_key(second)
    return (
        first_key == second_key
        or first_key.startswith(second_key + "/")
        or second_key.startswith(first_key + "/")
    )


def _unsafe_local_key(key: str) -> bool:
    lowered = key.casefold()
    blocked_fragments = (
        "include",
        "hook",
        "filter",
        "fsmonitor",
        "untrackedcache",
        "external",
        "textconv",
        "credential",
        "pager",
        "editor",
        "askpass",
        "ssh",
        "proxy",
        "attributesfile",
        "diff.filter",
        "interactive.difffilter",
    )
    return any(fragment in lowered for fragment in blocked_fragments)


def _valid_result(result: object) -> bool:
    return (
        hasattr(result, "stdout")
        and hasattr(result, "stderr")
        and isinstance(result.stdout, str)
        and isinstance(result.stderr, str)
    )


def _return_code(result: object) -> int | None:
    value = getattr(result, "return_code", getattr(result, "returncode", None))
    return value if type(value) is int or value is None else None


def _timed_out(result: object) -> bool:
    value = getattr(result, "timed_out", False)
    return value is True


def _truncated(result: object, stream: str) -> bool:
    value = getattr(result, f"{stream}_truncated", False)
    return value is True


def _original_count(result: object, stream: str) -> int:
    value = getattr(result, f"{stream}_original_byte_count", None)
    if type(value) is int and value >= 0:
        return value
    text = getattr(result, stream)
    return len(text.encode("utf-8"))


def _parse_sha(result: ProcessResult) -> str:
    if _truncated(result, "stdout") or _truncated(result, "stderr"):
        raise ControlledGitError()
    value = _parse_single_line(result)
    _validate_sha(value)
    return value


def _parse_single_line(result: ProcessResult) -> str:
    if _truncated(result, "stdout") or _truncated(result, "stderr"):
        raise ControlledGitError()
    output = result.stdout
    if not isinstance(output, str):
        raise ControlledGitError()
    lines = output.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise ControlledGitError()
    if any(ord(character) < 0x20 for character in lines[0]):
        raise ControlledGitError()
    return lines[0]


__all__ = ["ControlledGit", "ControlledGitError"]
