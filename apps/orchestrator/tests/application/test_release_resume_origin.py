from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.release_resume import resumed_release_origin
from forge.application.services.resume_source import continuation_binding
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent


def _case():
    now, run, actor = datetime.now(UTC), uuid4(), uuid4()

    def command(kind, version, payload, status, *, id=None):
        return CommandEnvelope(
            id=id or uuid4(),
            run_id=run,
            command_type=kind,
            idempotency_key="key",
            payload=payload,
            status=status,
            expected_run_version=version,
            actor_id=actor,
            payload_schema_version=1,
            attempt=1,
            available_at=now,
            lease_owner="worker" if status is CommandStatus.LEASED else None,
            lease_expires_at=now + timedelta(seconds=1) if status is CommandStatus.LEASED else None,
            completed_at=now
            if status in {CommandStatus.COMPLETED, CommandStatus.CANCELLED}
            else None,
        )

    approval = uuid4()
    source = command("publish_pr", 4, {"approval_id": str(approval)}, CommandStatus.CANCELLED)
    pause = command("pause", 4, {}, CommandStatus.COMPLETED)
    resume = command("resume", 5, {}, CommandStatus.COMPLETED)
    queued = command(
        "publish_pr",
        6,
        {
            "approval_id": str(approval),
            "resume_command_id": str(resume.id),
            "source_command_id": str(source.id),
        },
        CommandStatus.LEASED,
    )
    queued = replace(queued, idempotency_key=f"{run}:resume:{resume.id}:publish_pr")
    events = [
        RunEvent(
            run_id=run,
            run_version=5,
            event_type="run.paused",
            actor_class="operator",
            actor_id=actor,
            payload={
                "command_id": str(pause.id),
                "command_type": "pause",
                "command_payload": {},
                "expected_run_version": 4,
            },
        ),
        RunEvent(
            run_id=run,
            run_version=5,
            event_type="release.suspended",
            actor_class="worker",
            actor_id=actor,
            payload={
                "source_command_id": str(source.id),
                "resume_command_id": str(resume.id),
                "pause_command_id": str(pause.id),
                "phase": "publish_pr",
                "source": continuation_binding(source, source.id),
            },
        ),
        RunEvent(
            run_id=run,
            run_version=6,
            event_type="run.resumed",
            actor_class="operator",
            actor_id=actor,
            payload={
                "command_id": str(resume.id),
                "pause_command_id": str(pause.id),
                "restored_state": "PUBLISHING_PR",
                "continuation": continuation_binding(queued, source.id),
            },
        ),
    ]
    commands = {source.id: source, pause.id: pause, resume.id: resume}

    async def get(identifier):
        return commands[identifier]

    return (
        SimpleNamespace(
            commands=SimpleNamespace(get=get),
            events=SimpleNamespace(list_after=AsyncMock(return_value=events)),
        ),
        queued,
        source,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    [None, "body", "source", "pause", "status", "pause_actor", "pause_payload", "stopped_pause"],
)
async def test_resumed_release_origin_requires_exact_receipts_and_original_authority(tamper):
    work, queued, source = _case()
    if tamper == "pause_actor":
        event = work.events.list_after.return_value[0]
        work.events.list_after.return_value[0] = replace(event, actor_id=uuid4())
    elif tamper == "pause_payload":
        event = work.events.list_after.return_value[0]
        work.events.list_after.return_value[0] = replace(
            event, payload={**event.payload, "command_type": "cancel"}
        )
    elif tamper == "stopped_pause":
        event = work.events.list_after.return_value[1]
        work.events.list_after.return_value[1] = replace(
            event, payload={**event.payload, "pause_command_id": str(uuid4())}
        )
    elif tamper == "body":
        queued = replace(queued, payload={**queued.payload, "approval_id": str(uuid4())})
    elif tamper == "source":
        queued = replace(queued, payload={**queued.payload, "source_command_id": str(uuid4())})
    elif tamper == "pause":
        event = work.events.list_after.return_value[-1]
        work.events.list_after.return_value[-1] = replace(
            event, payload={**event.payload, "pause_command_id": str(uuid4())}
        )
    elif tamper == "status":
        source = replace(source, status=CommandStatus.COMPLETED)

        old = work.commands.get

        async def get(identifier):
            return source if identifier == source.id else await old(identifier)

        work.commands.get = get
    if tamper is None:
        assert await resumed_release_origin(work, queued) == source
    else:
        with pytest.raises(CommandRecoveryRequired):
            await resumed_release_origin(work, queued)
