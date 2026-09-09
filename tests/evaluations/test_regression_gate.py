"""Regression gates use declared thresholds and matching metric/fixture versions."""

import pytest
from forge.domain.evaluation import regression_failures


def gate(metrics, **changes):
    return regression_failures(**({
        "fixture_version": "fixture-v1", "metric_version": "metrics-v1",
        "baseline_fixture_version": "fixture-v1", "baseline_metric_version": "metrics-v1",
        "metrics": metrics, "floors": {"recall": 0.8}, "ceilings": {"cost_minor": 10},
    } | changes))


def test_threshold_boundaries_pass_and_independent_failures_are_reported():
    assert gate({"recall": 0.8, "cost_minor": 10}) == ()
    assert gate({"recall": 0.7, "cost_minor": 11}) == ("recall", "cost_minor")


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, "9", 10 ** 400])
def test_unknown_or_invalid_values_cannot_pass_a_budget(value):
    assert gate({"recall": 1, "cost_minor": value}) == ("cost_minor",)


@pytest.mark.parametrize("field", ["fixture_version", "metric_version"])
def test_incompatible_versions_are_not_compared(field):
    with pytest.raises(ValueError, match="versions differ"):
        gate({"recall": 1, "cost_minor": 0}, **{field: "new-version"})


def test_missing_metric_fails_and_invalid_threshold_is_rejected():
    assert gate({"cost_minor": 0}) == ("recall",)
    with pytest.raises(ValueError, match="finite numeric"):
        gate({}, floors={"recall": float("nan")})
