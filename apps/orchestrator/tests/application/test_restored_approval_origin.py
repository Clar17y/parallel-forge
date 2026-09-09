"""Restored evidence follows exact operator pause/resume authority."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.paused_approvals import approval_gate_origin
from forge.domain.command import CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunState


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [None, "digest", "actor", "pause_version", "source_version", "unfinished", "missing_pause"],
)
async def test_restored_gate_origin_rejects_changed_authority(fault):
    run_id, actor, resume_id, pause_id = uuid4(), uuid4(), uuid4(), uuid4()
    pause = SimpleNamespace(
        id=pause_id,
        run_id=run_id,
        command_type="pause",
        status=CommandStatus.COMPLETED,
        expected_run_version=8 if fault == "pause_version" else 9,
        payload_schema_version=1,
        payload={},
        actor_id=actor,
    )
    resume = SimpleNamespace(
        id=resume_id,
        run_id=run_id,
        command_type="resume",
        status=CommandStatus.LEASED if fault == "unfinished" else CommandStatus.COMPLETED,
        expected_run_version=10,
        payload_schema_version=1,
        payload={},
        actor_id=actor,
    )
    resumed = RunEvent(
        run_id=run_id,
        run_version=11,
        event_type="run.resumed",
        actor_class="operator",
        actor_id=uuid4() if fault == "actor" else actor,
        payload={
            "command_id": str(resume_id),
            "pause_command_id": str(pause_id),
            "restored_state": RunState.AWAITING_PR_APPROVAL.value,
            "approval_gate": {
                "gate": "pr",
                "evidence_digest": "b" * 64 if fault == "digest" else "a" * 64,
                "source_version": 8 if fault == "source_version" else 9,
            },
        },
    )
    paused = RunEvent(
        run_id=run_id,
        run_version=10,
        event_type="run.paused",
        actor_class="operator",
        actor_id=actor,
        payload={
            "command_id": str(pause_id),
            "command_type": "pause",
            "command_payload": {},
            "expected_run_version": 9,
        },
    )

    class Events:
        async def list_for_version(self, identity, version):
            assert identity == run_id
            return {11: [resumed], 10: [] if fault == "missing_pause" else [paused]}.get(
                version, []
            )

    class Commands:
        async def get(self, identity):
            return {resume_id: resume, pause_id: pause}[identity]

    work = SimpleNamespace(events=Events(), commands=Commands())
    if fault:
        with pytest.raises(CommandRecoveryRequired, match="provenance"):
            await approval_gate_origin(work, run_id, 11, "pr", "a" * 64)
    else:
        assert await approval_gate_origin(work, run_id, 11, "pr", "a" * 64) == 9
