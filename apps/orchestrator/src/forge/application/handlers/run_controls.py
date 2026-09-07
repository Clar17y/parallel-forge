"""Durable worker handlers for operator pause and cancel commands."""

from __future__ import annotations

from collections.abc import Mapping

from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunState
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
    )


__all__ = ["CancelRunHandler", "ControlCommandRejected", "PauseRunHandler"]
