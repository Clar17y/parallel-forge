"""Read-only traversal of exact receipt-bound stage continuations."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.resume_source import historical_resume_source, resume_source
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent


async def resumed_successor(
    work: UnitOfWork, events: Sequence[RunEvent], source: CommandEnvelope
) -> CommandEnvelope:
    """Follow exact resume successors of this stage, never an unrelated latest result."""
    while True:
        successors = []
        for event in events:
            continuation = event.payload.get("continuation")
            if (
                event.event_type == "run.resumed"
                and isinstance(continuation, Mapping)
                and continuation.get("source_command_id") == str(source.id)
            ):
                successors.append(continuation)
        if not successors:
            return source
        if len(successors) != 1:
            raise CommandRecoveryRequired("stage has ambiguous resume successors")
        try:
            queued = await work.commands.get(UUID(str(successors[0].get("command_id"))))
        except ValueError:
            raise CommandRecoveryRequired("stage resume successor is invalid") from None
        prior = (
            await resume_source(work, queued)
            if queued.status is CommandStatus.LEASED
            else await historical_resume_source(work, queued)
        )
        if prior != source:
            raise CommandRecoveryRequired("stage resume source differs")
        # The shared verifier proves strictly increasing versions and all stop,
        # resume and command receipts, including unchanged phase inputs.
        source = queued
