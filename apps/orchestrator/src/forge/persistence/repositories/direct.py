"""PostgreSQL repository for direct runs and sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.direct import (
    DirectCheckRecord,
    DirectReviewRecord,
    DirectRunNotFound,
    DirectRunRecord,
    DirectSessionRecord,
)
from forge.domain.policy import ProjectPolicy
from forge.persistence.models import DirectCheck, DirectReview, DirectRun, DirectSession, Project


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
        pr_number=row.pr_number,
        pr_url=row.pr_url,
        pr_rounds=int(row.pr_rounds or 0),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _check_record(row: DirectCheck) -> DirectCheckRecord:
    return DirectCheckRecord(
        id=row.id,
        run_id=row.run_id,
        round=row.round,
        command_name=row.command_name,
        kind=row.kind,
        tree_sha=row.tree_sha,
        command_digest=row.command_digest,
        policy_version=row.policy_version,
        status=row.status,
        exit_code=row.exit_code,
        duration_ms=row.duration_ms,
        failure_excerpt=row.failure_excerpt,
        created_at=row.created_at,
    )


def _review_record(row: DirectReview) -> DirectReviewRecord:
    raw = row.findings if isinstance(row.findings, list) else []
    findings = tuple(
        {str(k): v for k, v in item.items()} for item in raw if isinstance(item, dict)
    )
    return DirectReviewRecord(
        id=row.id, run_id=row.run_id, session_id=row.session_id, round=row.round,
        client=row.client, model=row.model, verdict=row.verdict, findings=findings,
        summary=row.summary, degraded=bool(row.degraded),
        reviewer_modified_worktree=bool(row.reviewer_modified_worktree),
        created_at=row.created_at,
    )


def _session_record(row: DirectSession) -> DirectSessionRecord:
    return DirectSessionRecord(
        id=row.id,
        run_id=row.run_id,
        state=row.state,
        kind=row.kind or "implement",
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

    async def publication_for(self, run_id: UUID) -> tuple[str, str]:
        """Return (github_repository, default_branch) for a run's project."""

        async with self._factory() as session:
            row = await session.get(DirectRun, run_id)
            if row is None:
                raise DirectRunNotFound("direct run was not found")
            project = await session.get(Project, row.project_id)
            if project is None:
                raise DirectRunNotFound("direct run project was not found")
            return project.github_repository or "", project.default_branch

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
                await session.execute(
                    select(DirectRun).where(
                        DirectRun.state.in_(
                            (
                                "RUNNING", "FINALIZING", "CHECKING", "REVIEWING",
                                "REPAIRING", "PUBLISHING", "PR_OPEN",
                            )
                        )
                    )
                )
            ).scalars().all()
            return tuple(_run_record(row) for row in rows)

    async def policy_for(self, run_id: UUID) -> tuple[ProjectPolicy, int] | None:
        """Return (policy, version) for the run's project, or None if unset."""

        from forge.persistence.repositories.projects import PostgresProjectRepository

        async with self._factory() as session:
            row = await session.get(DirectRun, run_id)
            if row is None:
                raise DirectRunNotFound("direct run was not found")
            project = await session.get(Project, row.project_id)
            if project is None or project.current_policy_version is None:
                return None
            record = await PostgresProjectRepository(session).get_policy(
                row.project_id, project.current_policy_version
            )
            return ProjectPolicy.model_validate(record.document), record.version

    async def checks(self, run_id: UUID) -> tuple[DirectCheckRecord, ...]:
        async with self._factory() as session:
            if await session.get(DirectRun, run_id) is None:
                raise DirectRunNotFound("direct run was not found")
            rows = (
                await session.execute(
                    select(DirectCheck)
                    .where(DirectCheck.run_id == run_id)
                    .order_by(DirectCheck.round, DirectCheck.created_at)
                )
            ).scalars().all()
            return tuple(_check_record(row) for row in rows)

    async def create_check(
        self,
        *,
        run_id: UUID,
        round: int,
        command_name: str,
        kind: str,
        tree_sha: str,
        command_digest: str,
        policy_version: int,
        status: str,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        failure_excerpt: str | None = None,
    ) -> DirectCheckRecord:
        async with self._factory() as session:
            row = DirectCheck(
                id=uuid4(),
                run_id=run_id,
                round=round,
                command_name=command_name,
                kind=kind,
                tree_sha=tree_sha,
                command_digest=command_digest,
                policy_version=policy_version,
                status=status,
                exit_code=exit_code,
                duration_ms=duration_ms,
                failure_excerpt=failure_excerpt,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _check_record(row)

    async def cached_check(
        self, *, run_id: UUID, tree_sha: str, command_digest: str, policy_version: int
    ) -> DirectCheckRecord | None:
        async with self._factory() as session:
            row = (
                await session.execute(
                    select(DirectCheck)
                    .where(
                        DirectCheck.run_id == run_id,
                        DirectCheck.tree_sha == tree_sha,
                        DirectCheck.command_digest == command_digest,
                        DirectCheck.policy_version == policy_version,
                        DirectCheck.status != "CACHED",
                    )
                    .order_by(DirectCheck.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return None if row is None else _check_record(row)

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
            elif row.state in {
                "RUNNING", "FINALIZING", "CHECKING", "REVIEWING", "REPAIRING",
                "PUBLISHING", "PR_OPEN",
            }:
                row.cancel_requested = True
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def cancel_requested(self, run_id: UUID) -> bool:
        async with self._factory() as session:
            value = (
                await session.execute(
                    select(DirectRun.cancel_requested).where(DirectRun.id == run_id)
                )
            ).scalar_one_or_none()
            return bool(value)

    async def reviews(self, run_id: UUID) -> tuple[DirectReviewRecord, ...]:
        async with self._factory() as session:
            if await session.get(DirectRun, run_id) is None:
                raise DirectRunNotFound("direct run was not found")
            rows = (
                await session.execute(
                    select(DirectReview)
                    .where(DirectReview.run_id == run_id)
                    .order_by(DirectReview.round, DirectReview.created_at)
                )
            ).scalars().all()
            return tuple(_review_record(row) for row in rows)

    async def create_review(
        self, *, run_id: UUID, session_id: UUID, round: int, client: str, model: str,
        verdict: str, findings: tuple[dict[str, object], ...], summary: str | None,
        degraded: bool, reviewer_modified_worktree: bool,
    ) -> DirectReviewRecord:
        async with self._factory() as session:
            row = DirectReview(
                id=uuid4(), run_id=run_id, session_id=session_id, round=round,
                client=client, model=model, verdict=verdict, findings=list(findings),
                summary=summary, degraded=degraded,
                reviewer_modified_worktree=reviewer_modified_worktree,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _review_record(row)

    async def create_session(
        self, *, run_id: UUID, run: DirectRunRecord | None = None, kind: str = "implement",
        client: str | None = None, model: str | None = None, effort: str | None = None,
    ) -> DirectSessionRecord:
        async with self._factory() as session:
            source = run or _run_record(await self._require(session, run_id))
            row = DirectSession(
                id=uuid4(),
                run_id=run_id,
                kind=kind,
                state="RUNNING",
                client=client if client is not None else source.client,
                model=model if model is not None else source.model,
                effort=effort if effort is not None else source.effort,
                started_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _session_record(row)

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
