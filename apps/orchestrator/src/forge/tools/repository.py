"""Bounded, read-only repository listing and file reading."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Sequence

from forge.application.ports.repository import (
    BinaryRepositoryFile,
    FileRead,
    RepositoryAccessDenied,
    RepositoryEncodingError,
    RepositoryEntry,
)
from forge.tools.paths import CanonicalRoot

_DEFAULT_MAX_FILE_BYTES = 256 * 1024
_DEFAULT_MAX_LIST_ENTRIES = 10_000
_FIXED_DIRECTORY_EXCLUSIONS = frozenset(
    {
        ".git",
        ".worktrees",
        ".forge-worktrees",
        "node_modules",
        ".venv",
        "venv",
        "env",
    }
)
_REPARSE_POINT = 0x00000400


class RepositoryReader:
    """Read regular repository files below one canonical root."""

    def __init__(
        self,
        root: CanonicalRoot | str | os.PathLike[str],
        *,
        secret_paths: Sequence[str] = (),
        managed_worktree_paths: Sequence[str] = (),
        artifact_paths: Sequence[str] = (),
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        max_list_entries: int = _DEFAULT_MAX_LIST_ENTRIES,
    ) -> None:
        self._root = root if isinstance(root, CanonicalRoot) else CanonicalRoot(root)
        self._secret_paths = _normalize_exclusions(self._root, secret_paths)
        self._managed_worktree_paths = _normalize_exclusions(self._root, managed_worktree_paths)
        self._artifact_paths = _normalize_exclusions(self._root, artifact_paths)
        self._max_file_bytes = _positive_bound(max_file_bytes, "max_file_bytes")
        self._max_list_entries = _positive_bound(max_list_entries, "max_list_entries")

    @property
    def root(self) -> CanonicalRoot:
        """Return the pinned canonical root used by this reader."""

        return self._root

    def list_files(self, path: str | os.PathLike[str] = ".") -> tuple[RepositoryEntry, ...]:
        """Return deterministic regular-file entries below a contained directory."""

        normalized = self._root.normalize(path, allow_root=True)
        self._ensure_allowed(normalized, direct=True)
        pending = [normalized]
        entries: list[RepositoryEntry] = []
        visited_entries = 0

        while pending:
            current = pending.pop()
            children = self._root.list_directory(current)
            if current != "." and _contains_pyvenv_cfg(children):
                continue
            for name, metadata in sorted(children, key=lambda item: _entry_sort_key(item[0])):
                visited_entries += 1
                if visited_entries > self._max_list_entries:
                    raise RepositoryAccessDenied("repository listing exceeded its bound")
                child = name if current == "." else f"{current}/{name}"
                if _is_link_or_reparse(metadata):
                    continue
                if self._is_excluded(child):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(child)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                entries.append(
                    RepositoryEntry(
                        path=child,
                        kind="file",
                        byte_count=int(metadata.st_size),
                    )
                )

        return tuple(sorted(entries, key=lambda entry: _entry_sort_key(entry.path)))

    def read_file(self, path: str | os.PathLike[str]) -> FileRead:
        """Read a bounded regular UTF-8 file without normalizing its text."""

        normalized = self._root.normalize(path)
        self._ensure_allowed(normalized, direct=True)
        with self._root.open_read(normalized) as stream:
            try:
                before = os.fstat(stream.fileno())
                data = stream.read(self._max_file_bytes + 1)
                after = os.fstat(stream.fileno())
            except OSError, ValueError:
                raise RepositoryAccessDenied("repository file could not be read") from None
        if int(before.st_size) != int(after.st_size):
            raise RepositoryAccessDenied("repository file changed during read")

        original_byte_count = int(before.st_size)
        truncated = original_byte_count > self._max_file_bytes
        bounded = data[: self._max_file_bytes]
        if b"\x00" in bounded:
            raise BinaryRepositoryFile()
        content = _decode_bounded_utf8(bounded, truncated=truncated)
        return FileRead(
            path=normalized,
            content=content,
            original_byte_count=original_byte_count,
            truncated=truncated,
        )

    def _ensure_allowed(self, normalized: str, *, direct: bool) -> None:
        if normalized == ".":
            return
        if any(self._root.matches(normalized, secret) for secret in self._secret_paths):
            raise RepositoryAccessDenied("secret-designated path is not readable")
        if self._is_excluded(normalized) or self._has_virtual_environment_ancestor(normalized):
            if direct:
                raise RepositoryAccessDenied("repository path is excluded")
            return

    def _is_excluded(self, normalized: str) -> bool:
        parts = normalized.split("/")
        fixed = (
            {part.casefold() for part in _FIXED_DIRECTORY_EXCLUSIONS}
            if os.name == "nt"
            else _FIXED_DIRECTORY_EXCLUSIONS
        )
        if any((part.casefold() if os.name == "nt" else part) in fixed for part in parts):
            return True
        return any(
            self._root.matches(normalized, configured)
            for configured in (*self._managed_worktree_paths, *self._artifact_paths)
        ) or any(self._root.matches(normalized, secret) for secret in self._secret_paths)

    def _has_virtual_environment_ancestor(self, normalized: str) -> bool:
        parts = normalized.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if _contains_pyvenv_cfg(self._root.list_directory(parent)):
                return True
        return False


def _normalize_exclusions(root: CanonicalRoot, values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        normalized.append(root.normalize(value))
    return tuple(normalized)


def _positive_bound(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _entry_sort_key(path: str) -> tuple[str, str]:
    return (path.casefold() if os.name == "nt" else path, path)


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _contains_pyvenv_cfg(children: Sequence[tuple[str, os.stat_result]]) -> bool:
    return any(
        (name.casefold() if os.name == "nt" else name) == "pyvenv.cfg"
        and not _is_link_or_reparse(metadata)
        and stat.S_ISREG(metadata.st_mode)
        for name, metadata in children
    )


def _decode_bounded_utf8(data: bytes, *, truncated: bool) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        if truncated and error.reason == "unexpected end of data" and error.end == len(data):
            try:
                return data[: error.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
        raise RepositoryEncodingError() from None


__all__ = [
    "BinaryRepositoryFile",
    "FileRead",
    "RepositoryAccessDenied",
    "RepositoryEncodingError",
    "RepositoryEntry",
    "RepositoryReader",
]
