"""A resume continuation cannot substitute its source or approved inputs."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.resume_source import resume_source
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent


def _case():
    now = datetime.now(UTC)
    run_id, original_actor, resume_actor = uuid4(), uuid4(), uuid4()

    def envelope(kind, version, actor, payload, status):
        return CommandEnvelope(
            id=uuid4(),
            run_id=run_id,
            command_type=kind,
            idempotency_key=str(uuid4()),
            payload=payload,
            status=status,
            expected_run_version=version,
            actor_id=actor,
            payload_schema_version=1,
            attempt=1,
            available_at=now,
            lease_owner="worker" if status is CommandStatus.LEASED else None,
            lease_expires_at=now + timedelta(seconds=60)
            if status is CommandStatus.LEASED
            else None,
            completed_at=now if status is not CommandStatus.LEASED else None,
        )

    source = envelope(
        "implement", 4, original_actor, {"semantic_attempt": 1}, CommandStatus.CANCELLED
    )
    resume = envelope("resume", 5, resume_actor, {}, CommandStatus.COMPLETED)
    pause_id = uuid4()
    queued = envelope(
        "implement",
        6,
        original_actor,
        {
            "semantic_attempt": 2,
            "resume_command_id": str(resume.id),
            "source_command_id": str(source.id),
        },
        CommandStatus.LEASED,
    )
    queued = replace(queued, idempotency_key=f"{run_id}:resume:{resume.id}:implement:2")
    event = RunEvent(
        run_id=run_id,
        run_version=6,
        event_type="run.resumed",
        actor_class="operator",
        actor_id=resume_actor,
        payload={
            "command_id": str(resume.id),
            "command_type": "resume",
            "command_payload": {},
            "expected_run_version": 5,
            "paused_version": 5,
            "pause_command_id": str(pause_id),
            "restored_state": "IMPLEMENTING",
            "continuation": {
                "command_id": str(queued.id),
                "command_type": queued.command_type,
                "idempotency_key": queued.idempotency_key,
                "payload": dict(queued.payload),
                "actor_id": str(original_actor),
                "source_command_id": str(source.id),
            },
        },
    )
    stopped = RunEvent(
        run_id=run_id,
        run_version=5,
        event_type="delivery.suspended",
        actor_class="worker",
        payload={
            "command_id": str(source.id),
            "command_type": source.command_type,
            "command_payload": dict(source.payload),
            "idempotency_key": source.idempotency_key,
            "expected_run_version": 4,
            "delivery_attempt": 1,
            "semantic_attempt": 1,
            "admitted_state": "IMPLEMENTING",
            "control_command_id": str(pause_id),
        },
    )
    commands = {source.id: source, resume.id: resume}

    async def get(command_id):
        return commands[command_id]

    work = SimpleNamespace(
        commands=SimpleNamespace(get=get),
        events=SimpleNamespace(list_after=AsyncMock(return_value=[stopped, event])),
    )
    return work, queued, source, resume, event, stopped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper", [None, "payload", "actor", "source", "event_actor", "receipt", "resume_status"]
)
async def test_resume_source_requires_exact_operator_and_stopped_delivery_binding(tamper):
    work, queued, source, resume, event, stopped = _case()
    if tamper == "payload":
        queued = replace(
            queued, payload={**queued.payload, "validation_evidence_set_id": str(uuid4())}
        )
    elif tamper == "actor":
        queued = replace(queued, actor_id=resume.actor_id)
    elif tamper == "source":
        queued = replace(queued, payload={**queued.payload, "source_command_id": str(uuid4())})
    elif tamper == "event_actor":
        work.events.list_after.return_value = [stopped, replace(event, actor_id=source.actor_id)]
    elif tamper == "receipt":
        work.events.list_after.return_value = [event]
    elif tamper == "resume_status":

        async def get(command_id):
            return (
                source if command_id == source.id else replace(resume, status=CommandStatus.FAILED)
            )

        work.commands.get = get
    if tamper is None:
        assert await resume_source(work, queued) == source
    else:
        with pytest.raises(CommandRecoveryRequired):
            await resume_source(work, queued)
