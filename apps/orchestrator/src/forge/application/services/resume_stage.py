"""Local execution stages which may retain authority across operator resume."""

from forge.domain.run import RunState

_STAGES = {
    RunState.CREATED: ("start_planning", "plan"),
    RunState.PLANNING: ("start_planning", "plan"),
    RunState.IMPLEMENTING: ("implement", "implement"),
    RunState.REMEDIATING: ("remediate", "implement"),
    RunState.VALIDATING: ("validate", "validate"),
    RunState.REVIEWING: ("review", "review"),
}


def resume_stage(state: RunState | None, command_type: str) -> tuple[str, str] | None:
    if state is RunState.REMEDIATING and command_type == "remediate_remote":
        return command_type, "implement"
    stage = _STAGES.get(state) if state is not None else None
    return stage if stage is not None and stage[0] == command_type else None
