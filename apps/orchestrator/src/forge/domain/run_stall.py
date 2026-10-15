"""Plain-language reasons a non-terminal run is not making progress."""

from __future__ import annotations

from dataclasses import dataclass

from forge.domain.run import RunState

_TERMINAL_RUN_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})
_WORK_PHASES = {
    RunState.PLANNING: "planning",
    RunState.IMPLEMENTING: "implementation",
    RunState.VALIDATING: "validation",
    RunState.REVIEWING: "review",
    RunState.REMEDIATING: "remediation",
}


@dataclass(frozen=True, slots=True)
class RunStallFacts:
    """Durable facts read for one run; no provider or client is contacted."""

    state: RunState
    execution_state: str | None
    execution_blocker: str | None
    primary_task_state: str | None
    primary_failure: str | None
    reconciling_tasks: int
    active_tasks: int = 0


def run_stall_reason(facts: RunStallFacts) -> str | None:
    """Return why the run is waiting, or None when work is active or finished."""
    if facts.state in _TERMINAL_RUN_STATES:
        return None
    if (
        facts.primary_task_state == "queued"
        and facts.execution_state is not None
        and facts.execution_state != "ACTIVE"
    ):
        state = facts.execution_state.lower().replace("_", " ")
        blocker = f" ({facts.execution_blocker})" if facts.execution_blocker else ""
        return (
            f"Waiting for the epic execution, which is {state}{blocker}. Forge does not "
            "start child work until it is active; resume or recover it from the epic's "
            "Delivery tab."
        )
    if facts.reconciling_tasks:
        return (
            "An attempt is waiting for its tool operations to be reconciled before any "
            "new work can start. Inspect the attempt in Tasks."
        )
    phase = _WORK_PHASES.get(facts.state)
    if (
        phase is not None
        and facts.primary_task_state == "terminal"
        and facts.primary_failure
        and not facts.active_tasks
    ):
        outcome = "ended without a plan" if facts.state is RunState.PLANNING else "ended"
        return (
            f"The {phase} attempt {outcome} ({facts.primary_failure}) and nothing is "
            "running. Retry it from Tasks if offered, or cancel the run and launch it again."
        )
    return None


__all__ = ["RunStallFacts", "run_stall_reason"]
