"""Durable worker handlers for operator pause and cancel commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from uuid import UUID

from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.state_engine import StateEngine
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.commands import CommandNotFound


class ControlCommandRejected(RuntimeError):
    """A current delivery conclusively lacks authority for this run version."""


class PauseRunHandler:
    """Fence and apply one operator-authorized pause transition."""

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await _execute(command, work, target=RunState.PAUSED)


class CancelRunHandler:
    """Fence and apply one operator-authorized cancellation transition."""

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await _execute(command, work, target=RunState.CANCELLED)


class ResumeRunHandler:
    """Restore a paused approval/intervention run after proving quiescence."""

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        _validate_resume_command(command)
        try:
            fenced = await work.commands.assert_current_lease(command)
        except CommandLeaseLost, CommandNotFound:
            raise CommandRecoveryRequired("resume command lease is no longer current") from None
        if not _same_delivery(command, fenced):
            raise CommandRecoveryRequired("resume command delivery differs from its lease")

        run = await work.runs.get_for_update(command.run_id)
        if run.state is not RunState.PAUSED:
            resumed = await _load_resume_event(work, command)
            expected_state = resumed[0] if resumed is not None else None
            pause_authority = (
                await _validate_pause_authority_at_version(
                    work, command.run_id, command.expected_run_version
                )
                if resumed is not None
                else None
            )
            if (
                expected_state is None
                or pause_authority is None
                or run.version != command.expected_run_version + 1
                or run.state is not expected_state
                or not await _replayed(
                    work,
                    command,
                    run.version,
                    "run.resumed",
                    _resume_payload(command, expected_state, pause_authority.id, run=run),
                )
            ):
                raise CommandRecoveryRequired("resume replay requires recovery")
            await work.commit()
            return

        if run.version != command.expected_run_version:
            raise ControlCommandRejected("resume command version is stale")
        target = _restored_state(run)
        if target not in {
            RunState.AWAITING_PLAN_APPROVAL,
            RunState.AWAITING_PR_APPROVAL,
            RunState.AWAITING_MERGE_APPROVAL,
            RunState.AWAITING_HUMAN_INTERVENTION,
        }:
            raise CommandRecoveryRequired("paused active phase requires recovery")

        pause_command = await _validate_pause_authority(work, run)
        proof = await work.runs.prove_quiescent(run.id, exclude_command_id=command.id)
        if not proof.is_quiescent:
            raise CommandRecoveryRequired("paused run has unsettled durable work")

        await work.runs.resume(
            run.id,
            run.version,
            "run.resumed",
            _resume_payload(command, target, pause_command.id, run=run),
            actor_class="operator",
            actor_id=command.actor_id,
        )
        await work.commit()


async def _execute(command: CommandEnvelope, work: UnitOfWork, *, target: RunState) -> None:
    _validate_command(command, target)
    try:
        fenced = await work.commands.assert_current_lease(command)
    except CommandLeaseLost, CommandNotFound:
        raise CommandRecoveryRequired("control command lease is no longer current") from None
    if not _same_delivery(command, fenced):
        raise CommandRecoveryRequired("control command delivery differs from its lease")
    run = await work.runs.get_for_update(command.run_id)
    payload = _event_payload(command)
    event_type = "run.paused" if target is RunState.PAUSED else "run.cancelled"

    if run.state is target:
        if run.version != command.expected_run_version + 1 or not await _replayed(
            work, command, run.version, event_type, payload
        ):
            raise CommandRecoveryRequired("control command replay requires recovery")
        await work.commit()
        return
    if run.version != command.expected_run_version:
        raise ControlCommandRejected("control command version is stale")
    if target is RunState.PAUSED:
        await work.runs.pause(
            run.id,
            run.version,
            event_type,
            payload,
            actor_class="operator",
            actor_id=command.actor_id,
        )
    else:
        await work.runs.transition(
            run.id,
            run.version,
            RunState.CANCELLED,
            event_type,
            payload,
            actor_class="operator",
            actor_id=command.actor_id,
        )
    await work.commit()


def _validate_command(command: CommandEnvelope, target: RunState) -> None:
    wanted = "pause" if target is RunState.PAUSED else "cancel"
    if (
        not isinstance(command, CommandEnvelope)
        or command.command_type != wanted
        or command.status is not CommandStatus.LEASED
        or command.payload_schema_version != 1
        or command.payload != {}
        or command.actor_id is None
    ):
        raise CommandRecoveryRequired("control command authority is invalid")


def _validate_resume_command(command: CommandEnvelope) -> None:
    if (
        not isinstance(command, CommandEnvelope)
        or command.command_type != "resume"
        or command.status is not CommandStatus.LEASED
        or command.payload_schema_version != 1
        or command.payload != {}
        or command.actor_id is None
    ):
        raise CommandRecoveryRequired("resume command authority is invalid")


def _same_delivery(command: CommandEnvelope, fenced: CommandEnvelope) -> bool:
    """Accept a lease renewal while rejecting substituted command authority."""

    return (
        fenced.id == command.id
        and fenced.run_id == command.run_id
        and fenced.command_type == command.command_type
        and fenced.idempotency_key == command.idempotency_key
        and fenced.payload == command.payload
        and fenced.status is CommandStatus.LEASED
        and fenced.expected_run_version == command.expected_run_version
        and fenced.actor_id == command.actor_id
        and fenced.payload_schema_version == command.payload_schema_version
        and fenced.attempt == command.attempt
        and fenced.lease_owner == command.lease_owner
    )


def _event_payload(command: CommandEnvelope) -> dict[str, object]:
    return {
        "command_id": str(command.id),
        "command_type": command.command_type,
        "command_payload": dict(command.payload),
        "expected_run_version": command.expected_run_version,
    }


def _restored_state(run: RunSnapshot) -> RunState | None:
    context = run.suspension_context
    target = context.state if context is not None else run.suspended_state
    return target if isinstance(target, RunState) else None


async def _validate_pause_authority(work: UnitOfWork, run: RunSnapshot) -> CommandEnvelope:
    return await _validate_pause_authority_at_version(work, run.id, run.version)


async def _validate_pause_authority_at_version(
    work: UnitOfWork, run_id: UUID, paused_version: int
) -> CommandEnvelope:
    events = [
        event
        for event in await work.events.list_for_version(run_id, paused_version)
        if event.event_type == "run.paused"
    ]
    if len(events) != 1:
        raise CommandRecoveryRequired("paused run has no unique causal pause event")
    event = events[0]
    if event.actor_class != "operator" or event.payload_schema_version != 1:
        raise CommandRecoveryRequired("causal pause event authority is invalid")
    payload = event.payload
    raw_id = payload.get("command_id")
    try:
        pause_id = UUID(str(raw_id))
    except AttributeError, TypeError, ValueError:
        raise CommandRecoveryRequired("causal pause command identifier is invalid") from None
    try:
        pause = await work.commands.get(pause_id)
    except CommandNotFound:
        raise CommandRecoveryRequired("causal pause command is missing") from None
    if (
        pause.run_id != run_id
        or pause.command_type != "pause"
        or pause.status is not CommandStatus.COMPLETED
        or pause.actor_id is None
        or pause.actor_id != event.actor_id
        or pause.payload_schema_version != 1
        or pause.payload != {}
        or pause.expected_run_version != paused_version - 1
        or payload != _event_payload(pause)
    ):
        raise CommandRecoveryRequired("causal pause command binding is invalid")
    return pause


def _resume_payload(
    command: CommandEnvelope,
    target: RunState,
    pause_command_id: UUID,
    *,
    run: RunSnapshot,
) -> dict[str, object]:
    restored = StateEngine().resume(run) if run.state is RunState.PAUSED else run
    return {
        "command_id": str(command.id),
        "command_type": "resume",
        "command_payload": {},
        "expected_run_version": command.expected_run_version,
        "paused_version": command.expected_run_version,
        "pause_command_id": str(pause_command_id),
        "restored_state": target.value,
        "restored_snapshot_digest": hashlib.sha256(
            json.dumps(
                asdict(restored), default=str, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }


async def _load_resume_event(
    work: UnitOfWork, command: CommandEnvelope
) -> tuple[RunState, UUID] | None:
    events = [
        event
        for event in await work.events.list_for_version(
            command.run_id, command.expected_run_version + 1
        )
        if event.event_type == "run.resumed"
    ]
    if (
        len(events) != 1
        or events[0].actor_class != "operator"
        or events[0].actor_id != command.actor_id
    ):
        return None
    payload = events[0].payload
    raw = payload.get("restored_state")
    pause_id = payload.get("pause_command_id")
    try:
        return RunState(raw), UUID(str(pause_id))  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


async def _replayed(
    work: UnitOfWork,
    command: CommandEnvelope,
    version: int,
    event_type: str,
    payload: Mapping[str, object],
) -> bool:
    events = [
        event
        for event in await work.events.list_for_version(command.run_id, version)
        if event.event_type == event_type
    ]
    return (
        len(events) == 1
        and events[0].payload == payload
        and events[0].actor_class == "operator"
        and events[0].actor_id == command.actor_id
        and events[0].payload_schema_version == 1
    )


__all__ = [
    "CancelRunHandler",
    "ControlCommandRejected",
    "PauseRunHandler",
    "ResumeRunHandler",
]
