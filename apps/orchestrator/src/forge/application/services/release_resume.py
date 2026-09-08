"""Receipt-bound continuation of paused release commands."""

from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.base_update_authority import base_update_origin
from forge.application.services.publication_replay import verify_publication_replay
from forge.application.services.resume_source import (
    RESUME_FIELDS,
    continuation_binding,
    resume_command_ids,
)
from forge.application.services.reviewed_push_replay import verify_reviewed_push_replay
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.merge_queue import QUEUE_OBSERVATION_FIELDS
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import CommandNotFound
from forge.persistence.repositories.release import ReleaseRecordConflict

_PHASES = {
    "publish_pr": RunState.PUBLISHING_PR,
    "push_reviewed_pr": RunState.MONITORING_PR,
    "merge_pr": RunState.MERGING,
    "observe_merge_queue": RunState.MERGING,
    "update_base": RunState.REMEDIATING,
}


async def _reviewed_push_authority(
    work: UnitOfWork,
    store: ArtifactStore | None,
    paused: RunSnapshot,
    source: CommandEnvelope,
    origin: CommandEnvelope,
) -> None:
    """Prove the immutable review decision before re-delivering its push."""
    if store is None:
        raise CommandRecoveryRequired("reviewed push resume evidence store is absent")
    try:
        approved = await ApprovedPlanLoader(store).load(work, paused.id)
    except Exception as error:
        raise CommandRecoveryRequired("reviewed push approved plan is unavailable") from error
    events = [
        event
        for event in await work.events.list_for_version(paused.id, origin.expected_run_version)
        if event.event_type == "run.review_decided"
        and event.actor_class == "worker"
        and event.actor_id is None
        and event.payload_schema_version == 1
        and event.payload.get("target") == RunState.MONITORING_PR.value
        and event.payload.get("queued_command_id") == str(origin.id)
        and event.payload.get("queued_key") == origin.idempotency_key
        and event.payload.get("queued_payload") == origin.payload
        and event.payload.get("pr_evidence_digest")
        == origin.payload.get("candidate_evidence_digest")
    ]
    if (
        approved.run.id != paused.id
        or approved.run.policy_version != paused.policy_version
        or source.actor_id != approved.approval_actor_id
        or len(events) != 1
    ):
        raise CommandRecoveryRequired("reviewed push resume authority differs")


async def settle_published_deliveries(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot
) -> None:
    """Acknowledge an expired publisher only after proving its committed outcome."""
    for source in await work.commands.list_outstanding_normal(
        run_id=paused.id, exclude_command_id=resume.id
    ):
        if source.command_type != "publish_pr":
            continue
        approval_id = _approval_id(source)
        if not await verify_publication_replay(source, work, approval_id, paused=True):
            continue
        origin = await resumed_release_origin(work, source)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        record = await work.releases.get_for_run(paused.id)
        if (
            not isinstance(approval, Approval)
            or record is None
            or approval.run_id != paused.id
            or approval.gate != "pr"
            or approval.policy_version != paused.policy_version
            or approval.authenticated_actor_id != source.actor_id
            or source.actor_id is None
            or approval.invalidated_at is not None
            or origin.expected_run_version != approval.run_version + 1
        ):
            raise CommandRecoveryRequired("published approval authority differs")
        # The idempotent repository path validates both canonical successful
        # operation receipts against the already-existing exact PR identity.
        try:
            await work.releases.record_publication(
                paused.id, record.pull_request, record.push_intent_id, record.publication_intent_id
            )
        except ReleaseRecordConflict:
            raise CommandRecoveryRequired("published operation receipts differ") from None
        intent = await work.operations.get(record.publication_intent_id)
        if (
            intent.request_payload.get("approval_id") != str(approval.id)
            or intent.request_payload.get("approval_digest") != approval.evidence_digest
        ):
            raise CommandRecoveryRequired("published operation approval differs")
        if await work.commands.complete_expired_observed_lease(source) is None:
            raise CommandRecoveryRequired("published command lease is active or changed")
        await work.events.append(
            RunEvent(
                run_id=paused.id,
                run_version=paused.version,
                event_type="publication.acknowledged_on_resume",
                actor_class="worker",
                actor_id=resume.actor_id,
                payload={
                    "source_command_id": str(source.id),
                    "resume_command_id": str(resume.id),
                    "publication_intent_id": str(record.publication_intent_id),
                },
            )
        )


async def settle_reviewed_push_deliveries(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot, store: ArtifactStore | None
) -> None:
    """Acknowledge an expired reviewed push only after its receipt is canonical."""
    for source in await work.commands.list_outstanding_normal(
        run_id=paused.id, exclude_command_id=resume.id
    ):
        if source.command_type != "push_reviewed_pr":
            continue
        origin = await resumed_release_origin(work, source)
        await _reviewed_push_authority(work, store, paused, source, origin)
        if not await verify_reviewed_push_replay(source, work, paused=True):
            continue
        if await work.commands.complete_expired_observed_lease(source) is None:
            raise CommandRecoveryRequired("reviewed push command lease is active or changed")
        record = await work.releases.get_for_run(paused.id)
        assert record is not None
        await work.events.append(
            RunEvent(
                run_id=paused.id,
                run_version=paused.version,
                event_type="reviewed_push.acknowledged_on_resume",
                actor_class="worker",
                actor_id=resume.actor_id,
                payload={
                    "source_command_id": str(source.id),
                    "resume_command_id": str(resume.id),
                    "push_intent_id": str(record.reviewed_push_intent_id),
                },
            )
        )


def _payload(command: CommandEnvelope) -> dict[str, object]:
    return {key: value for key, value in command.payload.items() if key not in RESUME_FIELDS}


def _approval_id(command: CommandEnvelope) -> UUID:
    payload = _payload(command)
    expected = (
        {
            "pull_request_id",
            "node_id",
            "approval_id",
            "candidate_evidence_digest",
            "previous_head_sha",
            "remote_attempt",
        }
        if command.command_type == "push_reviewed_pr"
        else {"approval_id"}
    )
    if command.command_type == "observe_merge_queue":
        expected = set(QUEUE_OBSERVATION_FIELDS)
        if type(payload.get("poll")) is not int or int(str(payload["poll"])) < 1:
            raise CommandRecoveryRequired("queue resume poll differs")
    if (
        command.command_type not in _PHASES
        or command.payload_schema_version != 1
        or set(payload) != expected
    ):
        raise CommandRecoveryRequired("release resume command is invalid")
    if command.command_type == "push_reviewed_pr":
        remote_attempt = payload.get("remote_attempt")
        if type(remote_attempt) is not int or remote_attempt < 1:
            raise CommandRecoveryRequired("reviewed push resume is invalid")
    try:
        return UUID(str(payload["approval_id"]))
    except ValueError:
        raise CommandRecoveryRequired("release resume approval is invalid") from None


async def resumed_release_origin(
    work: UnitOfWork, command: CommandEnvelope, *, replaying_resume: UUID | None = None
) -> CommandEnvelope:
    """Return immutable original release authority after validating each resume receipt."""
    current = command
    while (ids := resume_command_ids(current.payload)) is not None:
        try:
            resume, source = await work.commands.get(ids[0]), await work.commands.get(ids[1])
        except CommandNotFound, KeyError:
            raise CommandRecoveryRequired("release resume source is missing") from None
        phase = _PHASES.get(current.command_type)
        events = await work.events.list_after(command.run_id, 0)
        resumed = [
            e
            for e in events
            if e.event_type == "run.resumed" and e.run_version == current.expected_run_version
        ]
        stopped = [
            e
            for e in events
            if e.event_type == "release.suspended"
            and e.run_version == resume.expected_run_version
            and e.payload.get("source_command_id") == str(source.id)
        ]
        if (
            phase is None
            or current.payload_schema_version != 1
            or current.actor_id is None
            or source.status is not CommandStatus.CANCELLED
            or source.payload_schema_version != 1
            or source.run_id != command.run_id
            or source.command_type != current.command_type
            or source.actor_id != current.actor_id
            or source.expected_run_version != resume.expected_run_version - 1
            or resume.run_id != command.run_id
            or resume.command_type != "resume"
            or resume.payload != {}
            or resume.payload_schema_version != 1
            or resume.actor_id is None
            or (
                resume.status is not CommandStatus.COMPLETED
                and not (resume.id == replaying_resume and resume.status is CommandStatus.LEASED)
            )
            or current.expected_run_version != resume.expected_run_version + 1
            or current.idempotency_key
            != f"{command.run_id}:resume:{resume.id}:{current.command_type}"
            or _payload(current) != _payload(source)
            or len(resumed) != 1
            or len(stopped) != 1
            or resumed[0].payload.get("command_id") != str(resume.id)
            or resumed[0].payload.get("restored_state") != phase.value
            or resumed[0].payload.get("continuation") != continuation_binding(current, source.id)
            or resumed[0].actor_class != "operator"
            or resumed[0].actor_id != resume.actor_id
            or resumed[0].payload_schema_version != 1
            or stopped[0].payload.get("source") != continuation_binding(source, source.id)
            or stopped[0].actor_class != "worker"
            or stopped[0].actor_id != resume.actor_id
            or stopped[0].payload.get("resume_command_id") != str(resume.id)
            or stopped[0].payload.get("phase") != current.command_type
            or stopped[0].payload_schema_version != 1
            or stopped[0].payload
            != {
                "source_command_id": str(source.id),
                "resume_command_id": str(resume.id),
                "pause_command_id": resumed[0].payload.get("pause_command_id"),
                "phase": current.command_type,
                "source": continuation_binding(source, source.id),
            }
        ):
            raise CommandRecoveryRequired("release resume authority differs")
        try:
            pause = await work.commands.get(UUID(str(resumed[0].payload.get("pause_command_id"))))
        except ValueError, CommandNotFound, KeyError:
            raise CommandRecoveryRequired("release resume pause is missing") from None
        paused = [
            e
            for e in events
            if e.event_type == "run.paused" and e.run_version == resume.expected_run_version
        ]
        if (
            pause.status is not CommandStatus.COMPLETED
            or pause.run_id != command.run_id
            or pause.command_type != "pause"
            or pause.payload != {}
            or pause.payload_schema_version != 1
            or pause.actor_id is None
            or pause.expected_run_version != source.expected_run_version
            or len(paused) != 1
            or paused[0].actor_class != "operator"
            or paused[0].actor_id != pause.actor_id
            or paused[0].payload_schema_version != 1
            or paused[0].payload
            != {
                "command_id": str(pause.id),
                "command_type": "pause",
                "command_payload": {},
                "expected_run_version": source.expected_run_version,
            }
        ):
            raise CommandRecoveryRequired("release resume pause differs")
        current = source
    if current.command_type == "update_base":
        if (
            current.payload_schema_version != 1
            or set(_payload(current))
            != {"observation_digest", "pull_request_id", "remote_attempt", "target_base_sha"}
            or type(current.payload.get("remote_attempt")) is not int
        ):
            raise CommandRecoveryRequired("base resume command is invalid")
    else:
        _approval_id(current)
    return current


async def resume_release(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot, pause: CommandEnvelope,
    *, store: ArtifactStore | None = None,
) -> tuple[CommandEnvelope, CommandEnvelope]:
    """Cancel one quiescent release command and queue its receipt-bound continuation."""
    sources = await work.commands.list_outstanding_normal(
        run_id=paused.id, exclude_command_id=resume.id
    )
    if len(sources) != 1 or _PHASES.get(sources[0].command_type) is not paused.suspended_state:
        raise CommandRecoveryRequired("release resume has no unique stopped command")
    source = sources[0]
    origin = await resumed_release_origin(work, source)
    is_base = source.command_type == "update_base"
    is_reviewed_push = source.command_type == "push_reviewed_pr"
    is_queue = source.command_type == "observe_merge_queue"
    if is_reviewed_push:
        await _reviewed_push_authority(work, store, paused, source, origin)
    if is_base:
        if store is None:
            raise CommandRecoveryRequired("base resume evidence store is absent")
        approved = await ApprovedPlanLoader(store).load(work, paused.id)
        record = await work.releases.get_for_run(paused.id)
        if record is None:
            raise CommandRecoveryRequired("base resume PR identity is absent")
        await base_update_origin(work, store, origin, approved, record)
        publication = await work.operations.get(record.publication_intent_id)
        try:
            approval_id = UUID(str(publication.request_payload.get("approval_id")))
        except ValueError:
            raise CommandRecoveryRequired("base resume approval is invalid") from None
    else:
        approval_id = _approval_id(source)
    approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
    gate = "pr" if source.command_type in {"publish_pr", "push_reviewed_pr", "update_base"} else "merge"
    if (
        not isinstance(approval, Approval)
        or approval.run_id != paused.id
        or approval.gate != gate
        or approval.policy_version != paused.policy_version
        or (
            not is_base
            and not is_reviewed_push
            and approval.authenticated_actor_id != source.actor_id
        )
        or source.actor_id is None
        or approval.invalidated_at is not None
        or (
            not is_base
            and not is_reviewed_push
            and not is_queue
            and origin.expected_run_version != approval.run_version + 1
        )
        or source.expected_run_version != pause.expected_run_version
    ):
        raise CommandRecoveryRequired("release resume approval differs")
    if is_queue:
        from forge.application.services.queue_resume import verify_queue_resume

        await verify_queue_resume(work, origin, approval)
    cancelled = (
        await work.commands.cancel_pending_unstarted(source)
        if source.status is CommandStatus.PENDING
        else await work.commands.cancel_expired_observed_lease(
            source, reason="release stopped for operator resume"
        )
    )
    if (
        cancelled is None
        or not (
            await work.runs.prove_quiescent(paused.id, exclude_command_id=resume.id)
        ).is_quiescent
    ):
        raise CommandRecoveryRequired("release resume has unresolved effects")
    payload = _payload(source) | {
        "resume_command_id": str(resume.id),
        "source_command_id": str(source.id),
    }
    await work.events.append(
        RunEvent(
            run_id=paused.id,
            run_version=paused.version,
            event_type="release.suspended",
            actor_class="worker",
            actor_id=resume.actor_id,
            payload={
                "source_command_id": str(source.id),
                "resume_command_id": str(resume.id),
                "pause_command_id": str(pause.id),
                "phase": source.command_type,
                "source": continuation_binding(source, source.id),
            },
        )
    )
    queued = await work.commands.enqueue(
        run_id=paused.id,
        command_type=source.command_type,
        idempotency_key=f"{paused.id}:resume:{resume.id}:{source.command_type}",
        payload=payload,
        expected_run_version=paused.version + 1,
        actor_id=source.actor_id,
    )
    return cancelled, queued
