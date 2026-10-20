"""Consume durable owner continuation of a fenced ordinary plan gate."""

from forge.application.handlers.run_controls import _same_delivery
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.internal_preparation import continue_pending_plan
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.domain.command import CommandEnvelope
from forge.domain.run import RunState


class ContinuePlanHandler:
    def __init__(self, validator: PlanEvidenceValidator, artifacts: ArtifactStore) -> None:
        self._validator, self._artifacts = validator, artifacts

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if (
            command.command_type != "continue_plan"
            or command.actor_id is None
            or command.payload != {}
        ):
            raise CommandRecoveryRequired("plan continuation authority is invalid")
        if not _same_delivery(command, await work.commands.assert_current_lease(command)):
            raise CommandRecoveryRequired("plan continuation lease differs")
        run = await work.runs.get_for_update(command.run_id)
        if (
            run.version == command.expected_run_version + 1
            and run.state is RunState.PREPARING_WORKTREE
        ):
            events = await work.events.list_for_version(run.id, run.version)
            if any(
                event.event_type == "run.plan_internally_prepared"
                and event.payload.get("continuation_command_id") == str(command.id)
                and event.payload.get("authorized_by") == str(command.actor_id)
                for event in events
            ):
                await ApprovedPlanLoader(self._artifacts).load(work, run.id)
                await work.commit()
                return
        if run.version != command.expected_run_version:
            raise CommandRecoveryRequired("plan continuation version is stale")
        await continue_pending_plan(
            work,
            run,
            validator=self._validator,
            authorized_by=command.actor_id,
            authorization="owner_continuation",
            continuation_command_id=command.id,
        )
        if not _same_delivery(command, await work.commands.assert_current_lease(command)):
            raise CommandRecoveryRequired("plan continuation delivery differs")
        await work.commit()
