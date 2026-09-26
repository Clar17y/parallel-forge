"""Measured consumption and conservative unknown reservation arithmetic."""

from dataclasses import replace

import pytest
from forge.domain.subscription import AttemptTelemetry, TaskBudget
from forge.domain.subscription_budget import UsageAmounts, project_attempt_charge


def _reservation(**changes):
    return replace(
        TaskBudget(
            max_duration_seconds=10,
            max_tool_calls=8,
            max_named_checks=2,
            max_provider_attempts=1,
            max_repairs=0,
            max_input_tokens=100,
            max_output_tokens=40,
            max_cost_minor=20,
        ),
        **changes,
    )


def test_known_usage_charges_actual_values_and_one_admitted_attempt():
    telemetry = AttemptTelemetry(
        duration_ms=1250,
        tool_call_count=2,
        named_check_count=1,
        input_tokens=30,
        output_tokens=8,
        estimated_api_cost_minor=3,
    )
    charge = project_attempt_charge(_reservation(), telemetry, repair=False)
    assert charge.charged == UsageAmounts(
        duration_ms=1250,
        tool_calls=2,
        named_checks=1,
        provider_attempts=1,
        repairs=0,
        input_tokens=30,
        output_tokens=8,
        cost_minor=3,
    )
    assert charge.observed == charge.charged
    assert charge.unknown_fields == ()
    assert charge.exceeded_fields == ()


def test_missing_telemetry_retains_each_bounded_ceiling_but_charges_attempt_once():
    charge = project_attempt_charge(_reservation(max_repairs=1), None, repair=True)
    assert charge.charged == UsageAmounts(
        duration_ms=10_000,
        tool_calls=8,
        named_checks=2,
        provider_attempts=1,
        repairs=1,
        input_tokens=100,
        output_tokens=40,
        cost_minor=20,
    )
    assert charge.observed.duration_ms is None
    assert charge.observed.input_tokens is None
    assert charge.observed.provider_attempts == charge.observed.repairs == 1
    assert set(charge.unknown_fields) == {
        "duration_ms",
        "tool_calls",
        "named_checks",
        "input_tokens",
        "output_tokens",
        "cost_minor",
    }


def test_partial_measurements_release_only_known_unused_capacity():
    charge = project_attempt_charge(
        _reservation(),
        AttemptTelemetry(
            duration_ms=1,
            tool_call_count=0,
            named_check_count=0,
            input_tokens=0,
            output_tokens=None,
            estimated_api_cost_minor=None,
        ),
        repair=False,
    )
    assert charge.charged.input_tokens == 0
    assert charge.charged.output_tokens == 40
    assert charge.charged.cost_minor == 20
    assert charge.charged.duration_ms == 1
    assert charge.unknown_fields == ("output_tokens", "cost_minor")


def test_unbounded_unknowns_remain_unknown_and_cannot_fit_finite_limits():
    charge = project_attempt_charge(
        _reservation(max_input_tokens=None, max_cost_minor=None), None, repair=False
    )
    assert charge.charged.input_tokens is None and charge.charged.cost_minor is None
    assert not charge.charged.fits_within(_reservation())
    assert charge.charged.fits_within(_reservation(max_input_tokens=None, max_cost_minor=None))


def test_measured_overage_is_retained_instead_of_clamped_or_discarded():
    charge = project_attempt_charge(
        _reservation(),
        AttemptTelemetry(
            duration_ms=11_200,
            tool_call_count=9,
            named_check_count=3,
            input_tokens=101,
            output_tokens=41,
            estimated_api_cost_minor=21,
        ),
        repair=False,
    )
    assert charge.charged.duration_ms == 11_200
    assert charge.charged.input_tokens == 101
    assert set(charge.exceeded_fields) == {
        "duration_ms",
        "tool_calls",
        "named_checks",
        "input_tokens",
        "output_tokens",
        "cost_minor",
    }
    assert not charge.charged.fits_within(_reservation())


def test_aggregate_usage_preserves_unknowns_and_exact_capacity_boundary():
    first = UsageAmounts(duration_ms=1250, input_tokens=40, provider_attempts=1)
    second = UsageAmounts(duration_ms=8750, input_tokens=60, provider_attempts=1)
    total = first + second
    assert total.duration_ms == 10_000 and total.input_tokens == 100
    assert total.fits_within(_reservation(max_provider_attempts=2))
    assert not (total + UsageAmounts(duration_ms=1)).fits_within(
        _reservation(max_provider_attempts=2)
    )
    assert (total + UsageAmounts(input_tokens=None)).input_tokens is None


@pytest.mark.parametrize("value", [True, -1, 1.0, "1"])
def test_usage_counters_reject_noninteger_or_negative_values(value):
    with pytest.raises(ValueError):
        UsageAmounts(provider_attempts=value)


@pytest.mark.parametrize(
    "budget,repair",
    [
        (_reservation(max_provider_attempts=0), False),
        (_reservation(max_provider_attempts=2), False),
        (_reservation(max_repairs=1), False),
        (_reservation(), True),
    ],
)
def test_charge_requires_one_admitted_attempt_and_exact_repair_reservation(budget, repair):
    with pytest.raises(ValueError):
        project_attempt_charge(budget, None, repair=repair)
