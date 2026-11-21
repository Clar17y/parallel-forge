"""PostgreSQL repository for direct runs and sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.direct import (
    ACTIVE_DIRECT_RUN_STATES,
    ACTIVE_DIRECT_TASK_STATES,
    DirectCheckRecord,
    DirectChildTaskRecord,
    DirectDecomposeResult,
    DirectReviewRecord,
    DirectRunNotFound,
    DirectRunRecord,
    DirectSessionRecord,
)
from forge.domain.policy import ProjectPolicy
from forge.persistence.models import (
    DirectCheck,
    DirectReview,
    DirectRun,
    DirectSession,
    DirectTask,
    Project,
)


def _orphan_warning(pids: Sequence[int | None], *, cancelled: bool) -> str:
    if cancelled:
        action = "The owner cancelled recovery. Verify remaining providers manually before reusing the worktree."
    else:
        action = "Recovery will retry. Cancel ends retries and retains the worktree for manual cleanup."
    ids = ", ".join(str(pid) for pid in sorted({pid for pid in pids if pid is not None}))
    return f"Provider stop unconfirmed. {action} Recorded PIDs {ids or 'unavailable'}."[:512]


def _task_record(row: DirectTask) -> DirectChildTaskRecord:
    return DirectChildTaskRecord(
        id=row.id,
        run_id=row.run_id,
        ordinal=row.ordinal,
        task_text=row.task_text,
        shape=row.shape or "quick",
        state=row.state or "PENDING",
        plan_text=row.plan_text,
        head_sha=row.head_sha,
        summary=row.summary,
        created_at=row.created_at,
        updated_at=row.updated_at,
        plan_approved=bool(row.plan_approved),
    )


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
        shape=row.shape or "quick",
        review_plan_first=bool(row.review_plan_first),
        plan_text=row.plan_text,
        plan_approved=bool(row.plan_approved),
        control_version=row.control_version,
        approval_task_id=row.approval_task_id,
        owner_actions=tuple(row.owner_actions or []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _check_record(row: DirectCheck) -> DirectCheckRecord:
    return DirectCheckRecord(
        task_id=row.task_id,
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
        task_id=row.task_id,
        id=row.id, run_id=row.run_id, session_id=row.session_id, round=row.round,
        client=row.client, model=row.model, verdict=row.verdict, findings=findings,
        summary=row.summary, degraded=bool(row.degraded),
        reviewer_modified_worktree=bool(row.reviewer_modified_worktree),
        created_at=row.created_at,
    )


def _session_record(row: DirectSession) -> DirectSessionRecord:
    return DirectSessionRecord(
        task_id=row.task_id,
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
        self, *, project_id: UUID, task_text: str, client: str, model: str,
        effort: str | None, shape: str = "quick", review_plan_first: bool = False,
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
                shape=shape,
                review_plan_first=review_plan_first,
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
                        DirectRun.state.in_(ACTIVE_DIRECT_RUN_STATES)
                    )
                )
            ).scalars().all()
            return tuple(_run_record(row) for row in rows)

    async def _locked_run(self, session: AsyncSession, run_id: UUID) -> DirectRun:
        row = (await session.execute(
            select(DirectRun).where(DirectRun.id == run_id).with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise DirectRunNotFound("direct run was not found")
        return row

    @staticmethod
    def _owner_action(
        row: DirectRun, action: str, actor_id: UUID | None, expected_version: int | None,
        *, task_id: UUID | None = None, warnings: Sequence[str] = (),
    ) -> None:
        if row.shape == "epic" and expected_version is None:
            raise ValueError("a current snapshot version is required")
        if expected_version is not None and expected_version != row.control_version:
            raise ValueError("stale direct run snapshot; refresh before retrying")
        event: dict[str, object] = {
            "actor_id": str(actor_id) if actor_id else None, "action": action,
            "snapshot_version": row.control_version, "state": row.state,
            "task_id": str(task_id) if task_id else None, "warnings": list(warnings),
            "at": datetime.now(UTC).isoformat(),
        }
        row.owner_actions = [*(row.owner_actions or []), event][-64:]
        row.control_version += 1

    async def approve_plan(
        self, run_id: UUID, *, expected_version: int | None = None,
        task_id: UUID | None = None, actor_id: UUID | None = None,
    ) -> DirectRunRecord:
        async with self._factory() as session:
            row = await self._locked_run(session, run_id)
            if row.state != "AWAITING_PLAN_APPROVAL" or row.cancel_requested:
                raise ValueError("direct run is not awaiting plan approval")
            if row.approval_task_id != task_id:
                raise ValueError("stale child plan approval")
            self._owner_action(row, "approve_plan", actor_id, expected_version, task_id=task_id)
            if task_id is not None:
                child = await session.get(DirectTask, task_id)
                if child is None or child.run_id != run_id or child.state != "AWAITING_PLAN_APPROVAL":
                    raise ValueError("child is not awaiting plan approval")
                child.plan_approved = True
                # The worker resumes this plan, rather than planning or replaying earlier children.
            else:
                row.plan_approved = True
            row.approval_task_id = None
            row.state = "QUEUED"
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def approve_decomposition(
        self, run_id: UUID, *, expected_version: int | None = None,
        actor_id: UUID | None = None,
    ) -> DirectRunRecord:
        async with self._factory() as session:
            row = await self._locked_run(session, run_id)
            if row.state != "AWAITING_DECOMPOSITION_APPROVAL" or row.cancel_requested:
                raise ValueError("direct run is not awaiting decomposition approval")
            self._owner_action(row, "approve_decomposition", actor_id, expected_version)
            row.state = "QUEUED"
            row.plan_approved = True
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def accept_decomposition(
        self, run_id: UUID, result: DirectDecomposeResult,
    ) -> DirectRunRecord:
        """The accepted list and parked/active state become durable together."""
        if result.status != "done" or not 1 <= len(result.tasks) <= 20:
            raise ValueError("invalid accepted decomposition")
        async with self._factory() as session:
            row = await self._locked_run(session, run_id)
            if row.state != "DECOMPOSING":
                raise ValueError("direct run is not decomposing")
            if row.cancel_requested:
                row.state = "CANCELLED"
            else:
                existing = await session.scalar(select(DirectTask.id).where(DirectTask.run_id == run_id).limit(1))
                if existing is not None:
                    raise ValueError("decomposition is already persisted")
                session.add_all([
                    DirectTask(id=uuid4(), run_id=run_id, ordinal=i, task_text=t.task, shape=t.shape)
                    for i, t in enumerate(result.tasks)
                ])
                row.summary = result.summary
                row.state = "AWAITING_DECOMPOSITION_APPROVAL" if row.review_plan_first else "RUNNING"
            row.control_version += 1
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def park_child_plan(self, run_id: UUID, task_id: UUID, plan: str) -> None:
        async with self._factory() as session:
            row = await self._locked_run(session, run_id)
            child = await session.get(DirectTask, task_id)
            if child is None or child.run_id != run_id or child.state != "PLANNING":
                raise ValueError("child is not planning")
            child.plan_text = plan
            child.state = "CANCELLED" if row.cancel_requested else "AWAITING_PLAN_APPROVAL"
            row.state = child.state
            row.approval_task_id = None if row.cancel_requested else task_id
            row.control_version += 1
            await session.commit()

    async def tasks(self, run_id: UUID) -> tuple[DirectChildTaskRecord, ...]:
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(DirectTask)
                    .where(DirectTask.run_id == run_id)
                    .order_by(DirectTask.ordinal)
                )
            ).scalars().all()
            return tuple(_task_record(row) for row in rows)

    async def create_task(
        self,
        *,
        run_id: UUID,
        ordinal: int,
        task_text: str,
        shape: str = "quick",
        id: UUID | None = None,
    ) -> DirectChildTaskRecord:
        async with self._factory() as session:
            row = DirectTask(
                id=id or uuid4(),
                run_id=run_id,
                ordinal=ordinal,
                task_text=task_text,
                shape=shape,
                state="PENDING",
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _task_record(row)

    async def update_task(self, task_id: UUID, **fields: object) -> DirectChildTaskRecord:
        async with self._factory() as session:
            row = await session.get(DirectTask, task_id)
            if row is None:
                raise ValueError("direct task was not found")
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _task_record(row)

    async def replace_pending_tasks(
        self, run_id: UUID, task_items: Sequence[dict[str, object]], *,
        expected_version: int | None = None, actor_id: UUID | None = None,
    ) -> tuple[DirectChildTaskRecord, ...]:
        if not 1 <= len(task_items) <= 20:
            raise ValueError("direct epic requires between 1 and 20 tasks")
        async with self._factory() as session:
            run = await self._locked_run(session, run_id)
            if run.state != "AWAITING_DECOMPOSITION_APPROVAL" or run.cancel_requested:
                raise ValueError("direct run is not awaiting decomposition approval")
            existing = (await session.scalars(
                select(DirectTask).where(DirectTask.run_id == run_id).order_by(DirectTask.ordinal)
            )).all()
            if any(t.state != "PENDING" for t in existing):
                raise ValueError("cannot edit tasks that have already started")
            self._owner_action(run, "edit_tasks", actor_id, expected_version)
            def snapshot(tasks: Sequence[DirectTask]) -> list[dict[str, object]]:
                return [{"id": str(t.id), "ordinal": t.ordinal, "shape": t.shape,
                         "task_sha256": sha256(t.task_text.encode("utf-8")).hexdigest()}
                        for t in tasks]

            before_tasks = snapshot(existing)
            by_id = {t.id: t for t in existing}
            seen: set[UUID] = set()
            ordered = []
            for idx, item in enumerate(task_items):
                text = item.get("task")
                shape = item.get("shape", "quick")
                if not isinstance(text, str) or not text.strip() or len(text) > 8000 or len(text.encode("utf-8")) > 65536:
                    raise ValueError("task text is invalid")
                if shape not in {"quick", "planned"}:
                    raise ValueError("direct task shape is invalid")
                raw_id = item.get("id")
                tid = UUID(str(raw_id)) if raw_id else None
                if tid is not None and (tid not in by_id or tid in seen):
                    raise ValueError("unknown or duplicate direct task identity")
                if tid is None:
                    task = DirectTask(id=uuid4(), run_id=run_id, state="PENDING")
                    session.add(task)
                else:
                    seen.add(tid)
                    task = by_id[tid]
                task.ordinal = idx
                task.task_text = text.strip()
                task.shape = str(shape)
                ordered.append(task)
            for task in existing:
                if task.id not in seen:
                    await session.delete(task)
            # Retain bounded content-free identities for the exact owner edit,
            # including its prior order, without duplicating task prose/secrets.
            run.owner_actions = [*run.owner_actions[:-1], {
                **run.owner_actions[-1], "before_tasks": before_tasks,
                "after_tasks": snapshot(ordered),
            }]
            await session.commit()
            for task in ordered:
                await session.refresh(task)
            return tuple(_task_record(t) for t in ordered)

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
        task_id: UUID | None = None,
    ) -> DirectCheckRecord:
        async with self._factory() as session:
            row = DirectCheck(
                id=uuid4(),
                run_id=run_id,
                task_id=task_id,
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
            row = await self._locked_run(session, run_id)
            if row.state == "CANCELLED" and fields.get("state", "CANCELLED") != "CANCELLED":
                raise ValueError("cancelled direct run cannot resume")
            if row.cancel_requested and fields.get("state") in {"AWAITING_PLAN_APPROVAL", "AWAITING_DECOMPOSITION_APPROVAL"}:
                fields["state"] = "CANCELLED"
            row.control_version += 1
            for key, value in fields.items():
                if key == "options" and isinstance(value, tuple):
                    value = list(value)
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def request_cancel(self, run_id: UUID, *, actor_id: UUID | None = None) -> DirectRunRecord:
        async with self._factory() as session:
            row = await self._locked_run(session, run_id)
            parked_states = {"QUEUED", "AWAITING_PLAN_APPROVAL", "AWAITING_DECOMPOSITION_APPROVAL"}
            if row.state not in parked_states | ACTIVE_DIRECT_RUN_STATES:
                return _run_record(row)
            # Cancellation always applies to the locked current state; it must
            # not depend on the browser still having the latest progress poll.
            warnings = [row.failure_reason] if row.failure_reason else []
            self._owner_action(row, "cancel", actor_id, row.control_version, warnings=warnings)
            if row.state in parked_states:
                row.state = "CANCELLED"
                row.cancel_requested = True
                await session.execute(update(DirectTask).where(
                    DirectTask.run_id == run_id, DirectTask.state == "AWAITING_PLAN_APPROVAL"
                ).values(state="CANCELLED"))
            elif row.state in ACTIVE_DIRECT_RUN_STATES:
                row.cancel_requested = True
            await session.commit()
            await session.refresh(row)
            return _run_record(row)

    async def settle_orphan_run(
        self, run_id: UUID, *, stopped_sessions: Mapping[UUID, int | None],
    ) -> str | None:
        """Apply observed exits under the same run lock as an owner's Cancel."""
        async with self._factory.begin() as session:
            row = await self._locked_run(session, run_id)
            if row.state not in ACTIVE_DIRECT_RUN_STATES:
                return None
            now = datetime.now(UTC)
            session_changed = False
            for session_id, observed_pid in stopped_sessions.items():
                changed_id = await session.scalar(
                    update(DirectSession).where(
                        DirectSession.run_id == run_id,
                        DirectSession.id == session_id,
                        DirectSession.pid.is_not_distinct_from(observed_pid),
                        DirectSession.state == "RUNNING",
                    ).values(state="FAILED", ended_at=now, failure_reason="worker interrupted")
                    .returning(DirectSession.id)
                )
                if changed_id is not None:
                    session_changed = True

            remaining = (await session.scalars(
                select(DirectSession.pid).where(
                    DirectSession.run_id == run_id, DirectSession.state == "RUNNING"
                )
            )).all()
            if remaining:
                reason = _orphan_warning(remaining, cancelled=row.cancel_requested)
            else:
                reason = "cancelled" if row.cancel_requested else "worker interrupted"

            if row.cancel_requested or not remaining:
                row.state = "CANCELLED" if row.cancel_requested else "FAILED"
                if row.shape == "epic":
                    await session.execute(
                        update(DirectTask).where(
                            DirectTask.run_id == run_id, DirectTask.state.in_(ACTIVE_DIRECT_TASK_STATES)
                        ).values(state=row.state)
                    )
            elif row.failure_reason == reason:
                if session_changed:
                    row.control_version += 1
                return None  # The warning itself has not changed.

            row.failure_reason = reason
            row.control_version += 1
            return reason if remaining else None

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
        self,
        *,
        run_id: UUID,
        session_id: UUID,
        round: int,
        client: str,
        model: str,
        verdict: str,
        findings: tuple[dict[str, object], ...],
        summary: str | None,
        degraded: bool,
        reviewer_modified_worktree: bool,
        task_id: UUID | None = None,
    ) -> DirectReviewRecord:
        async with self._factory() as session:
            row = DirectReview(
                id=uuid4(),
                run_id=run_id,
                task_id=task_id,
                session_id=session_id,
                round=round,
                client=client,
                model=model,
                verdict=verdict,
                findings=list(findings),
                summary=summary,
                degraded=degraded,
                reviewer_modified_worktree=reviewer_modified_worktree,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _review_record(row)

    async def create_session(
        self,
        *,
        run_id: UUID,
        run: DirectRunRecord | None = None,
        kind: str = "implement",
        client: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        task_id: UUID | None = None,
    ) -> DirectSessionRecord:
        async with self._factory() as session:
            source = run or _run_record(await self._require(session, run_id))
            row = DirectSession(
                id=uuid4(),
                run_id=run_id,
                task_id=task_id,
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
