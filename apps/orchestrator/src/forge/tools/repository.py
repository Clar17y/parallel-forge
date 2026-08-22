"""Bounded, read-only repository listing and file reading."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

from forge.application.ports.repository import (
    BinaryRepositoryFile,
    FileRead,
    InstructionDocument,
    ProcessRunner,
    RepositoryAccessDenied,
    RepositoryEncodingError,
    RepositoryEntry,
    SearchMatch,
)
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner as LocalProcessRunner

_DEFAULT_MAX_FILE_BYTES = 256 * 1024
_DEFAULT_MAX_LIST_ENTRIES = 10_000
_DEFAULT_MAX_SEARCH_MATCHES = 100
_DEFAULT_MAX_SEARCH_BYTES = 8 * 1024 * 1024
_DEFAULT_RG_STREAM_BYTES = 1024 * 1024
_RG_ARG_MAX_BYTES = 32 * 1024


class _UnconfiguredRg:
    pass


_RG_UNCONFIGURED = _UnconfiguredRg()
_BUILTIN_INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "README.md")
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
        max_search_matches: int = _DEFAULT_MAX_SEARCH_MATCHES,
        max_search_bytes: int = _DEFAULT_MAX_SEARCH_BYTES,
        rg_executable: str | os.PathLike[str] | None | _UnconfiguredRg = _RG_UNCONFIGURED,
        process_runner: ProcessRunner | None = None,
        force_python_search: bool = False,
        instruction_names: Sequence[str] = (),
    ) -> None:
        self._root = root if isinstance(root, CanonicalRoot) else CanonicalRoot(root)
        self._secret_paths = _normalize_exclusions(self._root, secret_paths)
        self._managed_worktree_paths = _normalize_exclusions(self._root, managed_worktree_paths)
        self._artifact_paths = _normalize_exclusions(self._root, artifact_paths)
        self._max_file_bytes = _positive_bound(max_file_bytes, "max_file_bytes")
        self._max_list_entries = _positive_bound(max_list_entries, "max_list_entries")
        self._max_search_matches = _positive_bound(max_search_matches, "max_search_matches")
        self._max_search_bytes = _positive_bound(max_search_bytes, "max_search_bytes")
        if type(force_python_search) is not bool:
            raise TypeError("force_python_search must be a boolean")
        if rg_executable is _RG_UNCONFIGURED:
            self._rg_executable = None
            explicit_fallback = False
        elif rg_executable is None:
            self._rg_executable = None
            explicit_fallback = True
        else:
            self._rg_executable = _validate_rg_executable(
                cast(str | os.PathLike[str], rg_executable)
            )
            explicit_fallback = False
        self._force_python_search = force_python_search or explicit_fallback
        self._process_runner = process_runner
        self._instruction_names = _validate_instruction_names(instruction_names)

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

    def search(self, literal: str, path: str | os.PathLike[str] = ".") -> tuple[SearchMatch, ...]:
        """Search bounded repository text with literal, deterministic semantics."""

        _validate_search_literal(literal)
        normalized = self._root.normalize(path, allow_root=True)
        self._ensure_allowed(normalized, direct=True)
        entries = self._bounded_search_entries(normalized)
        if not entries:
            return ()
        executable = self._select_rg_executable()
        if executable is not None:
            argv = _build_rg_argv(
                executable,
                literal,
                entries,
                self._max_search_matches,
                self._rg_glob_exclusions(),
            )
            if argv is not None:
                return self._search_with_rg(argv, literal, normalized, entries)
        return self._search_with_python(literal, entries)

    def read_instructions(
        self, target_path: str | os.PathLike[str] = "."
    ) -> tuple[InstructionDocument, ...]:
        """Return untrusted instructions from the root and deepest applicable ancestor."""

        normalized = self._root.normalize(target_path, allow_root=True)
        self._ensure_allowed(normalized, direct=True)
        target_directory = self._instruction_target_directory(normalized)
        chain = _directory_chain(target_directory)
        root_documents = self._instruction_documents_in_directory(".")
        deepest_documents: tuple[InstructionDocument, ...] = ()
        for directory in chain[1:]:
            documents = self._instruction_documents_in_directory(directory)
            if documents:
                deepest_documents = documents
        return root_documents + deepest_documents

    def _instruction_target_directory(self, normalized: str) -> str:
        if normalized == ".":
            return "."
        try:
            children = self._root.list_directory(normalized)
        except RepositoryAccessDenied:
            children = None
        if children is not None:
            if _contains_pyvenv_cfg(children):
                raise RepositoryAccessDenied("instruction target is excluded")
            return normalized
        try:
            metadata = self._root.stat_file(normalized)
        except RepositoryAccessDenied:
            raise RepositoryAccessDenied("instruction target is unavailable") from None
        if stat.S_ISREG(metadata.st_mode):
            parts = normalized.split("/")
            return "." if len(parts) == 1 else "/".join(parts[:-1])
        raise RepositoryAccessDenied("instruction target is not a regular file or directory")

    def _instruction_documents_in_directory(
        self, directory: str
    ) -> tuple[InstructionDocument, ...]:
        children = self._root.list_directory(directory)
        by_name = {_instruction_name_key(name): (name, metadata) for name, metadata in children}
        documents: list[InstructionDocument] = []
        for configured_name in self._instruction_names:
            item = by_name.get(_instruction_name_key(configured_name))
            if item is None:
                continue
            actual_name, metadata = item
            if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                continue
            path = actual_name if directory == "." else f"{directory}/{actual_name}"
            if self._is_excluded(path) or self._has_virtual_environment_ancestor(path):
                continue
            result = self.read_file(path)
            documents.append(
                InstructionDocument(
                    path=result.path,
                    content=result.content,
                    original_byte_count=result.original_byte_count,
                    truncated=result.truncated,
                )
            )
        return tuple(documents)

    def _bounded_search_entries(self, normalized: str) -> tuple[RepositoryEntry, ...]:
        entries = tuple(
            entry
            for entry in self._search_entries(normalized)
            if not _is_hidden_search_path(entry.path)
        )
        inspected_bytes = 0
        for entry in entries:
            candidate_bytes = min(entry.byte_count, self._max_file_bytes)
            if inspected_bytes + candidate_bytes > self._max_search_bytes:
                raise RepositoryAccessDenied("repository search exceeded its byte bound")
            inspected_bytes += candidate_bytes
        return entries

    def _search_with_python(
        self, literal: str, entries: Sequence[RepositoryEntry]
    ) -> tuple[SearchMatch, ...]:
        matches: list[SearchMatch] = []
        for entry in entries:
            try:
                result = self.read_file(entry.path)
            except BinaryRepositoryFile, RepositoryEncodingError:
                continue
            for line_number, line_text in enumerate(result.content.splitlines(), start=1):
                if literal not in line_text:
                    continue
                matches.append(
                    SearchMatch(
                        path=result.path,
                        line_number=line_number,
                        line_text=line_text,
                    )
                )
                if len(matches) >= self._max_search_matches:
                    return tuple(matches)

        return tuple(matches)

    def _select_rg_executable(self) -> str | None:
        if self._force_python_search:
            return None
        if self._rg_executable is not None:
            return self._rg_executable
        discovered = shutil.which("rg")
        if not discovered:
            return None
        candidate = Path(discovered)
        if not candidate.is_absolute():
            return None
        try:
            if not candidate.is_file():
                return None
        except OSError, ValueError:
            return None
        return str(candidate)

    def _search_with_rg(
        self,
        argv: tuple[str, ...],
        literal: str,
        normalized: str,
        entries: Sequence[RepositoryEntry],
    ) -> tuple[SearchMatch, ...]:
        runner = self._process_runner
        if runner is None:
            runner = LocalProcessRunner(
                self._root,
                stdout_max_bytes=_DEFAULT_RG_STREAM_BYTES,
                stderr_max_bytes=_DEFAULT_RG_STREAM_BYTES,
            )
        result = runner.run_argv(
            argv,
            cwd=".",
            environment={"LC_ALL": "C", "LANG": "C"},
        )
        if (
            result.timed_out
            or result.stdout_truncated
            or result.stderr_truncated
            or result.stdout_original_byte_count > _DEFAULT_RG_STREAM_BYTES
            or result.stderr_original_byte_count > _DEFAULT_RG_STREAM_BYTES
        ):
            raise RepositoryAccessDenied("repository search failed")
        if result.return_code == 1:
            if result.stdout.strip():
                raise RepositoryAccessDenied("repository search failed")
            return ()
        if result.return_code != 0:
            raise RepositoryAccessDenied("repository search failed")
        if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
            raise RepositoryAccessDenied("repository search output is invalid")
        if not result.stdout.strip():
            raise RepositoryAccessDenied("repository search output is invalid")
        return self._parse_rg_output(result.stdout, literal, normalized, entries)[
            : self._max_search_matches
        ]

    def _parse_rg_output(
        self,
        output: str,
        literal: str,
        normalized_target: str,
        entries: Sequence[RepositoryEntry],
    ) -> tuple[SearchMatch, ...]:
        if "\ufffd" in output:
            raise RepositoryAccessDenied("repository search output is invalid")
        allowed = {_path_key(entry.path) for entry in entries}
        records: dict[tuple[str, int], SearchMatch] = {}
        for line in output.splitlines():
            if not line.strip():
                raise RepositoryAccessDenied("repository search output is invalid")
            try:
                record = json.loads(line)
            except TypeError, ValueError:
                raise RepositoryAccessDenied("repository search output is invalid") from None
            if not isinstance(record, dict):
                raise RepositoryAccessDenied("repository search output is invalid")
            record_type = record.get("type")
            if record_type in {"begin", "end", "summary"}:
                continue
            if record_type != "match":
                raise RepositoryAccessDenied("repository search output is invalid")
            data = record.get("data")
            if not isinstance(data, dict):
                raise RepositoryAccessDenied("repository search output is invalid")
            path = _rg_text_field(data.get("path"))
            line_text = _rg_text_field(data.get("lines"))
            line_number = data.get("line_number")
            if type(line_number) is not int or line_number <= 0:
                raise RepositoryAccessDenied("repository search output is invalid")
            line_text = line_text.rstrip("\r\n")
            if "\r" in line_text or "\n" in line_text or literal not in line_text:
                raise RepositoryAccessDenied("repository search output is invalid")
            try:
                normalized = self._root.normalize(path)
            except RepositoryAccessDenied:
                raise RepositoryAccessDenied("repository search returned an invalid path") from None
            if (
                _path_key(normalized) not in allowed
                or (
                    normalized_target != "."
                    and not self._root.matches(normalized, normalized_target)
                )
                or _is_hidden_search_path(normalized)
                or self._is_excluded(normalized)
                or self._has_virtual_environment_ancestor(normalized)
            ):
                raise RepositoryAccessDenied("repository search returned a forbidden path")
            try:
                metadata = self._root.stat_file(normalized)
            except RepositoryAccessDenied:
                raise RepositoryAccessDenied(
                    "repository search returned an unavailable path"
                ) from None
            if not stat.S_ISREG(metadata.st_mode):
                raise RepositoryAccessDenied("repository search returned a non-file path")
            match = SearchMatch(path=normalized, line_number=line_number, line_text=line_text)
            key = (_path_key(normalized), line_number)
            prior = records.get(key)
            if prior is not None and prior.line_text != line_text:
                raise RepositoryAccessDenied("repository search output is invalid")
            records[key] = match
        return tuple(
            sorted(
                records.values(),
                key=lambda match: (_entry_sort_key(match.path), match.line_number),
            )
        )

    def _rg_glob_exclusions(self) -> tuple[str, ...]:
        patterns: set[str] = set()
        fixed = sorted(_FIXED_DIRECTORY_EXCLUSIONS, key=_entry_sort_key)
        for component in fixed:
            escaped = _escape_rg_glob(component)
            for prefix in (escaped, f"**/{escaped}"):
                patterns.add(f"!{prefix}")
                patterns.add(f"!{prefix}/**")
        configured = (*self._secret_paths, *self._managed_worktree_paths, *self._artifact_paths)
        for path in configured:
            escaped = _escape_rg_glob(path)
            patterns.add(f"!{escaped}")
            patterns.add(f"!{escaped}/**")
        return tuple(sorted(patterns))

    def _search_entries(self, normalized: str) -> tuple[RepositoryEntry, ...]:
        if normalized == ".":
            return self.list_files()
        try:
            metadata = self._root.stat_file(normalized)
        except RepositoryAccessDenied:
            return self.list_files(normalized)
        if stat.S_ISREG(metadata.st_mode):
            return (
                RepositoryEntry(
                    path=normalized,
                    kind="file",
                    byte_count=int(metadata.st_size),
                ),
            )
        return self.list_files(normalized)

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


def _validate_rg_executable(value: str | os.PathLike[str] | None) -> str | None:
    if value is None:
        return None
    try:
        raw = os.fspath(value)
    except TypeError, ValueError:
        raise RepositoryAccessDenied("rg executable configuration is invalid") from None
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise RepositoryAccessDenied("rg executable configuration is invalid")
    try:
        candidate = Path(raw)
    except TypeError, ValueError:
        raise RepositoryAccessDenied("rg executable configuration is invalid") from None
    if not candidate.is_absolute():
        raise RepositoryAccessDenied("rg executable configuration is invalid")
    return raw


def _validate_instruction_names(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("instruction names must be a sequence of basenames")
    try:
        configured = tuple(values)
    except TypeError, ValueError:
        raise TypeError("instruction names must be a sequence of basenames") from None
    names = list(_BUILTIN_INSTRUCTION_NAMES)
    seen = {_instruction_name_key(name) for name in names}
    for value in configured:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or "/" in value
            or "\\" in value
            or value in {".", ".."}
        ):
            raise RepositoryAccessDenied("instruction name configuration is invalid")
        try:
            if Path(value).is_absolute():
                raise RepositoryAccessDenied("instruction name configuration is invalid")
        except TypeError, ValueError:
            raise RepositoryAccessDenied("instruction name configuration is invalid") from None
        key = _instruction_name_key(value)
        if key in seen:
            raise RepositoryAccessDenied("instruction name configuration is invalid")
        seen.add(key)
        names.append(value)
    return tuple(names)


def _instruction_name_key(name: str) -> str:
    return name.casefold() if os.name == "nt" else name


def _directory_chain(directory: str) -> tuple[str, ...]:
    if directory == ".":
        return (".",)
    parts = directory.split("/")
    return (".",) + tuple("/".join(parts[:index]) for index in range(1, len(parts) + 1))


def _build_rg_argv(
    executable: str,
    literal: str,
    entries: Sequence[RepositoryEntry],
    max_matches: int,
    exclusions: Sequence[str],
) -> tuple[str, ...] | None:
    argv: list[str] = [
        executable,
        "--fixed-strings",
        "--json",
        "--no-hidden",
        "--no-ignore",
        "--sort",
        "path",
        "--max-count",
        str(max_matches),
    ]
    for exclusion in exclusions:
        argv.extend(("--glob", exclusion))
    argv.extend(("--", literal))
    argv.extend(entry.path for entry in entries)
    encoded_size = sum(len(argument.encode("utf-8")) + 1 for argument in argv)
    if encoded_size > _RG_ARG_MAX_BYTES:
        return None
    return tuple(argv)


def _escape_rg_glob(value: str) -> str:
    special = frozenset("\\*?[]{}")
    return "".join(f"\\{character}" if character in special else character for character in value)


def _rg_text_field(value: object) -> str:
    if not isinstance(value, dict) or set(value) != {"text"}:
        raise RepositoryAccessDenied("repository search output is invalid")
    text = value.get("text")
    if not isinstance(text, str) or not text:
        raise RepositoryAccessDenied("repository search output is invalid")
    return text


def _path_key(path: str) -> str:
    return path.casefold() if os.name == "nt" else path


def _validate_search_literal(literal: str) -> None:
    if not isinstance(literal, str) or not literal or "\x00" in literal:
        raise ValueError("search literal must be nonempty UTF-8 text")
    try:
        literal.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("search literal must be nonempty UTF-8 text") from None


def _entry_sort_key(path: str) -> tuple[str, str]:
    return (path.casefold() if os.name == "nt" else path, path)


def _is_hidden_search_path(path: str) -> bool:
    return any(part.startswith(".") and part != ".env.example" for part in path.split("/"))


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
    "SearchMatch",
]
