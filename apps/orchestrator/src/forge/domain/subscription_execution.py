"""Shared run-control fence for fresh subscription attempts."""

from forge.domain.run import RunState

SUBSCRIPTION_WORK_STATES = frozenset({RunState.IMPLEMENTING, RunState.REMEDIATING})

_BLOCKED = frozenset(
    {
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.COMPLETED,
        RunState.AWAITING_PLAN_APPROVAL,
        RunState.AWAITING_PR_APPROVAL,
        RunState.AWAITING_HUMAN_INTERVENTION,
        RunState.AWAITING_MERGE_APPROVAL,
    }
)


def run_allows_subscription_attempt(state: str, pending_gate: str | None) -> bool:
    """Share control-state eligibility; role/stage authority is checked separately."""
    return pending_gate is None and state in RunState and state not in _BLOCKED
