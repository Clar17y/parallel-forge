"""Receipt proof for a reviewed push that committed before its delivery ack."""

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.command import CommandEnvelope
from forge.domain.run import RunState
from forge.persistence.repositories.release import ReleaseRecordConflict
from forge.persistence.repositories.runs import PersistenceDataError


async def verify_reviewed_push_replay(
    command: CommandEnvelope, work: UnitOfWork, *, paused: bool = False
) -> bool:
    """Validate one settled reviewed push and its exact successor monitor."""
    run = await work.runs.get_for_update(command.run_id)
    record = await work.releases.get_for_run(run.id)
    if (
        command.command_type != "push_reviewed_pr"
        or command.payload_schema_version != 1
        or command.actor_id is None
        or run.state is not (RunState.PAUSED if paused else RunState.MONITORING_PR)
        or (paused and run.suspended_state is not RunState.MONITORING_PR)
        or run.version != command.expected_run_version + (1 if paused else 0)
    ):
        raise CommandRecoveryRequired("reviewed push replay authority differs")
    events = [
        event
        for event in await work.events.list_after(run.id, 0)
        if event.event_type == "run.pr_updated"
        and event.payload.get("source_command_id") == str(command.id)
    ]
    if not events:
        return False
    if len(events) != 1:
        raise CommandRecoveryRequired("reviewed push replay has duplicate settlements")
    if (
        record is None
        or record.reviewed_push_intent_id is None
        or record.candidate_evidence_digest is None
    ):
        raise CommandRecoveryRequired("reviewed push replay receipt is absent")
    event = events[0]
    poll = event.payload.get("poll")
    queued = await work.commands.get_by_idempotency_key(str(event.payload.get("monitor_key")))
    if (
        type(poll) is not int
        or poll < 1
        or event.actor_class != "worker"
        or event.payload_schema_version != 1
        or event.actor_id != command.actor_id
        or event.run_version != command.expected_run_version
        or event.payload.get("pull_request_id") != str(record.id)
        or event.payload.get("push_intent_id") != str(record.reviewed_push_intent_id)
        or event.payload.get("candidate_evidence_digest") != record.candidate_evidence_digest
        or record.candidate_evidence_digest != command.payload.get("candidate_evidence_digest")
        or queued is None
        or str(queued.id) != event.payload.get("monitor_command_id")
        or queued.command_type != "monitor_pr"
        or queued.run_id != run.id
        or queued.payload_schema_version != 1
        or queued.idempotency_key != f"{run.id}:monitor-pr:{poll + 1}"
        or queued.actor_id != command.actor_id
        or queued.expected_run_version != event.run_version
        or queued.payload != {"pull_request_id": str(record.id), "poll": poll + 1}
    ):
        raise CommandRecoveryRequired("reviewed push replay differs")
    if event.payload != {
        "source_command_id": str(command.id),
        "pull_request_id": str(record.id),
        "push_intent_id": str(record.reviewed_push_intent_id),
        "candidate_evidence_digest": record.candidate_evidence_digest,
        "poll": poll,
        "monitor_command_id": str(queued.id),
        "monitor_key": queued.idempotency_key,
    }:
        raise CommandRecoveryRequired("reviewed push replay event differs")
    try:
        intent = await work.operations.get(record.reviewed_push_intent_id)
        if any(
            intent.request_payload.get(key) != command.payload.get(key)
            for key in (
                "approval_id", "candidate_evidence_digest", "previous_head_sha",
                "pull_request_id", "node_id",
            )
        ):
            raise CommandRecoveryRequired("reviewed push replay command binding differs")
        await work.releases.record_reviewed_push(
            run.id, record.pull_request, record.reviewed_push_intent_id
        )
    except ReleaseRecordConflict, PersistenceDataError:
        raise CommandRecoveryRequired("reviewed push replay receipt differs") from None
    return True
