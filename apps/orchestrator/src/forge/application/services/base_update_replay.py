"""Canonical base-update settlement proof shared by replay and pause recovery."""

from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.release_resume import resumed_release_origin
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.release import ReleaseRecordConflict
from forge.persistence.repositories.runs import PersistenceDataError


async def verify_base_update_replay(
    command: CommandEnvelope, work: UnitOfWork, *, paused: bool = False
) -> bool:
    origin = await resumed_release_origin(work, command)
    run = await work.runs.get_for_update(command.run_id)
    record = await work.releases.get_for_run(run.id)
    if (
        record is None
        or command.command_type != "update_base"
        or command.actor_id is None
        or str(record.id) != command.payload.get("pull_request_id")
        or origin.idempotency_key
        != f"{run.id}:remote-remediation:{command.payload.get('remote_attempt')}"
    ):
        raise CommandRecoveryRequired("base update replay authority differs")
    events = await work.events.list_after(run.id, 0)
    settled = [
        e
        for e in events
        if e.event_type == "run.base_updated"
        and e.payload.get("source_command_id") == str(command.id)
    ]
    if settled:
        event = settled[0]
        queued = await work.commands.get_by_idempotency_key(
            str(event.payload.get("validation_key"))
        )
        if (
            len(settled) != 1
            or run.state is not (RunState.PAUSED if paused else RunState.VALIDATING)
            or (paused and run.suspended_state is not RunState.VALIDATING)
            or run.version != command.expected_run_version + (2 if paused else 1)
            or event.run_version != command.expected_run_version + 1
            or event.actor_id != command.actor_id
            or event.actor_class != "worker"
            or event.payload.get("update_intent_id") != str(record.base_update_intent_id)
            or event.payload.get("adoption_intent_id") != str(record.base_adoption_intent_id)
            or queued is None
            or queued.command_type != "validate"
            or queued.run_id != run.id
            or queued.payload_schema_version != 1
            or type(queued.payload.get("semantic_attempt")) is not int
            or int(str(queued.payload.get("semantic_attempt"))) < 1
            or queued.payload != {"semantic_attempt": queued.payload.get("semantic_attempt")}
            or queued.idempotency_key
            != f"{run.id}:validate:{queued.payload.get('semantic_attempt')}"
            or queued.actor_id != command.actor_id
            or queued.expected_run_version != event.run_version
            or event.payload.get("validation_command_id") != str(queued.id)
        ):
            raise CommandRecoveryRequired("base update replay differs")
        if (
            record.base_update_intent_id is None
            or record.base_adoption_intent_id is None
            or event.payload != {
                "source_command_id": str(command.id),
                "pull_request_id": str(record.id),
                "update_intent_id": str(record.base_update_intent_id),
                "adoption_intent_id": str(record.base_adoption_intent_id),
                "head_sha": record.pull_request.head_sha,
                "base_sha": record.pull_request.base_sha,
                "validation_key": queued.idempotency_key,
                "validation_command_id": str(queued.id),
            }
        ):
            raise CommandRecoveryRequired("base update replay evidence differs")
        try:
            updated = await work.operations.get(record.base_update_intent_id)
            if any(
                updated.request_payload.get(key) != value
                for key, value in {
                    "pull_request_id": str(record.id),
                    "observation_digest": command.payload["observation_digest"],
                    "remote_attempt": command.payload["remote_attempt"],
                    "base_sha": command.payload["target_base_sha"],
                }.items()
            ):
                raise CommandRecoveryRequired("base update replay authority differs")
            await work.releases.record_base_update(
                run.id, record.pull_request,
                record.base_update_intent_id, record.base_adoption_intent_id,
            )
        except ReleaseRecordConflict, PersistenceDataError:
            raise CommandRecoveryRequired("base update replay receipt differs") from None
        return True
    return False


async def settle_base_updates(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot, store: ArtifactStore | None
) -> None:
    """Acknowledge only a proved committed update whose delivery lease expired."""
    for source in await work.commands.list_outstanding_normal(
        run_id=paused.id, exclude_command_id=resume.id
    ):
        if source.command_type != "update_base":
            continue
        if not await verify_base_update_replay(source, work, paused=True):
            continue
        if store is None:
            raise CommandRecoveryRequired("settled base update evidence store is absent")
        approved = await ApprovedPlanLoader(store).load(work, paused.id)
        record = await work.releases.get_for_run(paused.id)
        assert record is not None  # Verified by the canonical replay proof above.
        publication = await work.operations.get(record.publication_intent_id)
        try:
            approval_id = UUID(str(publication.request_payload.get("approval_id")))
        except ValueError:
            raise CommandRecoveryRequired("settled base update approval is invalid") from None
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            source.actor_id != approved.approval_actor_id
            or not isinstance(approval, Approval)
            or approval.run_id != paused.id
            or approval.gate != "pr"
            or approval.policy_version != paused.policy_version
            or approval.invalidated_at is not None
            or approval.evidence_digest != publication.request_payload.get("approval_digest")
        ):
            raise CommandRecoveryRequired("settled base update approval differs")
        if await work.commands.complete_expired_observed_lease(source) is None:
            raise CommandRecoveryRequired("settled base update lease is active or changed")
        await work.events.append(
            RunEvent(
                run_id=paused.id,
                run_version=paused.version,
                event_type="base_update.acknowledged_on_resume",
                actor_class="worker",
                actor_id=resume.actor_id,
                payload={
                    "source_command_id": str(source.id),
                    "resume_command_id": str(resume.id),
                    "update_intent_id": str(record.base_update_intent_id),
                    "adoption_intent_id": str(record.base_adoption_intent_id),
                },
            )
        )
