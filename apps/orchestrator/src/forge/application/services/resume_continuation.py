"""Queue one fresh execution for a reconciled local stage in the resume transaction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.failed_resume import validate_failed_receipt
from forge.application.services.resume_source import RESUME_FIELDS
from forge.application.services.resume_stage import resume_stage
from forge.application.services.state_engine import StateEngine
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunSnapshot, RunState


async def enqueue_resumed_stage(
    work: UnitOfWork,
    resume: CommandEnvelope,
    paused: RunSnapshot,
    sources: Sequence[CommandEnvelope],
) -> CommandEnvelope:
    """Preserve source inputs and counters; the caller restores state and commits.

    Sources must have passed ResumeReconciler. This helper creates no provider
    execution; ordinary stage admission will revalidate the current inputs.
    """
    current = await work.commands.assert_current_lease(resume)
    if (
        replace(current, lease_expires_at=resume.lease_expires_at) != resume
        or resume.command_type != "resume"
        or resume.payload_schema_version != 1
        or resume.payload != {}
        or resume.actor_id is None
        or paused.state is not RunState.PAUSED
        or paused.id != resume.run_id
        or paused.version != resume.expected_run_version
        or await work.runs.get_for_update(paused.id) != paused
    ):
        raise CommandRecoveryRequired("resume continuation authority changed")
    restored = StateEngine().resume(paused)
    if len(sources) != 1:
        raise CommandRecoveryRequired("resume has no unique reconciled local stage")
    source = sources[0]
    stage = resume_stage(restored.state, source.command_type)
    if stage is None:
        raise CommandRecoveryRequired("resume has no supported local stage")
    command_type, kind = stage
    if (
        source.run_id != paused.id
        or source.command_type != command_type
        or source.status
        not in {CommandStatus.COMPLETED, CommandStatus.CANCELLED, CommandStatus.FAILED}
        or source.payload_schema_version != 1
        or await work.commands.get(source.id) != source
    ):
        raise CommandRecoveryRequired("resume stage source differs from durable evidence")
    failed_unadmitted = False
    if source.status is CommandStatus.FAILED:
        events = [
            event
            for event in await work.events.list_for_version(paused.id, paused.version)
            if event.event_type == "run.paused"
        ]
        if len(events) != 1:
            raise CommandRecoveryRequired("failed continuation has no causal pause")
        try:
            pause_id = UUID(str(events[0].payload.get("command_id")))
        except ValueError:
            raise CommandRecoveryRequired("failed continuation pause is invalid") from None
        failed_unadmitted = any(
            event.event_type == "delivery.failed_before_admission"
            and event.payload.get("command_id") == str(source.id)
            for event in await work.events.list_for_version(paused.id, paused.version)
        )
        if failed_unadmitted:
            await validate_failed_receipt(
                work, source, paused_version=paused.version, pause_id=pause_id, state=restored.state
            )
    attempt = (
        await work.controller_steps.next_attempt(paused.id, kind)
        if kind == "validate"
        else await work.executions.next_attempt(paused.id, kind)
    )
    previous = source.payload.get("semantic_attempt", 1)
    receipts = [
        event
        for event in await work.events.list_for_version(paused.id, paused.version)
        if event.payload.get("command_id") == str(source.id)
    ]
    deferred = any(event.event_type == "delivery.deferred" for event in receipts)
    if type(previous) is not int or attempt != previous + (
        0 if deferred or failed_unadmitted else 1
    ):
        raise CommandRecoveryRequired("resume stage attempt is not next")
    payload = {key: value for key, value in source.payload.items() if key not in RESUME_FIELDS}
    payload.update(
        semantic_attempt=attempt,
        resume_command_id=str(resume.id),
        source_command_id=str(source.id),
    )
    return await work.commands.enqueue(
        run_id=paused.id,
        command_type=command_type,
        idempotency_key=f"{paused.id}:resume:{resume.id}:{command_type}:{attempt}",
        payload=payload,
        expected_run_version=restored.version,
        actor_id=source.actor_id,
    )


__all__ = ["enqueue_resumed_stage"]
