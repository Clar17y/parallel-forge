from __future__ import annotations

import pytest
from forge.domain.run import RunState
from forge.domain.run_stall import RunStallFacts, run_stall_reason


def facts(**values: object) -> RunStallFacts:
    base: dict[str, object] = {
        "state": RunState.PLANNING,
        "execution_state": None,
        "execution_blocker": None,
        "primary_task_state": "leased",
        "primary_failure": None,
        "reconciling_tasks": 0,
        "active_tasks": 0,
    }
    base.update(values)
    return RunStallFacts(**base)  # type: ignore[arg-type]


def test_active_work_has_no_stall_reason() -> None:
    assert run_stall_reason(facts()) is None
    assert run_stall_reason(facts(state=RunState.CANCELLED, primary_task_state="terminal")) is None


@pytest.mark.parametrize("execution_state", ["BLOCKED", "RESUME_REQUESTED", "PAUSED"])
def test_queued_epic_child_explains_inactive_execution(execution_state: str) -> None:
    reason = run_stall_reason(
        facts(
            primary_task_state="queued",
            execution_state=execution_state,
            execution_blocker="uncontrolled_child" if execution_state == "BLOCKED" else None,
        )
    )
    assert reason is not None
    assert "epic execution" in reason
    assert execution_state.lower().replace("_", " ") in reason
    if execution_state == "BLOCKED":
        assert "uncontrolled_child" in reason


def test_planning_run_whose_primary_ended_says_so_with_the_failure() -> None:
    reason = run_stall_reason(
        facts(primary_task_state="terminal", primary_failure="Muse terminal failed: outage")
    )
    assert reason is not None
    assert "ended without a plan" in reason
    assert "Muse terminal failed: outage" in reason


def test_reconciling_task_is_reported() -> None:
    reason = run_stall_reason(facts(primary_task_state="reconciling", reconciling_tasks=1))
    assert reason is not None
    assert "reconcil" in reason


@pytest.mark.parametrize("state", [RunState.IMPLEMENTING, RunState.VALIDATING, RunState.REVIEWING])
def test_work_phase_whose_primary_failed_with_nothing_running_is_reported(state: RunState) -> None:
    reason = run_stall_reason(
        facts(state=state, primary_task_state="terminal", primary_failure="Muse attempt protocol")
    )
    assert reason is not None
    assert "Muse attempt protocol" in reason and "nothing is running" in reason


def test_successful_or_still_active_work_is_not_reported_as_stalled() -> None:
    assert (
        run_stall_reason(facts(state=RunState.IMPLEMENTING, primary_task_state="terminal")) is None
    )
    assert (
        run_stall_reason(
            facts(
                state=RunState.IMPLEMENTING,
                primary_task_state="terminal",
                primary_failure="Muse attempt protocol",
                active_tasks=1,
            )
        )
        is None
    )
    assert (
        run_stall_reason(
            facts(
                state=RunState.AWAITING_PR_APPROVAL,
                primary_task_state="terminal",
                primary_failure="x",
            )
        )
        is None
    )
