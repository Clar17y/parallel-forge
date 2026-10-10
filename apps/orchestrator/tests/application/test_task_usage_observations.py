"""Source counters are projected without turning unknown values into zero."""

from dataclasses import replace

import pytest
from forge.application.services.task_usage_observations import project_source
from forge.domain.task_usage_contract import MAX_SIGNED_BIGINT, CounterKind, TokenCounts

from apps.orchestrator.tests.task_usage_contract.fixtures import examples


def test_final_replaces_live_and_keeps_disagreement() -> None:
    fixture = examples()
    live = replace(
        fixture["live"],
        tokens=TokenCounts(input_tokens=100, output_tokens=20),
        unknown_dimensions=(),
    )
    final = replace(fixture["final"], tokens=TokenCounts(input_tokens=90, output_tokens=18))
    projected = project_source((live, final))
    assert projected.input_tokens == 90
    assert projected.output_tokens == 18
    assert projected.final
    assert "final_below_live" in projected.disagreements


def test_delta_replay_and_unknown_dimension() -> None:
    fixture = examples()
    delta = fixture["delta"]
    unknown = replace(
        delta,
        event_id="unknown",
        sequence=2,
        tokens=TokenCounts(input_tokens=None, output_tokens=1),
        unknown_dimensions=("input_tokens",),
    )
    projected = project_source((unknown, delta))
    assert projected.input_tokens == 3
    assert projected.output_tokens == 3
    assert projected.completeness == "lower_bound"


def test_cumulative_decrement_requires_new_epoch() -> None:
    fixture = examples()
    first = replace(fixture["live"], tokens=TokenCounts(input_tokens=10))
    later = replace(first, event_id="later", sequence=2, tokens=TokenCounts(input_tokens=9))
    with pytest.raises(ValueError, match="decrement"):
        project_source((first, later))


def test_final_can_follow_delta_evidence() -> None:
    fixture = examples()
    delta = fixture["delta"]
    final = replace(
        delta,
        event_id="final",
        sequence=2,
        counter_kind=CounterKind.CUMULATIVE,
        tokens=TokenCounts(input_tokens=2, output_tokens=1),
        final=True,
        final_status="cancelled",
    )
    projected = project_source((delta, final))
    assert (projected.input_tokens, projected.output_tokens) == (2, 1)
    assert projected.final_status == "cancelled"


def test_explicit_new_epoch_resets_cumulative_counter() -> None:
    fixture = examples()
    first = replace(
        fixture["live"],
        counter_epoch="turn-1",
        tokens=TokenCounts(input_tokens=10, output_tokens=2),
        unknown_dimensions=(),
    )
    second = replace(
        first,
        event_id="turn-2",
        sequence=2,
        counter_epoch="turn-2",
        tokens=TokenCounts(input_tokens=4, output_tokens=1),
    )
    projected = project_source((first, second))
    assert (projected.input_tokens, projected.output_tokens) == (14, 3)
    assert projected.completeness == "complete"


def test_measured_zero_and_unknown_are_distinct() -> None:
    fixture = examples()
    measured_zero = fixture["live"]
    projected = project_source((measured_zero,))
    assert projected.input_tokens == 0
    assert projected.output_tokens is None
    assert projected.completeness == "lower_bound"
    final = replace(
        fixture["final"],
        tokens=TokenCounts(input_tokens=0, output_tokens=0),
        context=None,
    )
    replacement = project_source((measured_zero, final))
    assert (replacement.input_tokens, replacement.output_tokens) == (0, 0)
    assert replacement.completeness == "complete"

    unknown_delta = replace(
        fixture["delta"],
        tokens=TokenCounts(output_tokens=0),
        unknown_dimensions=("input_tokens",),
    )
    delta_projection = project_source((unknown_delta,))
    assert delta_projection.input_tokens is None
    assert delta_projection.output_tokens == 0
    assert delta_projection.completeness == "lower_bound"


def test_cached_and_reasoning_are_subsets_and_total_overflow_fails() -> None:
    fixture = examples()
    live = replace(
        fixture["live"],
        tokens=TokenCounts(
            input_tokens=MAX_SIGNED_BIGINT,
            output_tokens=1,
            cached_input_tokens=2,
            reasoning_output_tokens=1,
        ),
        unknown_dimensions=(),
    )
    projected = project_source((live,))
    assert projected.cached_input_tokens == 2
    assert projected.reasoning_output_tokens == 1
    assert projected.input_tokens == MAX_SIGNED_BIGINT
    with pytest.raises(ValueError, match="signed-64-bit"):
        _ = projected.known_total_tokens


def test_competing_finals_use_highest_sequence_and_retain_disagreement() -> None:
    fixture = examples()
    first = fixture["final"]
    later = replace(
        first,
        event_id="revised-final",
        sequence=3,
        tokens=TokenCounts(input_tokens=17, output_tokens=2),
        reconciles_event_id="final",
    )
    late_live = replace(
        fixture["live"],
        event_id="late-live",
        sequence=4,
        tokens=TokenCounts(input_tokens=25, output_tokens=None),
    )
    projected = project_source((late_live, later, first))
    assert (projected.input_tokens, projected.output_tokens) == (17, 2)
    assert projected.event_id == "revised-final"
    assert projected.disagreements == ("final_below_live", "multiple_finals")
