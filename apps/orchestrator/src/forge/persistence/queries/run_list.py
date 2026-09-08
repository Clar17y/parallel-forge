"""Bounded, snapshot-consistent reads for the operator run list."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from forge.observability.redaction import redact_value
from forge.persistence.models import ModelUsage, Project, PullRequest, Run, Task


class RunListQuery:
    """Read run list rows with bounded page and aggregate queries."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def list(
        self,
        *,
        state: str | None = None,
        project_id: UUID | None = None,
        attention: bool | None = None,
        updated_since: datetime | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, object]], bool]:
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            observed_at = await session.scalar(select(func.current_timestamp()))
            if not isinstance(observed_at, datetime):
                raise TypeError("run projection timestamp unavailable")
            predicates = []
            if state is not None:
                predicates.append(Run.state == state)
            if project_id is not None:
                predicates.append(Run.project_id == project_id)
            if updated_since is not None:
                predicates.append(Run.updated_at >= updated_since)
            attention_expr = _attention_expression()
            if attention is not None:
                predicates.append(attention_expr == attention)

            page_rows = list(
                (
                    await session.execute(
                        select(Run, Task, Project, attention_expr.label("attention"))
                        .join(Task, Task.id == Run.task_id)
                        .join(Project, Project.id == Run.project_id)
                        .where(*predicates)
                        .order_by(Run.updated_at.desc(), Run.id)
                        .offset(offset)
                        .limit(limit + 1)
                    )
                ).all()
            )
            truncated = len(page_rows) > limit
            page_rows = page_rows[:limit]
            run_ids = [row[0].id for row in page_rows]
            costs = await _costs(session, run_ids)
            pull_requests = await _latest_pull_requests(session, run_ids)

            result = []
            for run, task, project, needs_attention in page_rows:
                end = (
                    run.updated_at
                    if run.state in {"COMPLETED", "FAILED", "CANCELLED"}
                    else observed_at
                )
                elapsed_ms = _elapsed_ms(run.created_at, end)
                cost = costs.get(run.id, {"currencies": [], "unpriced_calls": 0})
                result.append(
                    {
                        "task_id": task.id,
                        "task_title": _text(task.title),
                        "project_id": project.id,
                        "project_name": _text(project.name),
                        "run_id": run.id,
                        "state": run.state,
                        "version": run.version,
                        "pending_gate": run.pending_gate,
                        "next_gate": run.pending_gate,
                        "attention_required": bool(needs_attention),
                        "local_remediation_count": run.local_remediation_count,
                        "remote_remediation_count": run.remote_remediation_count,
                        "created_at": run.created_at,
                        "updated_at": run.updated_at,
                        "elapsed_ms": elapsed_ms,
                        "elapsed_seconds": elapsed_ms / 1000,
                        "pull_request": pull_requests.get(run.id),
                        "cost_summary": cost,
                    }
                )
            return result, truncated


def _attention_expression() -> ColumnElement[bool]:
    return case(
        (
            (Run.pending_gate.is_not(None))
            | Run.state.in_(("AWAITING_HUMAN_INTERVENTION", "PAUSED", "FAILED")),
            True,
        ),
        else_=False,
    )


async def _costs(session: AsyncSession, run_ids: list[UUID]) -> dict[UUID, dict[str, object]]:
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ModelUsage.run_id,
                ModelUsage.currency,
                func.coalesce(func.sum(ModelUsage.estimated_cost_minor), 0),
                func.count().filter(ModelUsage.estimated_cost_minor.is_(None)),
            )
            .where(ModelUsage.run_id.in_(run_ids))
            .group_by(ModelUsage.run_id, ModelUsage.currency)
            .order_by(ModelUsage.run_id, ModelUsage.currency)
        )
    ).all()
    result: dict[UUID, dict[str, object]] = {
        run_id: {"currencies": [], "unpriced_calls": 0} for run_id in run_ids
    }
    for run_id, currency, known, unpriced in rows:
        item = result[run_id]
        currencies = item["currencies"]
        assert isinstance(currencies, list)
        currencies.append(
            {
                "currency": currency,
                "known_cost_minor": int(known or 0),
                "unpriced_calls": int(unpriced or 0),
            }
        )
        item["unpriced_calls"] = cast(int, item["unpriced_calls"]) + int(unpriced or 0)
    return result


async def _latest_pull_requests(
    session: AsyncSession, run_ids: list[UUID]
) -> dict[UUID, dict[str, object]]:
    if not run_ids:
        return {}
    ranked = (
        select(
            PullRequest.run_id,
            PullRequest.pull_request_number,
            PullRequest.repository,
            PullRequest.head_sha,
            func.row_number()
            .over(
                partition_by=PullRequest.run_id,
                order_by=(PullRequest.updated_at.desc(), PullRequest.id),
            )
            .label("rank"),
        )
        .where(PullRequest.run_id.in_(run_ids))
        .subquery()
    )
    rows = (await session.execute(select(ranked).where(ranked.c.rank == 1))).all()
    return {
        row.run_id: {
            "number": row.pull_request_number,
            "repository": row.repository,
            "head_sha": row.head_sha,
        }
        for row in rows
    }


def _elapsed_ms(start: datetime, end: datetime) -> int:
    # Timestamps are database values; clamp malformed clock drift at zero.
    return max(0, round((end - start).total_seconds() * 1000))


def _text(value: str) -> str:
    redacted = redact_value(value)
    return redacted if isinstance(redacted, str) else "[REDACTED]"
