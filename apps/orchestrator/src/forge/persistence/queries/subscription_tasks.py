"""Bounded snapshot reads of subscription task and attempt state."""

from dataclasses import asdict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.subscription import (
    AttemptTelemetry,
    ExecutionEnvelope,
    LogicalTaskContract,
    RouteBinding,
    RouteSpec,
    decode_subscription_record,
)
from forge.domain.subscription_execution import run_allows_subscription_attempt
from forge.domain.subscription_feedback import MAX_FEEDBACK_PER_TASK
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPolicy
from forge.observability.redaction import redact_value
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionEnvelope,
    SubscriptionTask,
)
from forge.persistence.models.subscription_feedback import SubscriptionTaskFeedback
from forge.persistence.queries.subscription_capacity import capacity_observation
from forge.persistence.queries.subscription_task_controls import (
    control_view,
    latest_control_receipts,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.subscription_quota import PostgresSubscriptionQuotaRepository


class SubscriptionTaskQuery:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        quota_policy: QuotaPolicy | None = None,
    ) -> None:
        self._factory = session_factory
        self._quota_policy = quota_policy or QuotaPolicy()

    async def tasks(
        self, run_id: UUID, *, offset: int = 0, limit: int = 25
    ) -> dict[str, object] | None:
        _bounds(offset, limit)
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            run = await session.get(Run, run_id)
            if run is None:
                return None
            envelope = await session.get(SubscriptionEnvelope, run_id)
            frozen = decode_subscription_record(envelope.payload) if envelope is not None else None
            if frozen is not None and not isinstance(frozen, ExecutionEnvelope):
                raise ValueError("invalid stored subscription envelope")
            preferred_routes = dict(frozen.routes) if frozen is not None else {}
            candidate = await session.get(SubscriptionSchedulerRun, run_id)
            quota = PostgresSubscriptionQuotaRepository(session, policy=self._quota_policy)
            quota_statuses = await quota.list_status()
            quota_by_key = {status.key: _quota_status(status) for status in quota_statuses}
            page = list(
                (
                    await session.execute(
                        select(SubscriptionTask, SubscriptionScheduledTask)
                        .outerjoin(
                            SubscriptionScheduledTask,
                            (
                                (SubscriptionScheduledTask.run_id == SubscriptionTask.run_id)
                                & (SubscriptionScheduledTask.task_id == SubscriptionTask.id)
                            ),
                        )
                        .where(SubscriptionTask.run_id == run_id)
                        .order_by(SubscriptionTask.created_at, SubscriptionTask.id)
                        .offset(offset)
                        .limit(limit + 1)
                    )
                ).all()
            )
            has_more = len(page) > limit
            page = page[:limit]
            ids = [row[0].id for row in page]
            control_receipts = await latest_control_receipts(session, ids)
            feedback_receipts = await _feedback_receipts(session, run_id, ids)
            effects = (
                dict(
                    (
                        await session.execute(
                            select(SubscriptionScheduledEffect.task_id, func.count())
                            .where(
                                SubscriptionScheduledEffect.run_id == run_id,
                                SubscriptionScheduledEffect.task_id.in_(ids),
                                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                            )
                            .group_by(SubscriptionScheduledEffect.task_id)
                        )
                    )
                    .tuples()
                    .all()
                )
                if ids
                else {}
            )
            tasks: list[dict[str, object]] = []
            providers: set[str] = set()
            waiting: dict[int, str] = {}
            for row, scheduled in page:
                contract = decode_subscription_record(row.payload)
                if not isinstance(contract, LogicalTaskContract) or (
                    contract.run_id != run_id or contract.task_id != row.id
                ):
                    raise ValueError("invalid stored subscription task")
                providers.add(contract.route.effective.provider)
                if (
                    scheduled is not None
                    and scheduled.state == "queued"
                    and not scheduled.pause_requested
                    and not scheduled.cancel_requested
                    and not row.pause_requested
                    and not row.cancel_requested
                ):
                    waiting[len(tasks)] = contract.route.effective.provider
                key = self._quota_policy.key_for(contract.route.effective)
                preferred = preferred_routes.get(contract.purpose)
                if key not in quota_by_key:
                    # A bounded global list must not hide this task's pool.
                    # Missing observations explicitly mean unknown telemetry.
                    quota_by_key[key] = _quota_status(await quota.status(key))
                tasks.append(
                    {
                        "task_id": row.id,
                        "parent_task_id": contract.parent_task_id,
                        "dependency_task_ids": list(contract.dependency_task_ids),
                        "purpose": contract.purpose.value,
                        "owned_paths": [_safe(value) for value in contract.owned_paths],
                        "state": scheduled.state if scheduled is not None else row.state,
                        "pause_requested": row.pause_requested
                        or bool(scheduled is not None and scheduled.pause_requested),
                        "cancel_requested": row.cancel_requested
                        or bool(scheduled is not None and scheduled.cancel_requested),
                        "version": row.version,
                        "repairs": scheduled.repairs if scheduled is not None else None,
                        "unsettled_effects": effects.get(row.id, 0),
                        "control": await control_view(
                            session, row, scheduled, control_receipts.get(row.id)
                        ),
                        "feedback_receipts": feedback_receipts.get(row.id, []),
                        "quota_status": quota_by_key[key],
                        "requested_route": _route(contract.route.requested),
                        "effective_route": _route(contract.route.effective),
                        "fallback_selected": preferred is not None
                        and contract.route.effective != preferred.effective,
                        "capacity_waits": [],
                    }
                )
            capacity = await capacity_observation(session, candidate, providers)
            if capacity is not None:
                for index, provider in waiting.items():
                    tasks[index]["capacity_waits"] = capacity.waits(provider)
            return {
                "run_id": run_id,
                "subscription": envelope is not None,
                "run_version": run.version,
                "run_is_terminal": run.state in ("COMPLETED", "CANCELLED", "FAILED"),
                "run_allows_execution": run_allows_subscription_attempt(run.state, run.pending_gate)
                and not await PostgresCommandRepository(
                    session=session
                ).has_pending_current_control_stop(run_id=run_id, expected_run_version=run.version),
                "candidate_epoch": candidate.candidate_epoch if candidate else None,
                "candidate_state": candidate.candidate_state if candidate else None,
                "capacity": capacity.projection() if capacity is not None else None,
                "tasks": tasks,
                "has_more": has_more,
                "quota_statuses": [_quota_status(value) for value in quota_statuses],
            }

    async def attempts(
        self, run_id: UUID, task_id: UUID, *, offset: int = 0, limit: int = 25
    ) -> dict[str, object] | None:
        _bounds(offset, limit)
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            task = await session.scalar(
                select(SubscriptionTask).where(
                    SubscriptionTask.run_id == run_id, SubscriptionTask.id == task_id
                )
            )
            if task is None:
                return None
            rows = list(
                (
                    await session.scalars(
                        select(SubscriptionAttempt)
                        .where(
                            SubscriptionAttempt.run_id == run_id,
                            SubscriptionAttempt.task_row_id == task_id,
                        )
                        .order_by(SubscriptionAttempt.attempt_number, SubscriptionAttempt.id)
                        .offset(offset)
                        .limit(limit + 1)
                    )
                ).all()
            )
            return {
                "run_id": run_id,
                "task_id": task_id,
                "attempts": [_attempt(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
            }


def _bounds(offset: int, limit: int) -> None:
    if (
        type(offset) is not int
        or type(limit) is not int
        or not (0 <= offset <= 1_000_000 and 1 <= limit <= 100)
    ):
        raise ValueError("invalid subscription projection bounds")


def _safe(value: str) -> str:
    return str(redact_value(value))


async def _feedback_receipts(
    session: AsyncSession, run_id: UUID, task_ids: list[UUID]
) -> dict[UUID, list[dict[str, object]]]:
    if not task_ids:
        return {}
    receipt_order = (
        SubscriptionTaskFeedback.created_at,
        SubscriptionTaskFeedback.id,
    )
    ranked = (
        select(
            SubscriptionTaskFeedback.id.label("receipt_id"),
            SubscriptionTaskFeedback.primary_task_id,
            SubscriptionTaskFeedback.task_id,
            SubscriptionTaskFeedback.state,
            SubscriptionTaskFeedback.feedback_digest,
            SubscriptionTaskFeedback.feedback_bytes,
            SubscriptionTaskFeedback.created_at,
            SubscriptionTaskFeedback.closed_reason,
            func.row_number()
            .over(
                partition_by=SubscriptionTaskFeedback.task_id,
                order_by=receipt_order,
            )
            .label("receipt_rank"),
        )
        .where(
            SubscriptionTaskFeedback.run_id == run_id,
            SubscriptionTaskFeedback.task_id.in_(task_ids),
        )
        .subquery()
    )
    rows = list(
        (
            await session.execute(
                select(ranked)
                .where(ranked.c.receipt_rank <= MAX_FEEDBACK_PER_TASK + 1)
                .order_by(ranked.c.task_id, ranked.c.receipt_rank)
            )
        ).mappings()
    )
    projected: dict[UUID, list[dict[str, object]]] = {}
    for row in rows:
        receipts = projected.setdefault(row["task_id"], [])
        receipts.append(
            {
                "receipt_id": row["receipt_id"],
                "primary_task_id": row["primary_task_id"],
                "status": row["state"],
                "feedback_digest": row["feedback_digest"],
                "feedback_bytes": row["feedback_bytes"],
                "observed_at": row["created_at"],
                "closed_reason": row["closed_reason"],
            }
        )
        if len(receipts) > MAX_FEEDBACK_PER_TASK:
            raise ValueError("subscription feedback projection exceeds its bound")
    return projected


def _route(route: RouteSpec) -> dict[str, object]:
    return {key: _safe(str(value)) for key, value in asdict(route).items()}


def _attempt(row: SubscriptionAttempt) -> dict[str, object]:
    route = decode_subscription_record(row.route_payload)
    if not isinstance(route, RouteBinding):
        raise TypeError("invalid stored subscription route")
    telemetry = (
        decode_subscription_record(row.telemetry_payload)
        if row.telemetry_payload is not None
        else None
    )
    if telemetry is not None and not isinstance(telemetry, AttemptTelemetry):
        raise ValueError("invalid stored subscription telemetry")
    return {
        "attempt_id": row.id,
        "attempt_number": row.attempt_number,
        "state": row.status,
        "requested_route": _route(route.requested),
        "effective_route": _route(route.effective),
        "input_tokens": telemetry.input_tokens if telemetry else None,
        "output_tokens": telemetry.output_tokens if telemetry else None,
        "cached_tokens": telemetry.cached_input_tokens if telemetry else None,
        "duration_ms": telemetry.duration_ms if telemetry else None,
        "tool_calls": telemetry.tool_call_count if telemetry else None,
        "named_checks": telemetry.named_check_count if telemetry else None,
        "estimated_api_cost_minor": telemetry.estimated_api_cost_minor if telemetry else None,
        "currency": _safe(telemetry.currency) if telemetry and telemetry.currency else None,
        "quota_status": telemetry.quota_status.value if telemetry else "unknown",
    }


def _quota_status(row: PoolQuotaStatus) -> dict[str, object]:
    return {
        "provider": _safe(row.key.provider),
        "account": _safe(row.key.account),
        "pool": _safe(row.key.pool),
        "status": row.status,
        "revision": row.revision,
        "observed_at": row.observed_at,
        "reason": _safe(row.reason or ""),
        "reset_at": row.reset_at,
        "next_eligible_at": row.next_eligible_at,
        "retry_basis": row.retry_basis,
        "probe_attempt_id": str(row.probe_attempt_id) if row.probe_attempt_id else None,
        "recovered_at": row.recovered_at,
    }
