"""Shared persisted publication proof for delivery replay and paused recovery."""

from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.command import CommandEnvelope
from forge.domain.run import RunState


async def verify_publication_replay(
    command: CommandEnvelope, work: UnitOfWork, approval_id: UUID, *, paused: bool = False
) -> bool:
    run = await work.runs.get_for_update(command.run_id)
    events = [
        event
        for event in await work.events.list_after(run.id, 0)
        if event.event_type == "run.pr_published"
        and event.payload.get("source_command_id") == str(command.id)
    ]
    if not events:
        return False
    recorded = await work.releases.get_for_run(run.id)
    queued = await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:1")
    if (
        len(events) != 1
        or recorded is None
        or queued is None
        or (
            (
                run.state is not RunState.PAUSED
                or run.suspended_state is not RunState.MONITORING_PR
                or run.version != command.expected_run_version + 2
            )
            if paused
            else (
                run.state is not RunState.MONITORING_PR
                or run.version != command.expected_run_version + 1
            )
        )
        or events[0].run_version != command.expected_run_version + 1
        or events[0].actor_class != "worker"
        or events[0].actor_id != command.actor_id
        or events[0].payload
        != {
            "source_command_id": str(command.id),
            "approval_id": str(approval_id),
            "pull_request_id": str(recorded.id),
            "node_id": recorded.pull_request.node_id,
            "push_intent_id": str(recorded.push_intent_id),
            "publication_intent_id": str(recorded.publication_intent_id),
            "monitor_command_id": str(queued.id),
        }
        or queued.command_type != "monitor_pr"
        or queued.actor_id != command.actor_id
        or queued.expected_run_version != events[0].run_version
        or queued.payload != {"pull_request_id": str(recorded.id), "poll": 1}
    ):
        raise CommandRecoveryRequired("publication replay differs")
    return True
