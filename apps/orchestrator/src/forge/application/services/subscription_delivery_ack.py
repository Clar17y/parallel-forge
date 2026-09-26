"""Preserve failed deliveries while binding proved repairs to operator resume."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.persistence.repositories.commands import CommandNotFound

ACKNOWLEDGMENTS = "subscription_delivery_acknowledgments"
_EVENTS = {
    "remediate_remote": "run.subscription_remote_repair_requested",
    "update_base": "run.subscription_base_adopted",
}


def _invalid() -> CommandRecoveryRequired:
    return CommandRecoveryRequired("subscription repair delivery acknowledgment differs")


def delivery_binding(source: CommandEnvelope, decision: RunEvent) -> dict[str, object]:
    """Bind immutable rows; callers separately prove the controller's decision."""
    if (
        source.status is not CommandStatus.FAILED
        or source.actor_id is None
        or source.payload_schema_version != 1
        or type(source.attempt) is not int
        or source.attempt < 1
        or decision.run_id != source.run_id
        or decision.event_type != _EVENTS.get(source.command_type)
        or decision.run_version != source.expected_run_version
        or decision.payload.get("source_command_id") != str(source.id)
        or decision.actor_class != "worker"
        or decision.actor_id != source.actor_id
        or decision.payload_schema_version != 1
        or decision.sequence is None
    ):
        raise _invalid()
    source_values = {
        "id": str(source.id),
        "run_id": str(source.run_id),
        "command_type": source.command_type,
        "idempotency_key": source.idempotency_key,
        "payload": source.payload,
        "status": source.status.value,
        "expected_run_version": source.expected_run_version,
        "actor_id": str(source.actor_id),
        "payload_schema_version": source.payload_schema_version,
        "attempt": source.attempt,
        "available_at": source.available_at.isoformat(),
        "created_at": source.created_at.isoformat() if source.created_at is not None else None,
        "completed_at": source.completed_at.isoformat()
        if source.completed_at is not None
        else None,
        "error_summary": source.error_summary,
    }
    return {
        "source_command_id": str(source.id),
        "source_command_digest": canonical_digest(source_values),
        "decision_event_id": str(decision.event_id),
        "decision_event_digest": canonical_digest(
            {
                "run_id": str(decision.run_id),
                "run_version": decision.run_version,
                "event_type": decision.event_type,
                "sequence": decision.sequence,
                "actor_class": decision.actor_class,
                "actor_id": str(decision.actor_id),
                "payload_schema_version": decision.payload_schema_version,
                "payload": decision.payload,
            }
        ),
    }


async def _control(
    work: UnitOfWork, event: RunEvent, kind: str, expected_version: int
) -> CommandEnvelope:
    try:
        command = await work.commands.get(UUID(str(event.payload.get("command_id"))))
    except CommandNotFound, KeyError, TypeError, ValueError:
        raise _invalid() from None
    expected = {
        "command_id": str(command.id),
        "command_type": kind,
        "command_payload": {},
        "expected_run_version": expected_version,
    }
    if (
        command.run_id != event.run_id
        or command.command_type != kind
        or command.status is not CommandStatus.COMPLETED
        or command.expected_run_version != expected_version
        or command.payload_schema_version != 1
        or command.payload != {}
        or command.actor_id is None
        or event.run_version != expected_version + 1
        or event.event_type != ("run.paused" if kind == "pause" else "run.resumed")
        or event.actor_class != "operator"
        or event.actor_id != command.actor_id
        or event.payload_schema_version != 1
        or canonical_digest({key: event.payload.get(key) for key in expected})
        != canonical_digest(expected)
        or (kind == "pause" and canonical_digest(event.payload) != canonical_digest(expected))
    ):
        raise _invalid()
    return command


async def require_acknowledged_delivery(work: UnitOfWork, source: CommandEnvelope) -> None:
    """Require completed delivery or its exact failed-delivery resume binding.

    This verifies delivery and control provenance only. The consumer must still
    re-prove the actual controller decision and its fresh acceptance authority.
    A failed command stays failed, including its original error and timestamps.
    """
    if await work.commands.get(source.id) != source:
        raise _invalid()
    if source.status is CommandStatus.COMPLETED:
        return
    events = await work.events.list_after(source.run_id, 0)
    decisions = [
        event
        for event in events
        if event.event_type == _EVENTS.get(source.command_type)
        and event.payload.get("source_command_id") == str(source.id)
    ]
    if len(decisions) != 1:
        raise _invalid()
    decision = decisions[0]
    binding = delivery_binding(source, decision)
    resumed = []
    for event in events:
        acknowledgments = event.payload.get(ACKNOWLEDGMENTS)
        if (
            event.event_type == "run.resumed"
            and isinstance(acknowledgments, Sequence)
            and any(
                isinstance(item, Mapping) and item.get("source_command_id") == str(source.id)
                for item in acknowledgments
            )
        ):
            resumed.append(event)
    if len(resumed) != 1:
        raise _invalid()
    restored = resumed[0]
    resume = await _control(work, restored, "resume", decision.run_version + 1)
    pauses = [
        event
        for event in events
        if event.event_type == "run.paused" and event.run_version == resume.expected_run_version
    ]
    if len(pauses) != 1:
        raise _invalid()
    paused = pauses[0]
    pause = await _control(work, paused, "pause", decision.run_version)
    expected = {
        ACKNOWLEDGMENTS: [binding],
        "paused_version": resume.expected_run_version,
        "pause_command_id": str(pause.id),
        "restored_state": RunState.REMEDIATING.value,
    }
    current = await work.runs.get_for_update(source.run_id)
    if (
        canonical_digest({key: restored.payload.get(key) for key in expected})
        != canonical_digest(expected)
        or current.version < restored.run_version
        or decision.sequence is None
        or paused.sequence is None
        or restored.sequence is None
        or not decision.sequence < paused.sequence < restored.sequence
    ):
        raise _invalid()
