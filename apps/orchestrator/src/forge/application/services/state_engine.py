"""Deterministic application service for run-state transitions."""

from __future__ import annotations

from collections.abc import Mapping

from forge.domain.errors import InvalidTransition
from forge.domain.run import RunSnapshot, RunState, SuspensionKind

LEGAL: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.PLANNING, RunState.CANCELLED}),
    RunState.PLANNING: frozenset(
        {
            RunState.AWAITING_PLAN_APPROVAL,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_PLAN_APPROVAL: frozenset(
        {RunState.PLANNING, RunState.PREPARING_WORKTREE, RunState.CANCELLED}
    ),
    RunState.PREPARING_WORKTREE: frozenset(
        {
            RunState.IMPLEMENTING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.IMPLEMENTING: frozenset(
        {
            RunState.VALIDATING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.VALIDATING: frozenset(
        {
            RunState.REVIEWING,
            RunState.REMEDIATING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.REVIEWING: frozenset(
        {
            RunState.REMEDIATING,
            RunState.AWAITING_PR_APPROVAL,
            RunState.MONITORING_PR,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.REMEDIATING: frozenset(
        {
            RunState.VALIDATING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_PR_APPROVAL: frozenset(
        {RunState.REMEDIATING, RunState.PUBLISHING_PR, RunState.CANCELLED}
    ),
    RunState.PUBLISHING_PR: frozenset(
        {
            RunState.MONITORING_PR,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.MONITORING_PR: frozenset(
        {
            RunState.REMEDIATING,
            RunState.AWAITING_MERGE_APPROVAL,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_HUMAN_INTERVENTION: frozenset({RunState.CANCELLED, RunState.FAILED}),
    RunState.AWAITING_MERGE_APPROVAL: frozenset(
        {
            RunState.MONITORING_PR,
            RunState.MERGING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.CANCELLED,
        }
    ),
    RunState.MERGING: frozenset(
        {
            RunState.COMPLETED,
            RunState.MONITORING_PR,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
        }
    ),
    RunState.PAUSED: frozenset({RunState.CANCELLED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

# A descriptive alias keeps callers from depending on an implementation name.
LEGAL_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = LEGAL

_TERMINAL_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})


class StateEngine:
    """Validate and apply one deterministic state operation at a time."""

    def transition(self, run: RunSnapshot, target: RunState) -> RunSnapshot:
        """Apply a normal transition if it is listed in :data:`LEGAL`."""

        target = self._coerce_state(target, run.state)
        if target not in LEGAL[run.state]:
            raise InvalidTransition(run.state, target)
        return run.with_state(target)

    def pause(self, run: RunSnapshot) -> RunSnapshot:
        """Pause a nonterminal run while retaining its exact active state."""

        if run.state in _TERMINAL_STATES or run.state is RunState.PAUSED:
            raise InvalidTransition(
                run.state,
                RunState.PAUSED,
                reason="only an active, non-paused run can be paused",
            )
        return run.with_state(
            RunState.PAUSED,
            suspended_state=run.state,
            suspension_kind=SuspensionKind.PAUSE,
        )

    def resume(self, run: RunSnapshot) -> RunSnapshot:
        """Restore the state retained by a paused run."""

        if run.state is not RunState.PAUSED:
            raise InvalidTransition(
                run.state,
                RunState.PAUSED,
                reason="resume requires a paused run",
            )
        if run.suspended_state is None or run.suspended_state is RunState.PAUSED:
            raise InvalidTransition(
                run.state,
                run.suspended_state,
                reason="paused run has no valid suspended state",
            )
        return run.with_state(run.suspended_state)

    def intervene(self, run: RunSnapshot) -> RunSnapshot:
        """Suspend an active run for a human decision."""

        if (
            run.state in _TERMINAL_STATES
            or run.state
            in {
                RunState.PAUSED,
                RunState.AWAITING_HUMAN_INTERVENTION,
            }
            or RunState.AWAITING_HUMAN_INTERVENTION not in LEGAL[run.state]
        ):
            raise InvalidTransition(
                run.state,
                RunState.AWAITING_HUMAN_INTERVENTION,
                reason="only an active run can enter intervention",
            )
        return run.with_state(
            RunState.AWAITING_HUMAN_INTERVENTION,
            suspended_state=run.state,
            suspension_kind=SuspensionKind.INTERVENTION,
        )

    def resolve_intervention(self, run: RunSnapshot, target: RunState) -> RunSnapshot:
        """Apply a target legal from the state suspended for intervention."""

        if run.state is not RunState.AWAITING_HUMAN_INTERVENTION:
            raise InvalidTransition(
                run.state,
                target,
                reason="resolve_intervention requires an intervention state",
            )
        if run.suspended_state is None:
            raise InvalidTransition(
                run.state,
                target,
                reason="intervention has no suspended state",
            )

        target = self._coerce_state(target, run.state)
        if target not in LEGAL[run.suspended_state]:
            raise InvalidTransition(run.suspended_state, target)
        return run.with_state(target)

    @staticmethod
    def _coerce_state(target: RunState, current: RunState) -> RunState:
        if isinstance(target, RunState):
            return target
        try:
            return RunState(target)
        except ValueError as error:
            raise InvalidTransition(current, reason=f"unknown target state: {target!r}") from error


__all__ = ["LEGAL", "LEGAL_TRANSITIONS", "StateEngine", "SuspensionKind"]
