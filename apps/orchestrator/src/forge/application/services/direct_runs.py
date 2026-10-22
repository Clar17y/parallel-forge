"""Direct-run API service: create/list/get/cancel plus bounded file reads."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.direct import (
    SUPPORTED_DIRECT_CLIENTS,
    DirectCheckRecord,
    DirectRunRecord,
    DirectSessionRecord,
)
from forge.domain.subscription_installations import load_subscription_installation_manifest
from forge.persistence.repositories.direct import PostgresDirectRepository
from forge.settings import Settings

MAX_TAIL_BYTES = 65_536
MAX_DIFF_BYTES = 65_536
MAX_LIST = 100


class DirectRunService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        installations: Iterable[object] | None = None,
    ) -> None:
        self._repo = PostgresDirectRepository(factory)
        self._settings = settings
        self._installations = tuple(installations) if installations is not None else None

    @property
    def repository(self) -> PostgresDirectRepository:
        return self._repo

    def _entries(self) -> tuple[object, ...]:
        if self._installations is not None:
            return self._installations
        manifest = load_subscription_installation_manifest(
            self._settings.subscription_installations_path
        )
        return () if manifest is None else tuple(manifest.installations)

    def routes(self) -> tuple[dict[str, str | None], ...]:
        seen: list[dict[str, str | None]] = []
        for entry in self._entries():
            client = str(getattr(entry, "client", ""))
            if client not in SUPPORTED_DIRECT_CLIENTS:
                continue
            item: dict[str, str | None] = {
                "client": client,
                "model": str(getattr(entry, "model", "")),
                "effort": getattr(entry, "effort", None),
            }
            if item not in seen:
                seen.append(item)
        return tuple(seen)

    def find_installation(self, client: str, model: str, effort: str | None) -> object:
        for entry in self._entries():
            if (
                str(getattr(entry, "client", "")) == client
                and str(getattr(entry, "model", "")) == model
                and (effort is None or str(getattr(entry, "effort", "")) == effort)
            ):
                return entry
        raise ValueError("unknown direct route")

    async def create(
        self, *, project_id: UUID, task: str, client: str, model: str, effort: str | None
    ) -> DirectRunRecord:
        if client not in SUPPORTED_DIRECT_CLIENTS:
            raise ValueError("unknown direct route")
        if not task.strip() or len(task.encode("utf-8")) > 64 * 1024:
            raise ValueError("direct task text is invalid")
        self.find_installation(client, model, effort)
        return await self._repo.create(
            project_id=project_id, task_text=task, client=client, model=model, effort=effort
        )

    async def list(self, limit: int = 50) -> tuple[DirectRunRecord, ...]:
        return await self._repo.list(min(max(limit, 1), MAX_LIST))

    async def get(self, run_id: UUID) -> DirectRunRecord:
        return await self._repo.get(run_id)

    async def sessions(self, run_id: UUID) -> tuple[DirectSessionRecord, ...]:
        return await self._repo.sessions(run_id)

    async def checks(self, run_id: UUID) -> tuple[DirectCheckRecord, ...]:
        return await self._repo.checks(run_id)

    async def cancel(self, run_id: UUID) -> DirectRunRecord:
        return await self._repo.request_cancel(run_id)

    async def transcript(
        self, run_id: UUID, session_id: UUID | None = None, tail_bytes: int = 32_768
    ) -> str:
        run = await self._repo.get(run_id)
        sessions = await self._repo.sessions(run_id)
        chosen = None
        if session_id is not None:
            chosen = next((s for s in sessions if s.id == session_id), None)
            if chosen is None:
                raise ValueError("unknown direct session")
        elif sessions:
            chosen = sessions[-1]
        if chosen is None or not chosen.transcript_path:
            # Fall back to the conventional path for runs settled before a session row.
            path = (
                Path(self._settings.data_root)
                / "direct-transcripts"
                / str(run.id)
            )
            if not path.exists():
                return ""
            files = sorted(path.glob("*.log"))
            if not files:
                return ""
            return _tail(files[-1], tail_bytes)
        return _tail(Path(chosen.transcript_path), tail_bytes)

    async def diff(self, run_id: UUID) -> str:
        run = await self._repo.get(run_id)
        if not run.worktree_path or not run.base_sha:
            return ""
        worktree = Path(run.worktree_path)
        if not worktree.exists():
            return ""
        if run.head_sha and run.head_sha != run.base_sha:
            args = ["diff", run.base_sha, run.head_sha]
        else:
            args = ["diff", run.base_sha]
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(worktree), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", errors="replace")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_DIFF_BYTES:
            text = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore") + "\n[TRUNCATED]"
        return text


def _tail(path: Path, tail_bytes: int) -> str:
    bounded = min(max(tail_bytes, 0), MAX_TAIL_BYTES)
    if bounded == 0 or not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) > bounded:
        data = data[-bounded:]
    return data.decode("utf-8", errors="replace")


__all__ = ["MAX_DIFF_BYTES", "MAX_TAIL_BYTES", "DirectRunService"]
