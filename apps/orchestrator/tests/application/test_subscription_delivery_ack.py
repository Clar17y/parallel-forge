"""A repair receipt does not make a failed delivery completed or self-authorizing."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.subscription_delivery_ack import (
    delivery_binding,
    require_acknowledged_delivery,
)
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent


def acknowledged_case():
    run_id, actor = uuid4(), uuid4()
    now = datetime.now(UTC)

    def command(kind, version, status):
        identity = uuid4()
        return CommandEnvelope(
            id=identity,
            run_id=run_id,
            command_type=kind,
            idempotency_key=f"{run_id}:{kind}:{identity}",
            payload={},
            status=status,
            expected_run_version=version,
            actor_id=actor,
            payload_schema_version=1,
            attempt=1,
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            created_at=now,
            completed_at=now,
            error_summary="delivery completion unavailable"
            if status is CommandStatus.FAILED
            else None,
        )

    source = command("remediate_remote", 10, CommandStatus.FAILED)
    pause = command("pause", 10, CommandStatus.COMPLETED)
    resume = command("resume", 11, CommandStatus.COMPLETED)
    decision = RunEvent(
        run_id=run_id,
        run_version=10,
        sequence=1,
        event_type="run.subscription_remote_repair_requested",
        actor_class="worker",
        actor_id=actor,
        payload={"source_command_id": str(source.id), "repair": {"repaired": True}},
    )

    def control_event(delivery, sequence):
        return RunEvent(
            run_id=run_id,
            run_version=delivery.expected_run_version + 1,
            event_type="run.paused" if delivery.command_type == "pause" else "run.resumed",
            actor_class="operator",
            actor_id=actor,
            sequence=sequence,
            payload={
                "command_id": str(delivery.id),
                "command_type": delivery.command_type,
                "command_payload": {},
                "expected_run_version": delivery.expected_run_version,
            },
        )

    paused = control_event(pause, 2)
    resumed = control_event(resume, 3)
    resumed = replace(
        resumed,
        payload=dict(resumed.payload)
        | {
            "paused_version": 11,
            "pause_command_id": str(pause.id),
            "restored_state": "REMEDIATING",
            "subscription_delivery_acknowledgments": [delivery_binding(source, decision)],
        },
    )
    commands = {item.id: item for item in (source, pause, resume)}
    events = [decision, paused, resumed]
    work = SimpleNamespace(
        commands=SimpleNamespace(get=AsyncMock(side_effect=lambda identity: commands[identity])),
        events=SimpleNamespace(list_after=AsyncMock(side_effect=lambda *_: tuple(events))),
        runs=SimpleNamespace(get_for_update=AsyncMock(return_value=SimpleNamespace(version=12))),
    )
    return SimpleNamespace(
        source=source,
        pause=pause,
        resume=resume,
        decision=decision,
        paused=paused,
        resumed=resumed,
        commands=commands,
        events=events,
        work=work,
    )


async def test_failed_delivery_requires_actual_completed_operator_controls():
    case = acknowledged_case()
    await require_acknowledged_delivery(case.work, case.source)
    assert case.commands[case.source.id] == case.source
    assert case.source.status is CommandStatus.FAILED


@pytest.mark.parametrize(
    "changed",
    [
        "failed_source",
        "decision",
        "binding",
        "pause_actor",
        "resume_status",
        "resume_payload",
        "resumed_actor",
        "duplicate",
        "order",
        "missing",
    ],
)
async def test_acknowledgment_rejects_altered_source_or_control_evidence(changed):
    case = acknowledged_case()
    if changed == "failed_source":
        case.commands[case.source.id] = replace(case.source, error_summary="different failure")
    elif changed == "decision":
        case.events[0] = replace(
            case.decision, payload=dict(case.decision.payload) | {"extra": True}
        )
    elif changed == "binding":
        binding = delivery_binding(case.source, case.decision) | {"extra": True}
        case.events[2] = replace(
            case.resumed,
            payload=dict(case.resumed.payload)
            | {
                "subscription_delivery_acknowledgments": [binding],
            },
        )
    elif changed == "pause_actor":
        case.commands[case.pause.id] = replace(case.pause, actor_id=uuid4())
    elif changed == "resume_status":
        case.commands[case.resume.id] = replace(case.resume, status=CommandStatus.FAILED)
    elif changed == "resume_payload":
        case.commands[case.resume.id] = replace(case.resume, payload={"extra": True})
    elif changed == "resumed_actor":
        case.events[2] = replace(case.resumed, actor_id=uuid4())
    elif changed == "duplicate":
        case.events.append(replace(case.resumed, event_id=uuid4(), sequence=4))
    elif changed == "order":
        case.events[1] = replace(case.paused, sequence=4)
    else:
        case.events.pop()
    with pytest.raises(CommandRecoveryRequired):
        await require_acknowledged_delivery(case.work, case.source)
