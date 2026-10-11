"""Shared contract additions required by accounting and policy/reporting lanes."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.api.schemas.task_usage import (
    BaselineWire,
    ObservationWire,
    PolicyWire,
    TaskAllowanceInput,
)
from forge.domain.task_usage_contract import MonitoringPolicy

from .fixtures import examples


def test_counter_epoch_is_explicit_bounded_and_backwards_compatible():
    live = examples()["live"]
    assert live.counter_epoch == "source"
    reset = replace(live, counter_epoch="turn-2")
    assert ObservationWire.validate_python(ObservationWire.dump_python(reset, mode="json")) == reset
    for invalid in ("", " " * 3, "x" * 97, True, 2):
        with pytest.raises((ValueError, TypeError)):
            replace(live, counter_epoch=invalid)


def test_manual_estimate_and_cohort_policy_round_trip_signed64():
    policy = MonitoringPolicy(
        project_id=uuid4(),
        manual_estimate_tokens=2**63 - 1,
        allow_cross_project_fallback=False,
    )
    assert PolicyWire.validate_python(PolicyWire.dump_python(policy, mode="json")) == policy
    for invalid in (0, -1, True, 2**63):
        with pytest.raises((ValueError, TypeError)):
            replace(policy, manual_estimate_tokens=invalid)
    with pytest.raises((ValueError, TypeError)):
        replace(policy, allow_cross_project_fallback=1)
    default = MonitoringPolicy(project_id=uuid4())
    assert default.manual_estimate_tokens is None
    assert default.hard_token_cap is None
    assert default.allow_cross_project_fallback is True


def test_manual_allowance_wire_rejects_storage_overflow():
    with pytest.raises(ValueError):
        TaskAllowanceInput(manual_estimate_tokens=2**63)
    with pytest.raises(ValueError):
        TaskAllowanceInput(manual_estimate_context_tokens=2**63)


def test_rolling_window_is_an_optional_signed64_policy_setting():
    policy = MonitoringPolicy(project_id=uuid4())
    assert policy.window_days is None
    bounded = replace(policy, window_days=2**63 - 1)
    assert PolicyWire.validate_python(PolicyWire.dump_python(bounded, mode="json")) == bounded
    for invalid in (0, -1, True, 2**63):
        with pytest.raises((TypeError, ValueError)):
            replace(policy, window_days=invalid)


def test_manual_baseline_has_a_frozen_estimate_without_claiming_measurements():
    manual = replace(
        examples()["baseline"],
        status="manual",
        estimator_version="manual-v1",
        reference_tokens=100,
        warning_threshold_tokens=150,
        checkpoint_threshold_tokens=200,
    )
    assert manual.sample_count == 0
    assert manual.mean_tokens is None
    assert BaselineWire.validate_python(BaselineWire.dump_python(manual, mode="json")) == manual
    with pytest.raises(ValueError):
        replace(manual, sample_count=1)
    with pytest.raises(ValueError):
        replace(manual, reference_tokens=None)


def test_unmeasured_outcomes_remain_in_baseline_coverage_denominator():
    ready = examples()["ready_baseline"]
    partial_coverage = replace(ready, outcome_count=ready.sample_count + 2)
    assert partial_coverage.outcome_count > partial_coverage.sample_count
    with pytest.raises(ValueError):
        replace(ready, coverage_count=ready.sample_count + 1)


def test_history_response_has_explicit_unknown_coverage_and_strict_counters():
    from forge.api.schemas.task_usage import TaskUsageHistoryResponse

    summary = {
        "status": "insufficient_history",
        "sample_count": 0,
        "eligible_count": 0,
        "excluded_count": 1,
        "coverage_count": 0,
        "measured_coverage": None,
        "cohort_source": "unavailable",
        "project_ids": [],
        "labels": [],
        "phase_role_breakdown": [],
    }
    payload = {
        "as_of": "2026-10-10T12:00:00Z",
        "filters": {"project_ids": [], "history_limit": 50, "minimum_comparables": 20},
        "overview": summary,
        "by_task_type": {},
        "by_project": {},
        "by_provider": {},
        "by_model": {},
        "by_effort": {},
        "comparable_references": {},
        "excluded_outcomes": {"unknown": 1},
    }
    response = TaskUsageHistoryResponse.model_validate(payload)
    assert response.overview.mean_tokens is None
    assert response.overview.measured_coverage is None
    for invalid in (True, -1, 2**63):
        with pytest.raises(ValueError):
            TaskUsageHistoryResponse.model_validate(
                {**payload, "overview": {**summary, "sample_count": invalid}}
            )


def test_execution_admission_caps_are_explicit_signed64_frozen_metadata():
    from forge.application.ports.executions import ExecutionAdmission
    from forge.domain.actor import AgentRole

    admission = ExecutionAdmission(
        run_id=uuid4(),
        step_id=uuid4(),
        agent_execution_id=uuid4(),
        kind="plan",
        attempt=1,
        role=AgentRole.PLANNER,
        instruction_version="1",
        provider="test",
        model="test",
        input_artifact_id=None,
        transition_from=None,
        transition_to=None,
        admitted_at=datetime.now(UTC),
        is_new=True,
    )
    assert admission.hard_token_cap is None
    assert admission.monitoring_policy_revision is None
    bounded = replace(admission, hard_token_cap=2**63 - 1, monitoring_policy_revision=2**63 - 1)
    assert bounded.hard_token_cap == 2**63 - 1
    for field in ("hard_token_cap", "monitoring_policy_revision"):
        for invalid in (0, -1, True, 2**63):
            with pytest.raises((TypeError, ValueError)):
                replace(admission, **{field: invalid})
