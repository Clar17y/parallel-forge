"""Read projections enriched with the same state authority used by commands."""

from uuid import UUID

from forge.application.ports.queries import DashboardQueryPort
from forge.application.services.auth import AuthenticatedActor, AuthenticationError
from forge.application.services.runs import (
    RunCommandType,
    RunCommandValidationError,
    _validate_state,
)
from forge.domain.run import _APPROVAL_STATE_BY_GATE, RunState


class ProjectionService:
    def __init__(self, query: DashboardQueryPort) -> None:
        self._query = query

    async def summary(self) -> dict[str, object]:
        return await self._query.summary()

    async def run_projection(
        self, run_id: UUID, actor: AuthenticatedActor
    ) -> dict[str, object] | None:
        if not isinstance(actor, AuthenticatedActor) or actor.actor_class != "operator":
            raise AuthenticationError("operator required")
        result = await self._query.run_projection(run_id)
        if result is None:
            return None
        run = result["run"]
        if not isinstance(run, dict) or type(run.get("version")) is not int:
            raise ValueError("invalid run projection")
        state = RunState(run["state"])
        commands: list[dict[str, object]] = []
        for command in RunCommandType:
            try:
                _validate_state(state, command.value)
            except RunCommandValidationError:
                continue
            commands.append(
                {
                    "name": command.value,
                    "expected_run_version": run["version"],
                    "requires_feedback": command
                    in {
                        RunCommandType.REQUEST_PLAN_REVISION,
                        RunCommandType.REQUEST_CANDIDATE_CHANGES,
                        RunCommandType.REJECT_MERGE,
                    },
                }
            )
        candidate = result["candidate"]
        gate = result["next_gate"]
        for approval_gate, approval_state in _APPROVAL_STATE_BY_GATE.items():
            if state == approval_state and gate == approval_gate.value:
                if (
                    not isinstance(candidate, dict)
                    or not candidate.get("pending_evidence_digest")
                    or type(run.get("policy_version")) is not int
                ):
                    raise ValueError("invalid approval projection")
                commands.append(
                    {
                        "name": f"approve_{approval_gate.value}",
                        "expected_run_version": run["version"],
                        "requires_feedback": False,
                        "gate": approval_gate.value,
                        "evidence_digest": candidate["pending_evidence_digest"],
                        "policy_version": run["policy_version"],
                    }
                )
        if result.get("recovery_hold") is True:
            commands = [command for command in commands if command["name"] in {"pause", "cancel"}]
        result["available_commands"] = commands
        if result["next_gate"] is None:
            if state in {RunState.CREATED, RunState.PLANNING}:
                result["next_gate"] = "plan"
            elif state in {
                RunState.PREPARING_WORKTREE,
                RunState.IMPLEMENTING,
                RunState.REMEDIATING,
                RunState.VALIDATING,
                RunState.REVIEWING,
            }:
                result["next_gate"] = "pr"
            elif state in {RunState.PUBLISHING_PR, RunState.MONITORING_PR}:
                result["next_gate"] = "merge"
        return result
