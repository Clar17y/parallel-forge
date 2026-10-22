"""PostgreSQL repository for direct runs and sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.direct import (
    DirectRunNotFound,
    DirectRunRecord,
    DirectSessionRecord,
)
from forge.persistence.models import DirectRun, DirectSession, Project


def _run_record(row: DirectRun) -> DirectRunRecord:
    options = row.options if isinstance(row.options, list) else []
    return DirectRunRecord(
        id=row.id,
        project_id=row.project_id,
        task_text=row.task_text,
        client=row.client,
        model=row.model,
        effort=row.effort,
        state=row.state,
        branch=row.branch,
        worktree_path=row.worktree_path,
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        summary=row.summary,
        question=row.question,
        options=tuple(str(item) for item in options),
        failure_reason=row.failure_reason,
        result_degraded=bool(row.result_degraded),
        cancel_requested=bool(row.cancel_requested),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _session_record(row: DirectSession) -> DirectSessionRecord:
    return DirectSessionRecord(
        id=row.id,
        run_id=row.run_id,
        state=row.state,
        client=row.client,
        model=row.model,
        effort=row.effort,
        pid=row.pid,
        transcript_path=row.transcript_path,
        last_output_at=row.last_output_at,
        output_bytes=row.output_bytes,
        exit_code=row.exit_code,
        started_at=row.started_at,
        ended_at=row.ended_at,
        failure_reason=row.failure_reason,
    )


class PostgresDirectRepository:
    """Each method owns one short transaction via the session factory."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def create(
        self, *, project_id: UUID, task_text: str, client: str, model: str, effort: str | None
    ) -> DirectRunRecord:
        async with self._factory() as session:
            project = await session.get(Project, project_id)
            if project is None:
                from forge.persistence.repositories.projects import ProjectNotFound

                raise ProjectNotFound("project was not found")
            row = DirectRun(
                id=uuid4(),
                project_id=project_id,
                task_text=task_text,
                client=client,
                model=model,
                effort=effort,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def get(self, run_id: UUID) -> DirectRunRecord:
        async with self._factory() as session:
            row = await session.get(DirectRun, run_id)
            if row is None:
                raise DirectRunNotFound("direct run was not found")
            return _run_record(row)

    async def project_for(self, run_id: UUID) -> tuple[str, str]:
        """Return (canonical_path, default_branch) for a run's project."""

        async with self._factory() as session:
            row = await session.get(DirectRun, run_id)
            if row is None:
                raise DirectRunNotFound("direct run was not found")
            project = await session.get(Project, row.project_id)
            if project is None:
                raise DirectRunNotFound("direct run project was not found")
            return project.canonical_path, project.default_branch

    async def list(self, limit: int = 50) -> tuple[DirectRunRecord, ...]:
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(DirectRun).order_by(DirectRun.created_at.desc()).limit(limit)
                )
            ).scalars().all()
            return tuple(_run_record(row) for row in rows)

    async def sessions(self, run_id: UUID) -> tuple[DirectSessionRecord, ...]:
        async with self._factory() as session:
            if await session.get(DirectRun, run_id) is None:
                raise DirectRunNotFound("direct run was not found")
            rows = (
                await session.execute(
                    select(DirectSession)
                    .where(DirectSession.run_id == run_id)
                    .order_by(DirectSession.started_at)
                )
            ).scalars().all()
            return tuple(_session_record(row) for row in rows)

    async def claim_next(self) -> DirectRunRecord | None:
        async with self._factory() as session:
            row = (
                await session.execute(
                    select(DirectRun)
                    .where(DirectRun.state == "QUEUED")
                    .order_by(DirectRun.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.cancel_requested:
                row.state = "CANCELLED"
                await session.commit()
                return None
            row.state = "RUNNING"
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def running_runs(self) -> tuple[DirectRunRecord, ...]:
        async with self._factory() as session:
            rows = (
                await session.execute(select(DirectRun).where(DirectRun.state == "RUNNING"))
            ).scalars().all()
            return tuple(_run_record(row) for row in rows)

    async def update_run(self, run_id: UUID, **fields: object) -> DirectRunRecord:
        async with self._factory() as session:
            row = await session.get(DirectRun, run_id)
            if row is None:
                raise DirectRunNotFound("direct run was not found")
            for key, value in fields.items():
                if key == "options" and isinstance(value, tuple):
                    value = list(value)
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def request_cancel(self, run_id: UUID) -> DirectRunRecord:
        async with self._factory() as session:
            row = await session.get(DirectRun, run_id)
            if row is None:
                raise DirectRunNotFound("direct run was not found")
            if row.state == "QUEUED":
                row.state = "CANCELLED"
                row.cancel_requested = True
            elif row.state in {"RUNNING", "FINALIZING"}:
                row.cancel_requested = True
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def cancel_requested(self, run_id: UUID) -> bool:
        async with self._factory() as session:
            row = await session.get(DirectRun, run_id)
            return bool(row and row.cancel_requested)

    async def create_session(
        self, *, run_id: UUID, run: DirectRunRecord | None = None
    ) -> DirectSessionRecord:
        async with self._factory() as session:
            source = run or _run_record(await self._require(session, run_id))
            row = DirectSession(
                id=uuid4(),
                run_id=run_id,
                state="RUNNING",
                client=source.client,
                model=source.model,
                effort=source.effort,
                started_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _session_record(row)

    async def latest_session(self, run_id: UUID) -> DirectSessionRecord | None:
        sessions = await self.sessions(run_id)
        return sessions[-1] if sessions else None

    async def update_session(self, session_id: UUID, **fields: object) -> None:
        async with self._factory() as session:
            await session.execute(
                update(DirectSession).where(DirectSession.id == session_id).values(**fields)
            )
            await session.commit()

    async def _require(self, session: AsyncSession, run_id: UUID) -> DirectRun:
        row = await session.get(DirectRun, run_id)
        if row is None:
            raise DirectRunNotFound("direct run was not found")
        return row


__all__ = ["PostgresDirectRepository"]
