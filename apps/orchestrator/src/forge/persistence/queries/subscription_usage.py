"""Bounded, snapshot-consistent aggregation of persisted subscription measurements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.subscription import (
    AttemptTelemetry,
)
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.queries.subscription_tasks import _bounds, _route, _safe
from forge.persistence.queries.subscription_usage_measurements import GroupKey
from forge.persistence.queries.subscription_usage_measurements import source_key as _source_key

_METRICS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_input_tokens": "cached_input_tokens",
    "duration_ms": "duration_ms",
    "tool_calls": "tool_call_count",
    "named_checks": "named_check_count",
    "estimated_api_cost_minor": "estimated_api_cost_minor",
}


def _encoded_field(record: Any, name: str) -> Any:
    # Select by field name, not dataclass position. PostgreSQL only selects the
    # page's groups; the existing strict decoder validates every streamed source.
    path = f'strict $.fields[*] ? (@[0] == "{name}")'
    pair = func.jsonb_path_query_first(
        record,
        literal(path, type_=JSONPATH),
        literal({}, type_=JSONB),
        True,
        type_=JSONB,
    )
    return pair[1]


@dataclass
class _Metric:
    total: int = 0
    measured: int = 0
    unknown: int = 0

    def add(self, value: int | None) -> None:
        if value is None:
            self.unknown += 1
        else:
            self.total += value
            self.measured += 1

    def payload(self) -> dict[str, int | None]:
        return {
            "known_total": self.total if self.measured else None,
            "measured_attempts": self.measured,
            "unknown_attempts": self.unknown,
        }


@dataclass
class _Group:
    key: GroupKey
    attempts: int = 0
    results: int = 0
    failed: int = 0
    metrics: dict[str, _Metric] = field(default_factory=lambda: {k: _Metric() for k in _METRICS})

    def add(
        self, result: SubscriptionAttemptResult | None, telemetry: AttemptTelemetry | None
    ) -> None:
        self.attempts += 1
        if result is not None:
            payload = result.result_payload
            if (
                type(payload.get("schema_version")) is not int
                or payload["schema_version"] not in (1, 2, 3, 4)
                or "failure" not in payload
            ):
                raise ValueError("invalid stored subscription result metadata")
            failures = (payload.get("failure"), payload.get("effective_failure"))
            if any(value is not None and type(value) is not str for value in failures):
                raise ValueError("invalid stored subscription result metadata")
            self.results += 1
            self.failed += int(any(value is not None for value in failures))
        for name, attribute in _METRICS.items():
            value = getattr(telemetry, attribute) if telemetry is not None else None
            if name == "estimated_api_cost_minor" and self.key[4] is None:
                # A unitless amount is not a coherent monetary subtotal.
                value = None
            self.metrics[name].add(value)

    def payload(self) -> dict[str, object]:
        project_id, run_id, purpose, route, currency = self.key
        return {
            "project_id": project_id,
            "run_id": run_id,
            "purpose": purpose.value,
            "effective_route": _route(route),
            "currency": _safe(currency) if currency else None,
            "attempts": self.attempts,
            "recorded_results": self.results,
            "failed_results": self.failed,
            "pending_results": self.attempts - self.results,
            **{name: metric.payload() for name, metric in self.metrics.items()},
        }


class SubscriptionUsageQuery:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def usage(
        self,
        *,
        run_id: UUID | None = None,
        offset: int = 0,
        limit: int = 25,
        include_assessment: bool = False,
    ) -> dict[str, object] | None:
        _bounds(offset, limit)
        if type(include_assessment) is not bool:
            raise TypeError("assessment selection must be boolean")
        if run_id is not None and not isinstance(run_id, UUID):
            raise TypeError("run identity must be a UUID")
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            if run_id is not None and await session.get(Run, run_id) is None:
                return None
            base = (
                select(
                    SubscriptionAttempt,
                    SubscriptionTask,
                    SubscriptionAttemptResult,
                    SubscriptionAttemptConsumption,
                    Run.project_id,
                )
                .select_from(SubscriptionAttempt)
                .join(
                    SubscriptionTask,
                    and_(
                        SubscriptionTask.run_id == SubscriptionAttempt.run_id,
                        SubscriptionTask.id == SubscriptionAttempt.task_row_id,
                    ),
                )
                .join(Run, Run.id == SubscriptionAttempt.run_id)
                .outerjoin(
                    SubscriptionAttemptResult,
                    SubscriptionAttemptResult.attempt_id == SubscriptionAttempt.id,
                )
                .outerjoin(
                    SubscriptionAttemptConsumption,
                    SubscriptionAttemptConsumption.attempt_id == SubscriptionAttempt.id,
                )
            )
            if run_id is not None:
                base = base.where(SubscriptionAttempt.run_id == run_id)
            telemetry = case(
                (
                    SubscriptionAttemptConsumption.attempt_id.is_not(None),
                    SubscriptionAttemptConsumption.telemetry_payload,
                ),
                else_=SubscriptionAttempt.telemetry_payload,
            )
            effective = _encoded_field(SubscriptionAttempt.route_payload["record"], "effective")
            purpose = _encoded_field(SubscriptionTask.payload["record"], "purpose")["value"].astext
            route_fields = [
                _encoded_field(effective, name).astext for name in ("provider", "client", "model")
            ]
            route_fields += [
                func.coalesce(_encoded_field(effective, name)["value"].astext, default)
                for name, default in (
                    ("effort", "low"),
                    ("auth_mode", "subscription"),
                    ("billing_mode", "allowance_only"),
                )
            ]
            dimensions = [
                Run.project_id,
                SubscriptionAttempt.run_id,
                purpose,
                *route_fields,
                _encoded_field(
                    func.jsonb_extract_path(telemetry, "record", type_=JSONB),
                    "currency",
                ).astext,
            ]
            columns = [value.label(f"dimension_{i}") for i, value in enumerate(dimensions)]
            order = [value.asc().nulls_last() for value in dimensions]
            # The CTE chooses complete groups, never a page of attempts. A server
            # cursor then decodes their original rows in bounded batches. This
            # keeps the shared decoder authoritative without loading the table or
            # issuing one lookup per group. Memory holds at most limit groups.
            page = (
                base.with_only_columns(*columns, maintain_column_froms=True)
                .group_by(*dimensions)
                .order_by(*order)
                .offset(offset)
                .limit(limit + 1)
                .cte("subscription_usage_groups")
            )
            statement = (
                base.join(
                    page,
                    and_(
                        *(
                            value.is_not_distinct_from(page.c[f"dimension_{i}"])
                            for i, value in enumerate(dimensions)
                        )
                    ),
                )
                .order_by(*order, SubscriptionAttempt.id)
                .execution_options(yield_per=100)
            )
            groups: dict[GroupKey, _Group] = {}
            has_more = False
            rows = await session.stream(statement)
            try:
                async for attempt, task, result, consumption, project_id in rows:
                    key, observed = _source_key(attempt, task, consumption, project_id)
                    if key not in groups:
                        if len(groups) == limit:
                            has_more = True
                            break
                        groups[key] = _Group(key)
                    groups[key].add(result, observed)
            finally:
                await rows.close()
            value: dict[str, object] = {
                "items": [group.payload() for group in groups.values()],
                "has_more": has_more,
            }
            if include_assessment:
                from forge.persistence.queries.subscription_usage_assessment import (
                    build_usage_assessment,
                )

                value["assessment"] = await build_usage_assessment(
                    session, run_id, group_keys=tuple(groups), has_more=has_more
                )
            return value
