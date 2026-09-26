"""All-selected scalar assessment with details limited to complete usage groups."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.run import RunState
from forge.domain.subscription import (
    DelegateDecision,
    ExecutionEnvelope,
    RouteBinding,
    SpecialistPurpose,
    TaskHandoff,
    WaitDecision,
    decode_subscription_record,
)
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionEnvelope,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.queries.subscription_tasks import _route, _safe
from forge.persistence.queries.subscription_usage_acceptances import task_acceptance_sources
from forge.persistence.queries.subscription_usage_measurements import GroupKey, source_key
from forge.persistence.queries.subscription_usage_proofs import (
    APPLIED_DISPOSITIONS,
    HistoricalSource,
    UsageEvidence,
)
from forge.persistence.queries.subscription_usage_sources import UsageSources


@dataclass
class _Totals:
    all_attempts: int = 0
    primary_attempts: int = 0
    total: int = 0
    measured: int = 0
    primary_total: int = 0
    primary_measured: int = 0

    def add(self, value: int | None, primary: bool) -> None:
        self.all_attempts += 1
        self.primary_attempts += int(primary)
        if value is None:
            return
        self.total += value
        self.measured += 1
        if primary:
            self.primary_total += value
            self.primary_measured += 1

    def payload(self) -> dict[str, object]:
        denominator = self.total if self.measured else None
        numerator = self.primary_total if self.primary_measured else None
        return {
            "numerator": numerator,
            "denominator": denominator,
            "primary_attempts": self.primary_attempts,
            "all_attempts": self.all_attempts,
            "numerator_measured_attempts": self.primary_measured,
            "denominator_measured_attempts": self.measured,
            "numerator_unknown_attempts": self.primary_attempts - self.primary_measured,
            "denominator_unknown_attempts": self.all_attempts - self.measured,
            "coverage": self.measured / self.all_attempts if self.all_attempts else None,
            "share": numerator / denominator if numerator is not None and denominator else None,
        }


@dataclass
class _Outcome:
    key: GroupKey
    attempts: int = 0
    distinct_tasks: int = 0
    terminal_tasks: int = 0
    verified_results: int = 0
    unverified_results: int = 0
    pending_results: int = 0
    failed_results: int = 0
    applied_decisions: int = 0
    completed_handoffs: int = 0
    task_acceptances: int = 0
    fallback_attempts: int = 0
    latest_fallback_reason: str | None = None
    latest_result_disposition: str | None = None
    previous_task: UUID | None = field(default=None, repr=False)
    previous_accepted_task: UUID | None = field(default=None, repr=False)
    latest_fallback_at: datetime | None = field(default=None, repr=False)
    latest_result_at: datetime | None = field(default=None, repr=False)

    def observe(
        self,
        attempt: SubscriptionAttempt,
        task: SubscriptionTask,
        result: SubscriptionAttemptResult | None,
        reason: str | None,
        source: HistoricalSource | None,
        applied: bool,
    ) -> None:
        self.attempts += 1
        # The stream is ordered by task identity. One previous identity per page
        # group replaces the draft's unbounded set of every historical task.
        if task.id != self.previous_task:
            self.distinct_tasks += 1
            self.terminal_tasks += int(task.state == "terminal")
            self.previous_task = task.id
        if result is None:
            self.pending_results += 1
        elif source is None:
            self.unverified_results += 1
        else:
            self.verified_results += 1
            self.failed_results += int(source.failed)
            self.applied_decisions += int(applied)
            self.completed_handoffs += int(applied and isinstance(source.decision, TaskHandoff))
        if result is not None and (
            self.latest_result_at is None or result.created_at > self.latest_result_at
        ):
            self.latest_result_at = result.created_at
            self.latest_result_disposition = _safe(result.disposition)[:32]
        if reason is not None:
            self.fallback_attempts += 1
            if self.latest_fallback_at is None or attempt.created_at > self.latest_fallback_at:
                self.latest_fallback_at = attempt.created_at
                self.latest_fallback_reason = reason

    def payload(self) -> dict[str, object]:
        project, run, purpose, route, currency = self.key
        return {
            "project_id": project,
            "run_id": run,
            "purpose": purpose.value,
            "effective_route": _route(route),
            "currency": _safe(currency) if currency else None,
            **{
                name: getattr(self, name)
                for name in (
                    "attempts",
                    "distinct_tasks",
                    "terminal_tasks",
                    "verified_results",
                    "unverified_results",
                    "pending_results",
                    "failed_results",
                    "applied_decisions",
                    "completed_handoffs",
                    "task_acceptances",
                    "fallback_attempts",
                    "latest_fallback_reason",
                    "latest_result_disposition",
                )
            },
        }


@dataclass
class _Waits:
    delegation_decisions: int = 0
    wait_decisions: int = 0
    continued: int = 0
    unfinished: int = 0
    ended_without_continuation: int = 0
    measured_intervals: int = 0
    elapsed_ms: int = 0

    def observe(
        self,
        decision: DelegateDecision | WaitDecision,
        observed_at: datetime,
        next_at: datetime | None,
        task: SubscriptionTask,
        run_state: str,
    ) -> None:
        self.delegation_decisions += int(isinstance(decision, DelegateDecision))
        self.wait_decisions += int(isinstance(decision, WaitDecision))
        if next_at is not None:
            self.continued += 1
            if next_at >= observed_at:
                self.measured_intervals += 1
                self.elapsed_ms += (next_at - observed_at) // timedelta(milliseconds=1)
        elif task.state == "terminal" or run_state in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            self.ended_without_continuation += 1
        else:
            self.unfinished += 1

    def payload(self) -> dict[str, object]:
        decisions = self.delegation_decisions + self.wait_decisions
        return {
            "decisions": decisions,
            "continued": self.continued,
            "unfinished": self.unfinished,
            "ended_without_continuation": self.ended_without_continuation,
            "measured_intervals": self.measured_intervals,
            "unknown_intervals": decisions - self.measured_intervals,
            "elapsed_ms": self.elapsed_ms if self.measured_intervals else None,
        }


def _fallback(
    attempt: SubscriptionAttempt, envelope: SubscriptionEnvelope | None, purpose: SpecialistPurpose
) -> tuple[str, str | None]:
    if envelope is None:
        return "unknown", None
    try:
        frozen = decode_subscription_record(envelope.payload)
        binding = decode_subscription_record(attempt.route_payload)
        if (
            not isinstance(frozen, ExecutionEnvelope)
            or not isinstance(binding, RouteBinding)
            or frozen.run_id != attempt.run_id
            or not frozen.permits_route(purpose, binding)
        ):
            raise ValueError
        if binding.effective == frozen.route_for(purpose).effective:
            return "preferred", None
        if binding.mapping_applied is None:
            raise ValueError
        return "fallback", _safe(binding.mapping_applied.reason)[:255]
    except TypeError, ValueError, KeyError:
        raise ValueError("invalid stored subscription fallback attribution") from None


async def build_usage_assessment(
    session: AsyncSession,
    run_id: UUID | None = None,
    *,
    group_keys: tuple[GroupKey, ...] = (),
    has_more: bool = False,
) -> dict[str, object]:
    if len(group_keys) > 100 or len(set(group_keys)) != len(group_keys):
        raise ValueError("assessment group page exceeds its bound")
    totals = {name: _Totals() for name in ("input_tokens", "output_tokens", "duration_ms")}
    outcomes = {key: _Outcome(key) for key in group_keys}
    routes = {"fallback": 0, "preferred": 0, "unknown": 0}
    waits = _Waits()
    unverified_decisions = 0
    admissions = select(
        SubscriptionAttempt.id,
        func.lead(SubscriptionAttempt.created_at)
        .over(
            partition_by=(SubscriptionAttempt.run_id, SubscriptionAttempt.task_row_id),
            order_by=SubscriptionAttempt.attempt_number,
        )
        .label("next_at"),
    ).where(SubscriptionAttempt.lease_owner.is_not(None))
    if run_id is not None:
        admissions = admissions.where(SubscriptionAttempt.run_id == run_id)
    next_admission = admissions.cte("next_admission")
    tables = UsageSources.named("assessed")
    statement = (
        tables.join(
            select(
                *tables.columns(),
                Run.project_id,
                SubscriptionEnvelope,
                next_admission.c.next_at,
                Run.state,
            ).select_from(tables.attempt)
        )
        .join(Run, Run.id == tables.attempt.run_id)
        .outerjoin(SubscriptionEnvelope, SubscriptionEnvelope.run_id == tables.attempt.run_id)
        .outerjoin(next_admission, next_admission.c.id == tables.attempt.id)
        .where(tables.attempt.lease_owner.is_not(None))
        .order_by(tables.task.id, tables.attempt.attempt_number)
        .execution_options(yield_per=100)
    )
    if run_id is not None:
        statement = statement.where(tables.attempt.run_id == run_id)
    rows = await session.stream(statement)
    try:
        async for row in rows:
            evidence = UsageEvidence(*row[:7])
            attempt, task, result, consumption = (
                evidence.attempt,
                evidence.task,
                evidence.result,
                evidence.consumption,
            )
            project_id, envelope, next_at, run_state = row[7:]
            key, telemetry = source_key(attempt, task, consumption, project_id)
            for name, value in totals.items():
                value.add(
                    getattr(telemetry, name) if telemetry else None,
                    key[2] is SpecialistPurpose.PRIMARY,
                )
            classification, reason = _fallback(attempt, envelope, key[2])
            routes[classification] += 1
            source = evidence.source(envelope)
            applied = evidence.applied(source)
            if (
                result is not None
                and result.accepted
                and result.disposition in APPLIED_DISPOSITIONS
                and not applied
            ):
                unverified_decisions += 1
            if (
                applied
                and source is not None
                and isinstance(source.decision, (DelegateDecision, WaitDecision))
            ):
                assert evidence.record is not None
                waits.observe(source.decision, evidence.record.created_at, next_at, task, run_state)
            if key in outcomes:
                outcomes[key].observe(attempt, task, result, reason, source, applied)
    finally:
        await rows.close()
    async for primary_key, child_key, task_id in task_acceptance_sources(session, run_id):
        if child_key is None:
            unverified_decisions += 1
            if primary_key in outcomes:
                outcomes[primary_key].applied_decisions -= 1
        elif child_key in outcomes:
            outcome = outcomes[child_key]
            if outcome.previous_accepted_task != task_id:
                outcome.task_acceptances += 1
                outcome.previous_accepted_task = task_id
    repair_statement = (
        select(func.count(SubscriptionRepairDebit.attempt_id))
        .join(SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionRepairDebit.attempt_id)
        .where(SubscriptionAttempt.lease_owner.is_not(None))
    )
    if run_id is not None:
        repair_statement = repair_statement.where(SubscriptionAttempt.run_id == run_id)
    repairs = int(await session.scalar(repair_statement) or 0)
    return {
        "primary_turns": totals["input_tokens"].primary_attempts,
        "all_attempts": totals["input_tokens"].all_attempts,
        "delegation_decisions": waits.delegation_decisions,
        "wait_decisions": waits.wait_decisions,
        "repair_debits": repairs,
        "fallback_attempts": routes["fallback"],
        "preferred_attempts": routes["preferred"],
        "unknown_route_attempts": routes["unknown"],
        "unverified_decisions": unverified_decisions,
        "shares": {name: value.payload() for name, value in totals.items()},
        "waits": waits.payload(),
        "outcomes": [value.payload() for value in outcomes.values()],
        "outcomes_has_more": has_more,
    }
