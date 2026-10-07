"""Cumulative epic usage from persisted authoring and bound child attempts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.subscription import (
    AttemptTelemetry,
    RouteBinding,
    TaskBudget,
    decode_subscription_record,
)
from forge.domain.subscription_quota import QuotaPoolKey
from forge.persistence.models.epic_brainstorm import BrainstormBudgetLedger
from forge.persistence.models.epic_run_bridge import (
    EpicBudgetAdmissionPermit,
    EpicChildBudgetHold,
    EpicExecutionControl,
    EpicItemAttempt,
)
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_quota import SubscriptionQuotaAdmission
from forge.persistence.models.subscription_usage import (
    SubscriptionAttemptConsumption,
    SubscriptionAttemptReservation,
)
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository

_DIMENSIONS = (
    "duration_ms",
    "tool_call_count",
    "input_tokens",
    "output_tokens",
    "estimated_api_cost_minor",
    "provider_attempts",
)
_CHILD_KEYS = {
    "duration_ms": "duration_ms",
    "tool_call_count": "tool_calls",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "estimated_api_cost_minor": "cost_minor",
    "provider_attempts": "provider_attempts",
}


def _amount(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _decode_optional(payload: object) -> object | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        return decode_subscription_record(payload)
    except TypeError, ValueError:
        return None


def budget_amounts(budget: TaskBudget) -> dict[str, int | None]:
    return {
        "duration_ms": budget.max_duration_seconds * 1000,
        "tool_call_count": budget.max_tool_calls,
        "input_tokens": budget.max_input_tokens,
        "output_tokens": budget.max_output_tokens,
        "estimated_api_cost_minor": budget.max_cost_minor,
        "provider_attempts": budget.max_provider_attempts,
    }


@dataclass(frozen=True, slots=True)
class EpicBudgetTotals:
    known: dict[str, int]
    held: dict[str, int]
    unknown: bool
    currency: str | None
    warnings: tuple[str, ...]

    @property
    def shared_unknown(self) -> bool:
        """Unproved lineage needs owner authority; telemetry follows authoring policy."""
        return any(
            warning.startswith("child_")
            or warning
            in {"authoring_positive_cost_lineage_unproved", "epic_cost_currency_conflict"}
            for warning in self.warnings
        )

    def blockers(
        self,
        ceiling: TaskBudget,
        next_hold: TaskBudget,
        disabled_dimensions: frozenset[str] = frozenset(),
    ) -> list[str]:
        limits, upcoming = budget_amounts(ceiling), budget_amounts(next_hold)
        result = list(self.warnings)
        for key, limit in limits.items():
            if limit is None or key in disabled_dimensions:
                continue
            amount = upcoming[key]
            if amount is None or self.known[key] + self.held[key] + amount > limit:
                result.append(f"epic_budget_{key}_exhausted")
        if self.unknown:
            result.append("epic_usage_unknown")
        return list(dict.fromkeys(result))


class PostgresEpicBudgetRepository:
    def __init__(self, session: AsyncSession, *, legacy_hold: TaskBudget) -> None:
        self.session = session
        self.legacy_hold = legacy_hold

    async def ceiling(self, epic_id: UUID) -> TaskBudget | None:
        ledger = await self.session.get(BrainstormBudgetLedger, epic_id)
        if ledger is None:
            return None
        value = decode_subscription_record(ledger.ceiling)
        if not isinstance(value, TaskBudget):
            raise TypeError("stored epic budget ceiling is invalid")
        return value

    async def totals(
        self, epic_id: UUID, *, for_internal_run: UUID | None = None
    ) -> EpicBudgetTotals:
        known = dict.fromkeys(_DIMENSIONS, 0)
        held = dict.fromkeys(_DIMENSIONS, 0)
        warnings: list[str] = []
        authoring = PostgresBrainstormRepository(self.session)
        prior = await authoring._epic_attempts(epic_id)
        authoring_known, authoring_held, authoring_unknown = authoring._charges(prior)
        for key in authoring_known:
            known[key] += authoring_known[key]
            held[key] += authoring_held[key]
        # Admission consumes an attempt slot even if the process settles before
        # launch. Keep unsettled, unstarted attempts as holds until resolved.
        known["provider_attempts"] += sum(
            attempt.process_started or attempt.process_settled for attempt in prior
        )
        held["provider_attempts"] += sum(
            not attempt.process_started and not attempt.process_settled for attempt in prior
        )
        unknown = authoring_unknown > 0
        if unknown:
            warnings.append("authoring_usage_unknown")
        currency_set: set[str] = set()
        money_unproved = False
        if prior:
            scopes = await authoring._attempt_scopes(prior)
            _, unsafe_money, _, _, has_money, unknown_money_hold = authoring._money_evidence(
                prior, scopes
            )
            if has_money and (unsafe_money or unknown_money_hold):
                unknown = True
                money_unproved = True
                warnings.append("authoring_cost_lineage_unproved")
            for authoring_attempt in prior:
                charge, _, _ = authoring._charges([authoring_attempt])
                authoring_cost = charge["estimated_api_cost_minor"]
                if authoring_cost <= 0:
                    continue
                scope = scopes.get(authoring_attempt.id)
                attempt_unit, unsafe, _, _, _, unproved_hold = authoring._money_evidence(
                    [authoring_attempt],
                    {authoring_attempt.id: scope} if scope is not None else {},
                )
                if (
                    attempt_unit is not None
                    and not unsafe
                    and not unproved_hold
                    and authoring_attempt.process_settled
                    and authoring_attempt.usage_known is True
                ):
                    currency_set.add(attempt_unit)
                else:
                    known["estimated_api_cost_minor"] -= authoring_cost
                    held["estimated_api_cost_minor"] += authoring_cost
                    unknown = True
                    money_unproved = True
                    if "authoring_cost_lineage_unproved" not in warnings:
                        warnings.append("authoring_cost_lineage_unproved")
                    if (
                        unsafe or attempt_unit is None
                    ) and "authoring_positive_cost_lineage_unproved" not in warnings:
                        warnings.append("authoring_positive_cost_lineage_unproved")
        child_rows = (
            await self.session.scalars(
                select(EpicItemAttempt)
                .where(EpicItemAttempt.epic_id == epic_id)
                .order_by(EpicItemAttempt.created_at, EpicItemAttempt.id)
            )
        ).all()
        holds = {
            hold.attempt_id: hold
            for hold in (
                await self.session.scalars(
                    select(EpicChildBudgetHold).where(EpicChildBudgetHold.epic_id == epic_id)
                )
            ).all()
        }
        for child in child_rows:
            hold = holds.get(child.id)
            floor_budget = self.legacy_hold
            if hold is not None:
                decoded = _decode_optional(hold.budget_payload)
                if isinstance(decoded, TaskBudget):
                    floor_budget = decoded
                else:
                    unknown = True
                    money_unproved = True
                    warnings.append("child_hold_unproved")
            floor = budget_amounts(floor_budget)
            child_known = dict.fromkeys(_DIMENSIONS, 0)
            child_held = dict.fromkeys(_DIMENSIONS, 0)
            attempts = (
                await self.session.scalars(
                    select(SubscriptionAttempt).where(SubscriptionAttempt.run_id == child.run_id)
                )
            ).all()
            if not attempts and child.run_id != for_internal_run:
                unknown = True
                money_unproved = True
                warnings.append("child_usage_unproved")
            for attempt in attempts:
                reservation = await self.session.get(SubscriptionAttemptReservation, attempt.id)
                consumption = await self.session.get(SubscriptionAttemptConsumption, attempt.id)
                if reservation is None:
                    unknown = True
                    money_unproved = True
                    warnings.append("child_reservation_missing")
                    for key in _DIMENSIONS:
                        amount = floor[key]
                        if amount is not None:
                            child_held[key] += amount
                    continue
                reserved = _decode_optional(reservation.budget_payload)
                if not isinstance(reserved, TaskBudget):
                    unknown = True
                    money_unproved = True
                    warnings.append("child_reservation_unproved")
                    for key in _DIMENSIONS:
                        amount = floor[key]
                        if amount is not None:
                            child_held[key] += amount
                    continue
                reserved_amounts = budget_amounts(reserved)
                if consumption is None:
                    unknown = True
                    money_unproved = True
                    warnings.append("child_consumption_unsettled")
                    for key in _DIMENSIONS:
                        amount = reserved_amounts[key]
                        if amount is not None:
                            child_held[key] += amount
                    continue
                observed = consumption.observed
                for key in _DIMENSIONS:
                    amount = _amount(observed.get(_CHILD_KEYS[key]))
                    if amount is None:
                        unknown = True
                        warnings.append("child_usage_unproved")
                        if key == "estimated_api_cost_minor":
                            money_unproved = True
                        held_amount = reserved_amounts[key]
                        if held_amount is not None:
                            child_held[key] += held_amount
                    else:
                        child_known[key] += amount
                cost = _amount(observed.get("cost_minor"))
                if cost is not None:
                    telemetry = _decode_optional(consumption.telemetry_payload)
                    route = _decode_optional(attempt.route_payload)
                    admission = await self.session.get(SubscriptionQuotaAdmission, attempt.id)
                    scope_valid = False
                    if admission is not None:
                        try:
                            QuotaPoolKey(admission.provider, admission.account, admission.pool)
                            scope_valid = True
                        except ValueError:
                            pass
                    if (
                        not isinstance(telemetry, AttemptTelemetry)
                        or not isinstance(route, RouteBinding)
                        or admission is None
                        or not scope_valid
                        or admission.provider != route.effective.provider
                        or telemetry.estimated_api_cost_minor != cost
                        or (
                            cost > 0
                            and PostgresBrainstormRepository._currency(telemetry.currency) is None
                        )
                    ):
                        unknown = True
                        money_unproved = True
                        warnings.append("child_cost_lineage_unproved")
                        child_known["estimated_api_cost_minor"] -= cost
                        child_held["estimated_api_cost_minor"] += max(
                            cost, reserved_amounts["estimated_api_cost_minor"] or 0
                        )
                    else:
                        if cost > 0 and telemetry.currency is not None:
                            currency_set.add(telemetry.currency)
            # The bridge hold is an exposure floor, never an additive second
            # charge for the same run. Only explicit quiescent settlement lifts it.
            for key in _DIMENSIONS:
                exposure = child_known[key] + child_held[key]
                floor_amount = floor[key] or 0
                if child.run_id != for_internal_run and (
                    hold is None or not hold.effects_settled or not attempts
                ):
                    child_held[key] += max(floor_amount - exposure, 0)
            for key in _DIMENSIONS:
                known[key] += child_known[key]
                held[key] += child_held[key]
        if len(currency_set) > 1:
            unknown = True
            warnings.append("epic_cost_currency_conflict")
            # Incomparable minor units cannot be represented as one aggregate.
            # Every source observation remains in its original durable row.
            known["estimated_api_cost_minor"] = 0
            held["estimated_api_cost_minor"] = 0
        currency = (
            next(iter(currency_set)) if len(currency_set) == 1 and not money_unproved else None
        )
        return EpicBudgetTotals(
            known=known,
            held=held,
            unknown=unknown,
            currency=currency,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def internal_blockers(self, run_id: UUID, next_hold: TaskBudget) -> list[str]:
        child = await self.session.scalar(
            select(EpicItemAttempt).where(EpicItemAttempt.run_id == run_id)
        )
        if child is None:
            return []  # Standalone v0.1/v0.2 runs have no epic admission rule.
        control = await self.session.get(EpicExecutionControl, child.execution_id)
        blockers = []
        if control is not None and control.state != "ACTIVE":
            blockers.append("execution_not_active")
        ceiling = await self.ceiling(child.epic_id)
        if ceiling is None:
            blockers.append("epic_ceiling_unset")
        else:
            totals = await self.totals(child.epic_id, for_internal_run=run_id)
            ledger = await self.session.get(BrainstormBudgetLedger, child.epic_id)
            blockers.extend(
                totals.blockers(
                    ceiling,
                    next_hold,
                    frozenset(ledger.disabled_dimensions) if ledger else frozenset(),
                )
            )
        return list(dict.fromkeys(blockers))

    async def available_permit(self, run_id: UUID) -> EpicBudgetAdmissionPermit | None:
        permit: EpicBudgetAdmissionPermit | None = await self.session.scalar(
            select(EpicBudgetAdmissionPermit)
            .where(
                EpicBudgetAdmissionPermit.run_id == run_id,
                EpicBudgetAdmissionPermit.consumed_attempt_id.is_(None),
            )
            .order_by(EpicBudgetAdmissionPermit.created_at, EpicBudgetAdmissionPermit.id)
            .limit(1)
        )
        return permit

    async def consume_permit(self, run_id: UUID, attempt_id: UUID, warnings: list[str]) -> None:
        permit = await self.available_permit(run_id)
        if permit is None:
            raise ValueError("owner admission permit is no longer available")
        permit.consumed_attempt_id = attempt_id
        permit.warnings = warnings
        await self.session.flush()
