"""Receipt-bound continuation of interrupted read-only release polls."""

from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.monitor_replay import verify_monitor_replay
from forge.application.services.resume_source import (
    RESUME_FIELDS,
    continuation_binding,
    resume_command_ids,
)
from forge.application.services.state_engine import StateEngine
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot
from forge.persistence.repositories.commands import CommandNotFound


async def settle_observed_monitors(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot, store: ArtifactStore | None
) -> None:
    """Acknowledge committed observations without replaying their remote reads."""
    events = await work.events.list_after(paused.id, 0)
    restored = StateEngine().resume(paused)
    for source in await work.commands.list_outstanding_normal(
        run_id=paused.id, exclude_command_id=resume.id
    ):
        if source.command_type != "monitor_pr":
            continue
        settled = [
            e
            for e in events
            if e.event_type == "run.pr_observed"
            and e.payload.get("source_command_id") == str(source.id)
        ]
        if not settled:
            continue
        if (
            store is None
            or len(settled) != 1
            or settled[0].run_version != paused.version - 1
            or settled[0].payload.get("target") != restored.state.value
        ):
            raise CommandRecoveryRequired("observed monitor outcome does not match paused state")
        await monitor_origin(work, source)
        record_id, poll = monitor_inputs(source)
        await verify_monitor_replay(store, source, work, settled, record_id, poll)
        if await work.commands.complete_expired_observed_lease(source) is None:
            raise CommandRecoveryRequired("observed monitor lease is active or changed")
        await work.events.append(
            RunEvent(
                run_id=paused.id,
                run_version=paused.version,
                event_type="monitor.acknowledged_on_resume",
                actor_class="worker",
                actor_id=resume.actor_id,
                payload={
                    "source_command_id": str(source.id),
                    "resume_command_id": str(resume.id),
                    "observation_event_id": str(settled[0].event_id),
                },
            )
        )


def monitor_inputs(command: CommandEnvelope) -> tuple[UUID, int]:
    payload = {key: value for key, value in command.payload.items() if key not in RESUME_FIELDS}
    if (
        command.command_type != "monitor_pr"
        or command.payload_schema_version != 1
        or command.actor_id is None
        or set(payload) != {"pull_request_id", "poll"}
        or type(payload["poll"]) is not int
        or payload["poll"] < 1
    ):
        raise CommandRecoveryRequired("PR monitoring command is invalid")
    try:
        record_id = UUID(str(payload["pull_request_id"]))
    except ValueError:
        raise CommandRecoveryRequired("PR monitoring identity is invalid") from None
    return record_id, payload["poll"]


async def monitor_origin(
    work: UnitOfWork, command: CommandEnvelope, *, replaying_resume: UUID | None = None
) -> CommandEnvelope:
    """Resolve original poll authority; live dispatch must separately fence its lease."""
    current = command
    while (ids := resume_command_ids(current.payload)) is not None:
        try:
            resume, source = await work.commands.get(ids[0]), await work.commands.get(ids[1])
        except CommandNotFound:
            raise CommandRecoveryRequired("monitor resume source is missing") from None
        _, poll = monitor_inputs(current)
        if (
            source.status is not CommandStatus.CANCELLED
            or source.run_id != command.run_id
            or source.command_type != "monitor_pr"
            or source.payload_schema_version != 1
            or source.actor_id != current.actor_id
            or source.expected_run_version != resume.expected_run_version - 1
            or resume.run_id != command.run_id
            or resume.command_type != "resume"
            or resume.payload_schema_version != 1
            or resume.payload != {}
            or resume.actor_id is None
            or (
                resume.status is not CommandStatus.COMPLETED
                and not (resume.id == replaying_resume and resume.status is CommandStatus.LEASED)
            )
            or current.expected_run_version != resume.expected_run_version + 1
            or current.idempotency_key != f"{command.run_id}:resume:{resume.id}:monitor_pr:{poll}"
            or {k: v for k, v in current.payload.items() if k not in RESUME_FIELDS}
            != {k: v for k, v in source.payload.items() if k not in RESUME_FIELDS}
        ):
            raise CommandRecoveryRequired("monitor resume authority differs")
        events = await work.events.list_after(command.run_id, 0)
        resumed = [
            e
            for e in events
            if e.event_type == "run.resumed" and e.run_version == current.expected_run_version
        ]
        stopped = [
            e
            for e in events
            if e.event_type == "monitor.suspended"
            and e.run_version == resume.expected_run_version
            and e.payload.get("source_command_id") == str(source.id)
        ]
        if (
            len(resumed) != 1
            or len(stopped) != 1
            or resumed[0].actor_class != "operator"
            or resumed[0].actor_id != resume.actor_id
            or resumed[0].payload.get("command_id") != str(resume.id)
            or resumed[0].payload.get("restored_state") != "MONITORING_PR"
            or resumed[0].payload.get("continuation") != continuation_binding(current, source.id)
            or stopped[0].actor_class != "worker"
            or stopped[0].actor_id != resume.actor_id
            or stopped[0].payload
            != {
                "source_command_id": str(source.id),
                "resume_command_id": str(resume.id),
                "pause_command_id": resumed[0].payload.get("pause_command_id"),
                "source": continuation_binding(source, source.id),
            }
        ):
            raise CommandRecoveryRequired("monitor resume receipt differs")
        try:
            pause = await work.commands.get(UUID(str(resumed[0].payload.get("pause_command_id"))))
        except ValueError, CommandNotFound:
            raise CommandRecoveryRequired("monitor resume pause is missing") from None
        pauses = [
            e
            for e in events
            if e.event_type == "run.paused" and e.run_version == resume.expected_run_version
        ]
        if (
            pause.run_id != command.run_id
            or pause.command_type != "pause"
            or pause.status is not CommandStatus.COMPLETED
            or pause.payload != {}
            or pause.payload_schema_version != 1
            or pause.actor_id is None
            or pause.expected_run_version != source.expected_run_version
            or len(pauses) != 1
            or pauses[0].actor_class != "operator"
            or pauses[0].actor_id != pause.actor_id
            or pauses[0].payload
            != {
                "command_id": str(pause.id),
                "command_type": "pause",
                "command_payload": {},
                "expected_run_version": source.expected_run_version,
            }
        ):
            raise CommandRecoveryRequired("monitor resume pause authority differs")
        current = source
    monitor_inputs(current)
    return current


async def resume_monitoring(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot, pause: CommandEnvelope
) -> tuple[CommandEnvelope, CommandEnvelope]:
    """Cancel only the exact unacknowledged poll and queue its continuation atomically."""
    sources = await work.commands.list_outstanding_normal(
        run_id=paused.id, exclude_command_id=resume.id
    )
    if len(sources) != 1:
        raise CommandRecoveryRequired("monitor resume has no unique stopped poll")
    source = sources[0]
    record_id, poll = monitor_inputs(source)
    await monitor_origin(work, source)
    record = await work.releases.get_for_run(paused.id)
    events = await work.events.list_after(paused.id, 0)
    if (
        source.expected_run_version != pause.expected_run_version
        or record is None
        or record.id != record_id
        or any(
            e.event_type == "run.pr_observed"
            and e.payload.get("source_command_id") == str(source.id)
            for e in events
        )
    ):
        raise CommandRecoveryRequired("monitor resume source already settled or differs")
    cancelled = (
        await work.commands.cancel_pending_unstarted(source)
        if source.status is CommandStatus.PENDING
        else await work.commands.cancel_expired_observed_lease(
            source, reason="poll stopped for operator resume"
        )
    )
    if cancelled is None:
        raise CommandRecoveryRequired("monitor resume source lease is active or changed")
    proof = await work.runs.prove_quiescent(paused.id, exclude_command_id=resume.id)
    if not proof.is_quiescent:
        raise CommandRecoveryRequired("monitor resume has unresolved effects")
    await work.events.append(
        RunEvent(
            run_id=paused.id,
            run_version=paused.version,
            event_type="monitor.suspended",
            actor_class="worker",
            actor_id=resume.actor_id,
            payload={
                "source_command_id": str(source.id),
                "resume_command_id": str(resume.id),
                "pause_command_id": str(pause.id),
                "source": continuation_binding(source, source.id),
            },
        )
    )
    payload = {k: v for k, v in source.payload.items() if k not in RESUME_FIELDS}
    payload.update(resume_command_id=str(resume.id), source_command_id=str(source.id))
    queued = await work.commands.enqueue(
        run_id=paused.id,
        command_type="monitor_pr",
        idempotency_key=f"{paused.id}:resume:{resume.id}:monitor_pr:{poll}",
        payload=payload,
        expected_run_version=paused.version + 1,
        actor_id=source.actor_id,
    )
    return cancelled, queued
