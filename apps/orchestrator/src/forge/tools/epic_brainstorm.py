"""Minimal read-only repository capability for discovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

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
        return tuple(item for item in entries if self._allowed(item.path))[: self._max_items]

    async def read_file(self, path: str) -> FileRead:
        _safe_path(path)
        await self._check()
        value = self._reader.read_file(path)
        _safe_path(value.path)
        encoded = value.content.encode("utf-8")
        content = redact_durable_text(encoded[: self._max_bytes].decode("utf-8", errors="ignore"))
        return FileRead(
            path=value.path,
            content=content,
            original_byte_count=value.original_byte_count,
            truncated=value.truncated or len(encoded) > self._max_bytes,
        )

    async def search(self, literal: str, path: str = ".") -> Sequence[SearchMatch]:
        _safe_path(path)
        if not literal or len(literal.encode("utf-8")) > 256:
            raise ValueError("search literal is invalid")
        await self._check()
        return tuple(
            SearchMatch(
                path=item.path,
                line_number=item.line_number,
                line_text=redact_durable_text(item.line_text[: self._max_bytes]),
            )
            for item in self._reader.search(literal, path)
            if self._allowed(item.path)
        )[: self._max_items]

    async def read_instructions(self, target_path: str = ".") -> Sequence[InstructionDocument]:
        _safe_path(target_path)
        await self._check()
        return tuple(
            InstructionDocument(
                path=item.path,
                content=redact_durable_text(item.content[: self._max_bytes]),
                original_byte_count=item.original_byte_count,
                truncated=item.truncated or len(item.content.encode("utf-8")) > self._max_bytes,
            )
            for item in self._reader.read_instructions(target_path)
            if self._allowed(item.path)
        )[: self._max_items]

    @staticmethod
    def _allowed(path: str) -> bool:
        try:
            _safe_path(path)
        except RepositoryAccessDenied:
            return False
        return True


__all__ = ["BrainstormReadOnlyTools"]
