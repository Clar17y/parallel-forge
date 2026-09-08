"""Explicit causal authority for fresh stage executions after an operator resume."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.failed_resume import validate_failed_receipt
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunState

RESUME_FIELDS = frozenset({"resume_command_id", "source_command_id"})


def continuation_binding(command: CommandEnvelope, source_id: UUID) -> dict[str, object]:
    """Exact queue identity persisted with the atomic resume transition."""
    return {
        "command_id": str(command.id),
        "command_type": command.command_type,
        "idempotency_key": command.idempotency_key,
        "payload": dict(command.payload),
        "actor_id": str(command.actor_id) if command.actor_id is not None else None,
        "source_command_id": str(source_id),
    }


def resume_command_ids(payload: Mapping[str, object]) -> tuple[UUID, UUID] | None:
    """Decode only a complete canonical resume route, never a partial hint."""
    if not RESUME_FIELDS.intersection(payload):
        return None
    try:
        resume_id, source_id = (
            UUID(str(payload[key])) for key in ("resume_command_id", "source_command_id")
        )
        if (
            not resume_id.int
            or not source_id.int
            or resume_id == source_id
            or str(resume_id) != payload["resume_command_id"]
            or str(source_id) != payload["source_command_id"]
        ):
            raise ValueError
    except KeyError, TypeError, ValueError:
        raise CommandRecoveryRequired("resume continuation identifiers are invalid") from None
    return resume_id, source_id


async def resume_source(work: UnitOfWork, queued: CommandEnvelope) -> CommandEnvelope | None:
    """Verify resume provenance; the stage still fences its lease and live inputs.

    Return the original stopped command for its existing phase-specific causal
    checks. A continuation preserves every phase input except semantic attempt.
    """
    return await _resume_source(work, queued, historical=False)


async def resume_history(work: UnitOfWork, queued: CommandEnvelope) -> tuple[CommandEnvelope, ...]:
    """Return verified predecessors, newest first, with strictly decreasing versions."""
    source = await resume_source(work, queued)
    history = []
    while source is not None:
        history.append(source)
        if resume_command_ids(source.payload) is None:
            break
        source = await _resume_source(work, source, historical=True)
        if source is None:
            raise CommandRecoveryRequired("resume origin is unavailable")
    return tuple(history)


async def resume_origin(work: UnitOfWork, queued: CommandEnvelope) -> CommandEnvelope | None:
    """Resolve original stage authority through verified resume predecessors."""
    history = await resume_history(work, queued)
    return history[-1] if history else None


async def _resume_source(
    work: UnitOfWork, queued: CommandEnvelope, *, historical: bool
) -> CommandEnvelope | None:
    identities = resume_command_ids(queued.payload)
    if identities is None:
        return None
    resume_id, source_id = identities
    try:
        resume = await work.commands.get(resume_id)
        source = await work.commands.get(source_id)
    except Exception:  # noqa: BLE001 - unavailable provenance never authorizes dispatch
        raise CommandRecoveryRequired("resume continuation source is unavailable") from None
    attempt = queued.payload.get("semantic_attempt")
    previous_attempt = source.payload.get("semantic_attempt", 1)
    if (
        queued.payload_schema_version != 1
        or queued.status
        not in (
            {CommandStatus.CANCELLED, CommandStatus.COMPLETED, CommandStatus.FAILED}
            if historical
            else {CommandStatus.LEASED}
        )
        or resume.run_id != queued.run_id
        or resume.command_type != "resume"
        or resume.status is not CommandStatus.COMPLETED
        or resume.payload_schema_version != 1
        or resume.payload != {}
        or resume.actor_id is None
        or resume.expected_run_version + 1 != queued.expected_run_version
        or source.id == queued.id
        or source.run_id != queued.run_id
        or source.expected_run_version >= resume.expected_run_version
        or source.command_type != queued.command_type
        or source.status
        not in {CommandStatus.CANCELLED, CommandStatus.COMPLETED, CommandStatus.FAILED}
        or source.payload_schema_version != 1
        or source.actor_id != queued.actor_id
        or type(attempt) is not int
        or type(previous_attempt) is not int
        or queued.idempotency_key
        != f"{queued.run_id}:resume:{resume.id}:{queued.command_type}:{attempt}"
        or {
            key: value
            for key, value in queued.payload.items()
            if key not in RESUME_FIELDS | {"semantic_attempt"}
        }
        != {
            key: value
            for key, value in source.payload.items()
            if key not in RESUME_FIELDS | {"semantic_attempt"}
        }
    ):
        raise CommandRecoveryRequired("resume continuation changed stage authority")
    events = await work.events.list_after(queued.run_id, 0)
    resumed = [
        event
        for event in events
        if event.run_version == queued.expected_run_version and event.event_type == "run.resumed"
    ]
    stopped = [
        event
        for event in events
        if event.run_version == resume.expected_run_version
        and event.event_type
        in {"delivery.suspended", "delivery.deferred", "delivery.failed_before_admission"}
        and event.payload.get("command_id") == str(source.id)
    ]
    if len(resumed) != 1 or len(stopped) != 1:
        raise CommandRecoveryRequired("resume continuation has no unique causal evidence")
    event, receipt = resumed[0], stopped[0]
    payload = event.payload
    deferred = receipt.event_type == "delivery.deferred"
    failed_unadmitted = receipt.event_type == "delivery.failed_before_admission"
    if (source.status is CommandStatus.FAILED) != failed_unadmitted:
        raise CommandRecoveryRequired("failed continuation receipt type differs")
    if failed_unadmitted:
        try:
            pause_id = UUID(str(payload.get("pause_command_id")))
            raw_state = payload.get("restored_state")
            if not isinstance(raw_state, str):
                raise TypeError
            state = RunState(raw_state)
        except ValueError, TypeError:
            raise CommandRecoveryRequired("failed continuation control differs") from None
        await validate_failed_receipt(
            work, source, paused_version=resume.expected_run_version, pause_id=pause_id, state=state
        )
    if deferred and (
        source.status is not CommandStatus.CANCELLED
        or source.attempt != 0
        or source.expected_run_version != resume.expected_run_version - 1
        or receipt.payload.get("actor_id")
        != (str(source.actor_id) if source.actor_id is not None else None)
        or receipt.payload.get("payload_schema_version") != source.payload_schema_version
    ):
        raise CommandRecoveryRequired("deferred continuation authority differs")
    if (
        attempt != previous_attempt + (0 if deferred or failed_unadmitted else 1)
        or event.actor_class != "operator"
        or event.actor_id != resume.actor_id
        or event.payload_schema_version != 1
        or payload.get("command_id") != str(resume.id)
        or payload.get("command_type") != "resume"
        or payload.get("command_payload") != {}
        or payload.get("expected_run_version") != resume.expected_run_version
        or payload.get("paused_version") != resume.expected_run_version
        or payload.get("continuation") != continuation_binding(queued, source.id)
        or receipt.actor_class != "worker"
        or receipt.actor_id is not None
        or receipt.payload_schema_version != 1
        or receipt.payload.get(
            "pause_command_id" if deferred or failed_unadmitted else "control_command_id"
        )
        != payload.get("pause_command_id")
        or receipt.payload.get(
            "deferred_state" if deferred or failed_unadmitted else "admitted_state"
        )
        != payload.get("restored_state")
        or receipt.payload.get("command_type") != source.command_type
        or receipt.payload.get("command_payload") != source.payload
        or receipt.payload.get("idempotency_key") != source.idempotency_key
        or receipt.payload.get("expected_run_version") != source.expected_run_version
        or receipt.payload.get("delivery_attempt") != source.attempt
        or receipt.payload.get("semantic_attempt") != previous_attempt
    ):
        raise CommandRecoveryRequired("resume continuation causal evidence differs")
    return source


__all__ = [
    "RESUME_FIELDS",
    "continuation_binding",
    "resume_command_ids",
    "resume_history",
    "resume_origin",
    "resume_source",
]
