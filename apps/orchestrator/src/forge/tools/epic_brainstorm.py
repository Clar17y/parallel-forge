"""Minimal read-only repository capability for discovery."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from itertools import islice

from forge.application.ports.repository import (
    FileRead,
    InstructionDocument,
    RepositoryAccessDenied,
    RepositoryEntry,
    RepositoryReader,
    RepositoryRoot,
    SearchMatch,
)
from forge.domain.payload import redact_durable_text

_PRIVATE_KEY_MARKER = re.compile(
    r"(?i)-{4,5} ?(?P<action>BEGIN|END) "
    r"(?P<kind>(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?) ?-{4,5}"
)
_AMBIGUOUS_LINE_SEPARATOR = re.compile(r"[\v\f\x1c-\x1e\x85\u2028\u2029]|\r(?!\n)")


def _advance_private_key(active_kinds: list[str], marker: re.Match[str]) -> None:
    kind = marker.group("kind").upper()
    if marker.group("action").upper() == "BEGIN":
        active_kinds.append(kind)
    elif active_kinds and active_kinds[-1] == kind:
        active_kinds.pop()


def _redact_private_keys(value: str) -> str:
    pieces: list[str] = []
    start: int | None = None
    active_kinds: list[str] = []
    copied = 0
    for marker in _PRIVATE_KEY_MARKER.finditer(value):
        if not active_kinds and marker.group("action").upper() == "BEGIN":
            start = marker.start()
        _advance_private_key(active_kinds, marker)
        if start is not None and not active_kinds:
            pieces.extend((value[copied:start], "[REDACTED]"))
            copied = marker.end()
            start = None
    if start is not None:
        pieces.extend((value[copied:start], "[REDACTED]"))
    else:
        pieces.append(value[copied:])
    return "".join(pieces)


def _safe_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        normalized.startswith("/")
        or ":" in normalized
        or any(part in ("..", "") for part in parts)
        or any(part.startswith(".") and part not in (".", ".env.example") for part in parts)
        or any(part.lower() in ("secrets", "credentials", ".ssh") for part in parts)
        or any(part.lower().endswith((".pem", ".key", ".p12", ".env")) for part in parts)
    ):
        raise RepositoryAccessDenied("repository path is unavailable to brainstorming")


class BrainstormReadOnlyTools:
    def __init__(
        self,
        reader: RepositoryReader,
        *,
        authorize: Callable[[], Awaitable[None]] | None = None,
        max_items: int = 100,
        max_bytes: int = 4096,
    ) -> None:
        if max_items < 1 or max_items > 100 or max_bytes < 1 or max_bytes > 8192:
            raise ValueError("invalid read-only tool limits")
        self._reader, self._max_items, self._max_bytes = reader, max_items, max_bytes
        self._authorize = authorize

    async def _check(self) -> None:
        if self._authorize is not None:
            await self._authorize()

    def _bound_text(self, value: str, *, source_truncated: bool = False) -> tuple[str, bool]:
        # The controlled reader can already have cut a PEM block before its END marker.
        # Redact complete source first, then fail closed on an unmatched BEGIN.
        redacted = redact_durable_text(_redact_private_keys(value))
        try:
            encoded = redacted.encode("utf-8")
        except UnicodeError:
            raise RepositoryAccessDenied("repository text is invalid") from None
        return (
            encoded[: self._max_bytes].decode("utf-8", errors="ignore"),
            source_truncated or len(encoded) > self._max_bytes,
        )

    @property
    def root(self) -> RepositoryRoot:
        return self._reader.root

    async def excludes_paths(self, paths: Sequence[str]) -> bool:
        await self._check()
        return self._reader.excludes_paths(paths)

    async def list_files(self, path: str = ".") -> Sequence[RepositoryEntry]:
        _safe_path(path)
        await self._check()
        entries = self._reader.list_files(path)
        return tuple(
            islice((item for item in entries if self._allowed(item.path)), self._max_items)
        )

    async def read_file(self, path: str) -> FileRead:
        _safe_path(path)
        await self._check()
        value = self._reader.read_file(path)
        _safe_path(value.path)
        content, truncated = self._bound_text(value.content, source_truncated=value.truncated)
        return FileRead(
            path=value.path,
            content=content,
            original_byte_count=value.original_byte_count,
            truncated=truncated,
        )

    async def search(self, literal: str, path: str = ".") -> Sequence[SearchMatch]:
        _safe_path(path)
        if not literal or len(literal.encode("utf-8")) > 256:
            raise ValueError("search literal is invalid")
        await self._check()
        results: list[SearchMatch] = []
        protected_by_path: dict[str, tuple[set[int], list[str], bool]] = {}
        for item in self._reader.search(literal, path):
            if not self._allowed(item.path):
                continue
            if item.path not in protected_by_path:
                source = self._reader.read_file(item.path)
                protected: set[int] = set()
                active_kinds: list[str] = []
                lines = source.content.splitlines()
                for number, line in enumerate(lines, 1):
                    hidden = bool(active_kinds)
                    for marker in _PRIVATE_KEY_MARKER.finditer(line):
                        hidden = True
                        _advance_private_key(active_kinds, marker)
                    if hidden:
                        protected.add(number)
                if source.truncated:
                    protected.add(len(lines))
                protected_by_path[item.path] = (
                    protected,
                    lines,
                    bool(_AMBIGUOUS_LINE_SEPARATOR.search(source.content)),
                )
            protected, lines, ambiguous = protected_by_path[item.path]
            hidden = (
                ambiguous
                or item.line_number < 1
                or item.line_number > len(lines)
                or item.line_number in protected
                or item.line_text != lines[item.line_number - 1]
            )
            results.append(
                SearchMatch(
                    path=item.path,
                    line_number=item.line_number,
                    line_text=self._bound_text("[REDACTED]" if hidden else item.line_text)[0],
                )
            )
            if len(results) >= self._max_items:
                break
        return tuple(results)

    async def read_instructions(self, target_path: str = ".") -> Sequence[InstructionDocument]:
        _safe_path(target_path)
        await self._check()
        results: list[InstructionDocument] = []
        for item in self._reader.read_instructions(target_path):
            if not self._allowed(item.path):
                continue
            content, truncated = self._bound_text(item.content, source_truncated=item.truncated)
            results.append(
                InstructionDocument(
                    path=item.path,
                    content=content,
                    original_byte_count=item.original_byte_count,
                    truncated=truncated,
                )
            )
            if len(results) >= self._max_items:
                break
        return tuple(results)

    @staticmethod
    def _allowed(path: str) -> bool:
        try:
            _safe_path(path)
        except RepositoryAccessDenied:
            return False
        return True


__all__ = ["BrainstormReadOnlyTools"]
