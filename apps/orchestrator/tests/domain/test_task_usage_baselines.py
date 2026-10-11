import math
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from forge.domain.task_usage_baselines import (
    BaselineSample,
    SamplingRules,
    select_cohort,
    summarize_samples,
)


def sample(tokens: int | None, offset: int, **kwargs) -> BaselineSample:
    return BaselineSample(
        project_id=kwargs.pop("project_id", uuid4()),
        subject_kind=kwargs.pop("subject_kind", "run"),
        subject_id=uuid4(),
        completed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=offset),
        total_tokens=tokens,
        outcome=kwargs.pop("outcome", "accepted"),
        finalized=kwargs.pop("finalized", True),
        measured=kwargs.pop("measured", tokens is not None),
        configured_route=kwargs.pop("configured_route", "route-a"),
        effective_route=kwargs.pop("effective_route", "route-a"),
        **kwargs,
    )


def test_statistics_use_only_finalized_accepted_measured_top_level_samples():
    project = uuid4()
    samples = [
        sample(0, 0, project_id=project),
        sample(10, 1, project_id=project),
        sample(20, 2, project_id=project),
        sample(1000, 3, project_id=project),
        sample(None, 4, project_id=project),
        sample(500, 5, project_id=project, outcome="failed"),
        sample(500, 6, project_id=project, finalized=False),
    ]
    summary = summarize_samples(
        samples,
        rules=SamplingRules(history_limit=5, minimum_comparables=2),
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
        project_ids={project},
    )
    assert summary.status == "ready"
    assert summary.sample_count == 4
    assert summary.mean_tokens == 257.5
    assert summary.median_tokens == 15
    assert summary.p90_tokens == pytest.approx(706)
    assert summary.coverage_count == 4
    assert summary.excluded_count == 2
    assert summary.coverage == 0.8
    assert summary.window_started_at == samples[0].completed_at
    assert summary.window_ended_at == samples[4].completed_at


def test_phase_role_aggregate_overflow_remains_unknown_while_totals_stay_measured():
    project = uuid4()
    maximum = 2**63 - 1
    rows = [sample(maximum, 0, project_id=project,
                   phase_role_breakdown=(("implementation", "developer", maximum),)),
            sample(1, 1, project_id=project,
                   phase_role_breakdown=(("implementation", "developer", 1),))]
    summary = summarize_samples(rows, rules=SamplingRules(history_limit=2, minimum_comparables=1),
                                as_of=datetime(2026, 2, 1, tzinfo=UTC), project_ids={project})
    assert summary.sample_count == 2 and summary.coverage_count == 2
    assert summary.phase_role_breakdown == (("implementation", "developer", None),)
    assert summary.reference_tokens is not None
    with pytest.raises(ValueError, match="phase.*signed-64"):
        sample(1, 0, phase_role_breakdown=(("implementation", "developer", maximum + 1),))


def test_exact_route_precedes_labelled_cross_project_fallback_and_access_filter():
    current_project, visible, hidden = uuid4(), uuid4(), uuid4()
    current = sample(1, 3, project_id=current_project)
    rows = [current, sample(10, 0, project_id=visible), sample(100, 1, project_id=hidden)]
    summary = summarize_samples(
        rows,
        rules=SamplingRules(history_limit=10, minimum_comparables=2),
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
        project_ids={current_project, visible},
        subject_kind="run",
        configured_route="route-a",
        effective_route="route-a",
        target_project_id=current_project,
    )
    assert summary.cohort_source == "accessible_cross_project"
    assert set(summary.project_ids) == {current_project, visible}
    assert summary.mean_tokens == 5.5
    assert "accessible_cross_project" in summary.labels


def test_sparse_history_is_report_only_and_manual_estimate_is_labelled():
    summary = summarize_samples(
        [sample(1, 0)],
        rules=SamplingRules(history_limit=50, minimum_comparables=2),
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert summary.status == "insufficient_history"
    assert summary.sample_count == 1
    assert summary.mean_tokens == 1
    assert summary.reference_tokens is None
    manual = summarize_samples(
        [],
        rules=SamplingRules(
            history_limit=50, minimum_comparables=20, manual_estimate_tokens=200_000
        ),
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert manual.status == "manual"
    assert manual.sample_count == 0 and manual.mean_tokens is None
    assert manual.reference_tokens == 200_000
    assert manual.labels == ("manual_estimate",)


def test_same_timestamp_window_ties_are_deterministic_and_rules_are_editable():
    project = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    low = BaselineSample(
        project_id=project,
        subject_kind="run",
        subject_id=UUID("00000000-0000-0000-0000-000000000001"),
        completed_at=at,
        total_tokens=10,
        outcome="completed",
        finalized=True,
        measured=True,
        configured_route="route-a",
        effective_route="route-a",
    )
    high = BaselineSample(
        **{
            **{field: getattr(low, field) for field in low.__dataclass_fields__},
            "subject_id": UUID("00000000-0000-0000-0000-000000000002"),
            "total_tokens": 30,
        }
    )
    summary = summarize_samples(
        [high, low],
        rules=SamplingRules(history_limit=1, minimum_comparables=1),
        as_of=at + timedelta(days=1),
    )
    assert summary.mean_tokens == 30
    assert summary.window_started_at == summary.window_ended_at == at


def test_signed_64_bit_window_saturates_at_supported_datetime_floor():
    at = datetime(2026, 1, 1, tzinfo=UTC)
    summary = summarize_samples(
        [sample(25, 0, project_id=uuid4())],
        rules=SamplingRules(history_limit=1, minimum_comparables=1, window_days=2**63 - 1),
        as_of=at,
    )
    assert summary.sample_count == 1


def test_member_filters_are_conjunctive_across_a_single_route_member():
    project = uuid4()
    mixed = sample(100, 0, project_id=project, route_members=(
        ("developer", "provider-a", "model-b", "medium"),
        ("reviewer", "provider-b", "model-a", "high"),
    ))
    summary = summarize_samples(
        [mixed], rules=SamplingRules(history_limit=5, minimum_comparables=1),
        as_of=datetime(2026, 2, 1, tzinfo=UTC), project_ids={project},
        provider="provider-a", model="model-a", overview=True,
    )
    assert summary.sample_count == 0


def test_selected_fallback_cohort_matches_rolling_summary():
    local, foreign = uuid4(), uuid4()
    rows = [sample(10, 0, project_id=local), sample(None, 2, project_id=local),
            sample(None, 3, project_id=local), sample(40, 4, project_id=foreign)]
    rules = SamplingRules(history_limit=2, minimum_comparables=1)
    at = datetime(2026, 2, 1, tzinfo=UTC)
    selection = select_cohort(rows, rules=rules, as_of=at, project_ids={local, foreign},
                              target_project_id=local, configured_route="route-a",
                              effective_route="route-a")
    summary = summarize_samples(rows, rules=rules, as_of=at, project_ids={local, foreign},
                                target_project_id=local, configured_route="route-a",
                                effective_route="route-a")
    assert selection.cohort_source == summary.cohort_source == "accessible_cross_project"
    assert tuple(row.subject_id for row in selection.samples) == (rows[2].subject_id, rows[3].subject_id)
    assert summary.sample_count == 1 and summary.mean_tokens == 40


def test_valid_extreme_multipliers_saturate_manual_and_ready_advisory_thresholds():
    at = datetime(2026, 2, 1, tzinfo=UTC)
    extreme = SamplingRules(
        history_limit=2, minimum_comparables=1,
        manual_estimate_tokens=2**63 - 1,
        warning_multiplier=1e308, checkpoint_multiplier=1e308,
    )
    manual = summarize_samples([], rules=extreme, as_of=at)
    ready = summarize_samples([sample(2**63 - 1, 0)], rules=extreme, as_of=at)
    for summary in (manual, ready):
        assert summary.warning_threshold_tokens == sys.float_info.max
        assert summary.checkpoint_threshold_tokens == sys.float_info.max
        assert math.isfinite(summary.reference_tokens)
    zero = summarize_samples([sample(0, 0)], rules=extreme, as_of=at)
    assert zero.warning_threshold_tokens == zero.checkpoint_threshold_tokens == 0
    ordinary = summarize_samples(
        [], rules=SamplingRules(manual_estimate_tokens=100), as_of=at,
    )
    assert (ordinary.warning_threshold_tokens, ordinary.checkpoint_threshold_tokens) == (150, 200)


def test_exact_comparable_exclusions_ignore_other_projects_routes_and_kinds():
    local, foreign, hidden = uuid4(), uuid4(), uuid4()
    rows = [
        sample(10, 0, project_id=local),
        sample(None, 1, project_id=local, outcome="failed", finalized=False),
        sample(None, 2, project_id=local, configured_route="route-b",
               outcome="failed", finalized=False),
        sample(None, 3, project_id=local, effective_route="route-b",
               outcome="failed", finalized=False),
        sample(None, 4, project_id=foreign, outcome="failed", finalized=False),
        sample(None, 5, project_id=hidden, outcome="failed", finalized=False),
        sample(None, 6, project_id=local, subject_kind="authoring_job",
               task_kind="authoring", outcome="failed", finalized=False),
        sample(None, 50, project_id=local, outcome="failed", finalized=False),
    ]
    at = datetime(2026, 2, 1, tzinfo=UTC)
    rules = SamplingRules(history_limit=1, minimum_comparables=1,
                          allow_cross_project_fallback=True)
    exact = summarize_samples(
        rows, rules=rules, as_of=at, project_ids={local, foreign},
        subject_kind="run", task_kind="implementation", target_project_id=local,
        configured_route="route-a", effective_route="route-a",
    )
    assert exact.cohort_source == "exact_project_route"
    assert exact.sample_count == 1 and exact.excluded_count == 1
    overview = summarize_samples(
        rows, rules=rules, as_of=at, project_ids={local, foreign},
        subject_kind="run", task_kind="implementation", overview=True,
    )
    assert overview.excluded_count == 4


def test_fallback_exclusions_follow_selected_accessible_route_cohort():
    local, foreign, hidden = uuid4(), uuid4(), uuid4()
    rows = [
        sample(0, 0, project_id=local),
        sample(None, 1, project_id=local, outcome="failed", finalized=False),
        sample(40, 2, project_id=foreign),
        sample(None, 3, project_id=foreign, outcome="failed", finalized=False),
        sample(None, 4, project_id=foreign, configured_route="other",
               outcome="failed", finalized=False),
        sample(None, 5, project_id=hidden, outcome="failed", finalized=False),
    ]
    at = datetime(2026, 2, 1, tzinfo=UTC)
    kwargs = {"as_of": at, "project_ids": {local, foreign}, "target_project_id": local,
              "subject_kind": "run", "configured_route": "route-a",
              "effective_route": "route-a"}
    fallback = summarize_samples(
        rows, rules=SamplingRules(history_limit=2, minimum_comparables=2), **kwargs,
    )
    assert fallback.cohort_source == "accessible_cross_project"
    assert fallback.sample_count == 2 and fallback.excluded_count == 2
    assert fallback.median_tokens == 20
    ready_exact = summarize_samples(
        rows, rules=SamplingRules(history_limit=1, minimum_comparables=1), **kwargs,
    )
    assert ready_exact.cohort_source == "exact_project_route"
    assert ready_exact.sample_count == 1 and ready_exact.excluded_count == 1
    assert ready_exact.median_tokens == 0


def test_comparable_exclusions_require_one_matching_route_member():
    local = uuid4()
    members = (("developer", "provider-a", "model-a", "medium"),)
    crossed = (("developer", "provider-a", "model-b", "medium"),
               ("reviewer", "provider-b", "model-a", "high"))
    rows = [
        sample(0, 0, project_id=local, route_members=members),
        sample(None, 1, project_id=local, route_members=members,
               outcome="failed", finalized=False),
        sample(None, 2, project_id=local, route_members=crossed,
               outcome="failed", finalized=False),
    ]
    summary = summarize_samples(
        rows, rules=SamplingRules(history_limit=1, minimum_comparables=1),
        as_of=datetime(2026, 2, 1, tzinfo=UTC), project_ids={local},
        target_project_id=local, configured_route="route-a", effective_route="route-a",
        provider="provider-a", model="model-a", effort="medium",
    )
    assert summary.sample_count == 1 and summary.excluded_count == 1
