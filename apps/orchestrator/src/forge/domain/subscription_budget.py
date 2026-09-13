"""Usage accounting independent of provider transport and database transactions."""

from __future__ import annotations

from dataclasses import dataclass, fields
from types import NotImplementedType

from forge.domain.subscription import AttemptTelemetry, TaskBudget


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageAmounts:
    """Zero means measured empty usage; None means an unknown amount."""

    duration_ms: int | None = 0
    tool_calls: int | None = 0
    named_checks: int | None = 0
    provider_attempts: int | None = 0
    repairs: int | None = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    cost_minor: int | None = 0

    def __post_init__(self) -> None:
        for name, value in self.values().items():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or unknown")

    def values(self) -> dict[str, int | None]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def __add__(self, other: object) -> UsageAmounts | NotImplementedType:
        if not isinstance(other, UsageAmounts):
            return NotImplemented
        right = other.values()
        values: dict[str, int | None] = {}
        for name, value in self.values().items():
            other_value = right[name]
            values[name] = None if value is None or other_value is None else value + other_value
        return UsageAmounts(**values)

    def fits_within(self, budget: TaskBudget) -> bool:
        """Unknown reservations cannot fit a finite limit."""
        limits = budget_ceiling(budget).values()
        return all(
            limit is None or (value is not None and value <= limit)
            for name, value in self.values().items()
            for limit in (limits[name],)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptCharge:
    observed: UsageAmounts
    charged: UsageAmounts
    unknown_fields: tuple[str, ...]
    exceeded_fields: tuple[str, ...]


def budget_ceiling(budget: TaskBudget) -> UsageAmounts:
    if not isinstance(budget, TaskBudget):
        raise TypeError("task budget is required")
    return UsageAmounts(
        duration_ms=budget.max_duration_seconds * 1000,
        tool_calls=budget.max_tool_calls,
        named_checks=budget.max_named_checks,
        provider_attempts=budget.max_provider_attempts,
        repairs=budget.max_repairs,
        input_tokens=budget.max_input_tokens,
        output_tokens=budget.max_output_tokens,
        cost_minor=budget.max_cost_minor,
    )


def project_attempt_charge(
    reservation: TaskBudget, telemetry: AttemptTelemetry | None, *, repair: bool
) -> AttemptCharge:
    """Preserve measurements, including overages, and retain unknown ceilings.

    One attempt was already durably admitted. A missing result does not make
    that attempt free. Policy admission and row locking belong to the caller;
    this projection never rejects measured overage and thereby loses its debit.
    """
    if not isinstance(reservation, TaskBudget) or type(repair) is not bool:
        raise TypeError("a task reservation and exact repair flag are required")
    if telemetry is not None and not isinstance(telemetry, AttemptTelemetry):
        raise TypeError("telemetry must be measured or unknown")
    if reservation.max_provider_attempts != 1 or reservation.max_repairs != int(repair):
        raise ValueError("an attempt reservation must bind one attempt and its repair unit")
    observed = UsageAmounts(
        duration_ms=None if telemetry is None else telemetry.duration_ms,
        tool_calls=None if telemetry is None else telemetry.tool_call_count,
        named_checks=None if telemetry is None else telemetry.named_check_count,
        provider_attempts=1,
        repairs=int(repair),
        input_tokens=None if telemetry is None else telemetry.input_tokens,
        output_tokens=None if telemetry is None else telemetry.output_tokens,
        cost_minor=None if telemetry is None else telemetry.estimated_api_cost_minor,
    )
    limits = budget_ceiling(reservation).values()
    values = observed.values()
    return AttemptCharge(
        observed=observed,
        charged=UsageAmounts(
            **{name: limits[name] if value is None else value for name, value in values.items()}
        ),
        unknown_fields=tuple(name for name, value in values.items() if value is None),
        exceeded_fields=tuple(
            name
            for name, value in values.items()
            for limit in (limits[name],)
            if value is not None and limit is not None and value > limit
        ),
    )
