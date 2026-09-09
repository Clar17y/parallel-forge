"""Fail-closed settlement of a paused command that was never admitted."""

from __future__ import annotations

from uuid import UUID

from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.controller_steps import ControllerStepUnsettledError
from forge.application.ports.executions import ExecutionUnsettledError
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.resume_stage import resume_stage
from forge.domain.command import CommandEnvelope, CommandStatus, thaw_payload
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.commands import CommandNotFound


async def settle_deferred_delivery(
    work: UnitOfWork,
    resume_command: CommandEnvelope,
    source: CommandEnvelope,
) -> CommandEnvelope:
    """Cancel one unstarted paused-stage delivery and write its zero-admission receipt.

    The caller owns the transaction, and must not replay this helper using the
    stale pending source after a successful call.  The returned cancelled
    source is the durable replay reference; later resume wiring can use it to
    continue the stage.  This helper does not commit, restore state, prepare a
    worktree, or call a provider.
    """

    _validate_resume(resume_command)
    try:
        fenced = await work.commands.assert_current_lease(resume_command)
    except CommandLeaseLost, CommandNotFound:
        raise CommandRecoveryRequired("resume command lease is no longer current") from None
    if not _same_delivery(resume_command, fenced):
        raise CommandRecoveryRequired("resume command delivery differs from its lease")

    run = await work.runs.get_for_update(resume_command.run_id)
    if run.state is not RunState.PAUSED or run.version != resume_command.expected_run_version:
        raise CommandRecoveryRequired("deferred settlement requires the exact current paused run")
    pause = await _pause_authority(work, run)
    kind, semantic_attempt = await _validate_source(work, run, source)

    cancelled = (
        await work.commands.cancel_pending_unstarted(source)
        if source.status is CommandStatus.PENDING
        else await work.commands.cancel_expired_observed_lease(
            source, reason="unadmitted expired delivery settled during resume reconciliation"
        )
    )
    if cancelled is None:
        raise CommandRecoveryRequired("unstarted delivery changed before deferred settlement")
    proof = await work.runs.prove_quiescent(run.id, exclude_command_id=resume_command.id)
    if not proof.is_quiescent:
        raise CommandRecoveryRequired("paused run has unresolved durable effects")

    receipt = _receipt(run, cancelled, pause, kind, semantic_attempt)
    events = await work.events.list_after(run.id, 0)
    matches = [
        event
        for event in events
        if event.event_type == "delivery.deferred"
        and event.payload.get("command_id") == str(cancelled.id)
    ]
    if matches:
        raise CommandRecoveryRequired("deferred delivery receipt already exists")
    await work.events.append(receipt)
    return cancelled


def _validate_resume(command: CommandEnvelope) -> None:
    if (
        command.command_type != "resume"
        or command.status is not CommandStatus.LEASED
        or command.payload_schema_version != 1
        or command.payload != {}
        or command.actor_id is None
    ):
        raise CommandRecoveryRequired("resume command authority is invalid")


def _same_delivery(command: CommandEnvelope, fenced: CommandEnvelope) -> bool:
    return (
        command.id == fenced.id
        and command.run_id == fenced.run_id
        and command.command_type == fenced.command_type
        and command.idempotency_key == fenced.idempotency_key
        and command.payload == fenced.payload
        and command.expected_run_version == fenced.expected_run_version
        and command.actor_id == fenced.actor_id
        and command.payload_schema_version == fenced.payload_schema_version
        and command.attempt == fenced.attempt
        and command.lease_owner == fenced.lease_owner
        and fenced.status is CommandStatus.LEASED
    )


async def _pause_authority(work: UnitOfWork, run: RunSnapshot) -> CommandEnvelope:
    matches = [
        event
        for event in await work.events.list_for_version(run.id, run.version)
        if event.event_type == "run.paused"
    ]
    if len(matches) != 1:
        raise CommandRecoveryRequired("paused run has no unique causal pause event")
    event = matches[0]
    if event.actor_class != "operator" or event.payload_schema_version != 1:
        raise CommandRecoveryRequired("causal pause event authority is invalid")
    try:
        pause = await work.commands.get(UUID(str(event.payload.get("command_id"))))
    except CommandNotFound, TypeError, ValueError:
        raise CommandRecoveryRequired("causal pause command is missing") from None
    expected_payload = {
        "command_id": str(pause.id),
        "command_type": "pause",
        "command_payload": {},
        "expected_run_version": run.version - 1,
    }
    if (
        pause.run_id != run.id
        or pause.command_type != "pause"
        or pause.status is not CommandStatus.COMPLETED
        or pause.actor_id is None
        or pause.actor_id != event.actor_id
        or pause.payload_schema_version != 1
        or pause.payload != {}
        or pause.expected_run_version != run.version - 1
        or event.payload != expected_payload
    ):
        raise CommandRecoveryRequired("causal pause command binding is invalid")
    return pause


async def _validate_source(
    work: UnitOfWork, run: RunSnapshot, source: CommandEnvelope
) -> tuple[str, int]:
    stage = resume_stage(run.suspended_state, source.command_type)
    semantic_attempt = source.payload.get("semantic_attempt", 1)
    if (
        stage is None
        or source.run_id != run.id
        or source.command_type != stage[0]
        or source.status not in {CommandStatus.PENDING, CommandStatus.LEASED}
        or source.attempt < 0
        or (source.status is CommandStatus.PENDING and source.attempt != 0)
        or (
            source.status is CommandStatus.PENDING
            and (source.lease_owner is not None or source.lease_expires_at is not None)
        )
        or source.payload_schema_version != 1
        or source.expected_run_version != run.version - 1
        or type(semantic_attempt) is not int
        or semantic_attempt < 1
    ):
        raise CommandRecoveryRequired("unstarted paused delivery authority is invalid")
    try:
        next_attempt = (
            await work.controller_steps.next_attempt(run.id, stage[1])
            if stage[1] == "validate"
            else await work.executions.next_attempt(run.id, stage[1])
        )
    except ControllerStepUnsettledError, ExecutionUnsettledError:
        raise CommandRecoveryRequired("unstarted delivery has an unsettled prior attempt") from None
    if next_attempt != semantic_attempt:
        raise CommandRecoveryRequired("unstarted delivery semantic attempt was already admitted")
    return stage[1], semantic_attempt


def _receipt(
    run: RunSnapshot,
    source: CommandEnvelope,
    pause: CommandEnvelope,
    kind: str,
    semantic_attempt: int,
) -> RunEvent:
    if run.suspended_state is None:
        raise CommandRecoveryRequired("deferred delivery has no paused phase")
    return RunEvent(
        run_id=run.id,
        run_version=run.version,
        event_type="delivery.deferred",
        actor_class="worker",
        payload={
            "command_id": str(source.id),
            "command_type": source.command_type,
            "idempotency_key": source.idempotency_key,
            "command_payload": thaw_payload(source.payload),
            "expected_run_version": source.expected_run_version,
            "actor_id": str(source.actor_id) if source.actor_id is not None else None,
            "payload_schema_version": source.payload_schema_version,
            "delivery_attempt": source.attempt,
            "kind": kind,
            "semantic_attempt": semantic_attempt,
            "deferred_state": run.suspended_state.value,
            "pause_command_id": str(pause.id),
        },
    )


__all__ = ["settle_deferred_delivery"]
