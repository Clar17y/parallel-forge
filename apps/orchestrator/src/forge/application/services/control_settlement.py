"""Verify worker-applied control stops before settling late agent results."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from forge.application.ports.commands import CommandLeaseLost
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.state_engine import StateEngine
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState


async def controlled_stop(
    work: UnitOfWork, command: CommandEnvelope, admitted_run: RunSnapshot
) -> bool:
    """Return whether exactly verified control authority stopped this delivery."""

    try:
        fenced = await work.commands.assert_current_lease(command)
    except CommandLeaseLost:
        return False
    if not _same_normal_delivery(command, fenced):
        return False
    current = await work.runs.get_for_update(command.run_id)
    events = await work.events.list_after(command.run_id, 0)
    engine = StateEngine()
    expected = engine.pause(admitted_run)
    pause = await _control_event(work, events, "run.paused", expected, command.run_id)
    if pause is not None:
        if current == expected:
            return True
        expected = StateEngine().transition(expected, RunState.CANCELLED)
    if pause is None:
        expected = engine.transition(admitted_run, RunState.CANCELLED)
    cancel = await _control_event(work, events, "run.cancelled", expected, command.run_id)
    return cancel is not None and current == expected


async def pending_current_control_stop(work: UnitOfWork, run: RunSnapshot) -> bool:
    """Fence finalization while an exact current-version control is actionable.

    The caller obtains ``run`` with ``get_for_update`` and keeps that lock until
    commit or rollback.  An accepted control cannot become stale by a competing
    stage publication in that interval.
    """

    return await work.commands.has_pending_current_control_stop(
        run_id=run.id, expected_run_version=run.version
    )


async def _control_event(
    work: UnitOfWork,
    events: Sequence[RunEvent],
    event_type: str,
    expected: RunSnapshot,
    run_id: UUID,
) -> RunEvent | None:
    matches = [
        event
        for event in events
        if event.event_type == event_type and event.run_version == expected.version
    ]
    if len(matches) != 1:
        return None
    event = matches[0]
    try:
        control = await work.commands.get(UUID(str(event.payload["command_id"])))
    except Exception:  # noqa: BLE001 - an unavailable control record grants no authority
        return None
    wanted = "pause" if event_type == "run.paused" else "cancel"
    payload = {
        "command_id": str(control.id),
        "command_type": wanted,
        "command_payload": {},
        "expected_run_version": expected.version - 1,
    }
    if (
        control.run_id != run_id
        or control.command_type != wanted
        or control.status not in {CommandStatus.LEASED, CommandStatus.COMPLETED}
        or control.payload_schema_version != 1
        or control.payload != {}
        or control.actor_id is None
        or control.expected_run_version != expected.version - 1
        or event.payload != payload
        or event.payload_schema_version != 1
        or event.actor_class != "operator"
        or event.actor_id != control.actor_id
    ):
        return None
    return event


def _same_normal_delivery(command: CommandEnvelope, fenced: CommandEnvelope) -> bool:
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


__all__ = ["controlled_stop", "pending_current_control_stop"]
