"""Bind planning restarts to the immutable event that authorized the queue entry."""

from __future__ import annotations

from uuid import UUID

from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.command import CommandEnvelope, CommandStatus


async def restart_source(
    work: UnitOfWork,
    queued: CommandEnvelope,
    *,
    source_status: CommandStatus = CommandStatus.COMPLETED,
) -> CommandEnvelope | None:
    events = [
        event
        for event in await work.events.list_for_version(queued.run_id, queued.expected_run_version)
        if event.event_type in {"run.plan_revision_requested", "approval.stale"}
    ]
    if len(events) != 1:
        return None
    event = events[0]
    payload = event.payload
    if (
        event.run_id != queued.run_id
        or event.run_version != queued.expected_run_version
        or event.actor_id != queued.actor_id
        or payload.get("planning_command_id") != str(queued.id)
        or payload.get("planning_payload") != queued.payload
        or payload.get("semantic_attempt") != queued.payload.get("semantic_attempt")
    ):
        return None
    try:
        source_id = UUID(str(payload["command_id"]))
    except KeyError, ValueError:
        return None
    source = await work.commands.get(source_id)
    if (
        source is None
        or source.run_id != queued.run_id
        or source.actor_id != queued.actor_id
        or source.expected_run_version + 1 != queued.expected_run_version
        or source.status is not source_status
    ):
        return None
    if event.event_type == "run.plan_revision_requested":
        if (
            event.actor_class != "operator"
            or source.command_type != "request_plan_revision"
            or set(queued.payload) != {"semantic_attempt", "feedback_digest"}
            or not isinstance(queued.payload["feedback_digest"], str)
            or payload.get("feedback_digest") != queued.payload["feedback_digest"]
        ):
            return None
    elif (
        event.actor_class != "worker"
        or source.command_type != "approve_plan"
        or set(queued.payload) != {"semantic_attempt"}
        or source.payload != {"approval_id": payload.get("approval_id")}
    ):
        return None
    return source
