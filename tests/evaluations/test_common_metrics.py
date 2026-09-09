"""Evaluation usage retains measured units and unknown price/acceptance."""

from forge.domain.evaluation import score_usage
from forge.observability.usage import UsageRecord


def test_cached_tokens_are_not_counted_twice_and_unknown_cost_stays_unknown():
    result = score_usage(UsageRecord(provider="fake", model="fake", input_tokens=20,
        cached_input_tokens=5, output_tokens=10, duration_ms=100, tool_call_count=2),
        denied_tool_calls=["write"], human_acceptance=None)
    assert result.total_tokens == 30
    assert result.cached_input_tokens == 5
    assert result.estimated_cost_minor is None and result.currency is None
    assert result.human_acceptance is None
    assert result.duration_ms == 100 and result.tool_count == 2 and result.denied_calls == 1


def test_zero_cost_and_explicit_human_rejection_are_preserved():
    result = score_usage(UsageRecord(provider="fake", model="fake", estimated_cost_minor=0,
        currency="USD", pricing_version="v1"), denied_tool_calls=[], human_acceptance=False)
    assert result.estimated_cost_minor == 0 and result.currency == "USD"
    assert result.human_acceptance is False
