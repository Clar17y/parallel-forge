"""Persisted human authority shared by merge delivery and terminal recovery."""

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.release_resume import resumed_release_origin
from forge.domain.command import CommandEnvelope
from forge.domain.run import RunState
from forge.persistence.models import Approval


async def verify_merge_delivery(
    command: CommandEnvelope, work: UnitOfWork, approval: Approval
) -> None:
    command = await resumed_release_origin(work, command)
    events = [
        e
        for e in await work.events.list_for_version(command.run_id, command.expected_run_version)
        if e.event_type == "run.merge_approved"
        and e.actor_class == "operator"
        and e.actor_id == command.actor_id
        and e.payload.get("approval_id") == str(approval.id)
        and e.payload.get("evidence_digest") == approval.evidence_digest
        and e.payload.get("invalidated") is False
        and e.payload.get("target") == RunState.MERGING.value
        and e.payload.get("queued_command_id") == str(command.id)
        and e.payload.get("queued_key") == command.idempotency_key
        and e.payload.get("queued_type") == command.command_type
        and e.payload.get("queued_payload") == command.payload
    ]
    if len(events) != 1:
        raise CommandRecoveryRequired("merge rejection has no causal approval")
