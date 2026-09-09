"""Deterministic, read-only local Git repository inspection.

The adapter intentionally exposes one small operation to the application layer.  It
does not fetch, checkout, write configuration, or resolve credentials.  Every Git
invocation is made with an argument vector and a disabled terminal prompt so a
malformed or hostile repository cannot turn registration into an interactive or
unbounded process.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from forge.application.ports.projects import RepositoryInspection

_GIT_OUTPUT_LIMIT = 8192
_GITHUB_HOST = "github.com"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_IDENTITY = re.compile(r"^[^/\\:]+/[^/\\:]+$")
_SCP_REMOTE = re.compile(r"^(?:[^@/:]+@)?([^/:]+):(.+)$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


class RepositoryInspectionError(ValueError):
    """A generic, safe repository validation failure."""

    def __init__(self) -> None:
        super().__init__("repository validation failed")


class LocalGitRepositoryInspector:
    """Inspect one local repository without following links or leaking Git output."""

    def __init__(self, *, runner: GitRunner | None = None) -> None:
        self._runner = runner or subprocess.run

    def inspect(
        self,
        *,
        repository_path: str,
        data_root: str,
        github_repository: str,
        default_branch: str,
    ) -> RepositoryInspection:
        """Return canonical repository identity and its current default-branch SHA."""

        repository = _canonical_directory(repository_path)
        data = _canonical_directory(data_root)
        if _contains(repository, data) or _contains(data, repository):
            raise RepositoryInspectionError()

        top_level = self._git_output(repository, ["rev-parse", "--show-toplevel"])
        try:
            reported_top_level = _canonical_directory(top_level)
        except RepositoryInspectionError:
            raise RepositoryInspectionError() from None
        if _path_key(reported_top_level) != _path_key(repository):
            raise RepositoryInspectionError()

        remote = self._git_output(repository, ["config", "--local", "--get", "remote.origin.url"])
        if _normalize_github_repository(remote) != _normalize_github_repository(
            github_repository, allow_bare=True
        ):
            raise RepositoryInspectionError()

        _validate_branch(default_branch)
        self._git_output(repository, ["check-ref-format", "--branch", default_branch])
        base_ref = f"refs/heads/{default_branch}"
        base_sha = self._git_output(
            repository,
            ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        )
        if _SHA.fullmatch(base_sha) is None:
            raise RepositoryInspectionError()
        return RepositoryInspection(
            canonical_path=str(repository),
            github_repository=_normalize_github_repository(github_repository, allow_bare=True),
            default_branch=default_branch,
            base_ref=base_ref,
            base_sha=base_sha,
        )

    def _git_output(self, repository: Path, arguments: list[str]) -> str:
        argv = ["git", "-C", str(repository), *arguments]
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "true",
            }
        )
        try:
            result = self._runner(
                argv,
                shell=False,
                cwd=str(repository),
                env=environment,
                timeout=10,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except (
            OSError,
            subprocess.SubprocessError,
            TimeoutError,
            TypeError,
            ValueError,
            UnicodeError,
        ):
            raise RepositoryInspectionError() from None
        if result.returncode != 0:
            raise RepositoryInspectionError()
        output = result.stdout
        if (
            not isinstance(output, str)
            or len(output.encode("utf-8", errors="replace")) > _GIT_OUTPUT_LIMIT
        ):
            raise RepositoryInspectionError()
        return output.strip()


def canonical_path_key(path: str | Path) -> str:
    """Return the case-insensitive identity key used for project paths."""

    return _path_key(Path(path))


def _canonical_directory(value: str | Path) -> Path:
    try:
        path = Path(value)
    except TypeError, ValueError:
        raise RepositoryInspectionError() from None
    if not path.is_absolute():
        raise RepositoryInspectionError()
    _reject_links_and_missing_components(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError, RuntimeError:
        raise RepositoryInspectionError() from None
    if not resolved.is_dir():
        raise RepositoryInspectionError()
    return resolved


def _reject_links_and_missing_components(path: Path) -> None:
    """Walk lstat-visible components before resolving so links are never followed."""

    current = Path(path.anchor)
    # ``Path.parts`` includes the drive/root anchor as its first item on both
    # Windows and POSIX.  The anchor itself is trusted OS infrastructure; every
    # configured component below it is inspected individually.
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError, ValueError:
            raise RepositoryInspectionError() from None
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise RepositoryInspectionError()


def _contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def _path_key(path: Path) -> str:
    value = str(path).replace("\\", "/").rstrip("/")
    if not value:
        value = "/"
    return value.casefold()


def _normalize_github_repository(value: str, *, allow_bare: bool = False) -> str:
    if not isinstance(value, str):
        raise RepositoryInspectionError()
    raw = value.strip()
    if not raw or "\x00" in raw:
        raise RepositoryInspectionError()

    if allow_bare and _REPOSITORY_IDENTITY.fullmatch(raw):
        owner, name = raw.split("/", 1)
        return f"{owner.casefold()}/{name.casefold()}"

    scp = _SCP_REMOTE.fullmatch(raw)
    if scp is not None and "://" not in raw:
        host, path = scp.groups()
        if host.casefold() != _GITHUB_HOST:
            raise RepositoryInspectionError()
    else:
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise RepositoryInspectionError() from None
        if parsed.scheme.casefold() not in {"https", "ssh"}:
            raise RepositoryInspectionError()
        if hostname is None or hostname.casefold() != _GITHUB_HOST:
            raise RepositoryInspectionError()
        if parsed.username is not None and parsed.scheme.casefold() == "https":
            raise RepositoryInspectionError()
        if parsed.password is not None or port is not None:
            raise RepositoryInspectionError()
        if parsed.query or parsed.fragment:
            raise RepositoryInspectionError()
        path = parsed.path

    path = path.removeprefix("/")
    path = path.removesuffix(".git")
    if not _REPOSITORY_IDENTITY.fullmatch(path):
        raise RepositoryInspectionError()
    owner, name = path.split("/", 1)
    return f"{owner.casefold()}/{name.casefold()}"


def _validate_branch(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or value.startswith("-")
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise RepositoryInspectionError()


__all__ = [
    "GitRunner",
    "LocalGitRepositoryInspector",
    "RepositoryInspectionError",
    "canonical_path_key",
]
