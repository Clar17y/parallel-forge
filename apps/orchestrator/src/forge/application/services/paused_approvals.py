"""Retire obsolete release approvals while restoring an exactly paused gate."""

from datetime import UTC, datetime
from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.state_engine import StateEngine
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import CommandNotFound


async def approval_gate_origin(
    work: UnitOfWork, run_id: UUID, version: int, gate: str, digest: str | None
) -> int:
    """Follow explicit pause/resume receipts without transferring old approvals."""
    if gate not in {"pr", "merge"}:
        raise CommandRecoveryRequired("restored approval provenance gate is invalid")
    while True:
        events = await work.events.list_for_version(run_id, version)
        resumed = [event for event in events if event.event_type == "run.resumed"]
        if not resumed:
            return version
        if len(resumed) != 1 or version < 2:
            raise CommandRecoveryRequired("restored approval provenance is ambiguous")
        event = resumed[0]
        try:
            resume = await work.commands.get(UUID(str(event.payload.get("command_id"))))
            pause = await work.commands.get(UUID(str(event.payload.get("pause_command_id"))))
        except ValueError, CommandNotFound:
            raise CommandRecoveryRequired("restored approval provenance is invalid") from None
        pauses = [
            item
            for item in await work.events.list_for_version(run_id, version - 1)
            if item.event_type == "run.paused"
        ]
        if (
            resume.run_id != run_id
            or resume.command_type != "resume"
            or resume.status is not CommandStatus.COMPLETED
            or resume.expected_run_version != version - 1
            or resume.payload_schema_version != 1
            or resume.payload != {}
            or resume.actor_id is None
            or event.actor_class != "operator"
            or event.actor_id != resume.actor_id
            or event.payload.get("restored_state")
            != (
                RunState.AWAITING_PR_APPROVAL.value
                if gate == "pr"
                else RunState.AWAITING_MERGE_APPROVAL.value
            )
            or event.payload.get("approval_gate")
            != {"gate": gate, "evidence_digest": digest, "source_version": version - 2}
            or pause.run_id != run_id
            or pause.command_type != "pause"
            or pause.status is not CommandStatus.COMPLETED
            or pause.expected_run_version != version - 2
            or pause.payload_schema_version != 1
            or pause.payload != {}
            or pause.actor_id is None
            or len(pauses) != 1
            or pauses[0].actor_class != "operator"
            or pauses[0].actor_id != pause.actor_id
            or pauses[0].payload
            != {
                "command_id": str(pause.id),
                "command_type": "pause",
                "command_payload": {},
                "expected_run_version": version - 2,
            }
        ):
            raise CommandRecoveryRequired("restored approval provenance differs")
        version -= 2


async def settle_paused_approvals(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot, pause: CommandEnvelope
) -> None:
    """The caller holds the run lock and has verified the resume and pause leases.

    Approval handlers have no external effects before consuming their gate. A
    still-paused approval gate therefore allows its old delivery to be retired,
    but never an active lease or a delivery from a different gate/version.
    All changes share the caller's restoration transaction.
    """
    restored = StateEngine().resume(paused)
    gate = {
        RunState.AWAITING_PR_APPROVAL: "pr",
        RunState.AWAITING_MERGE_APPROVAL: "merge",
    }.get(restored.state)
    if gate is None:
        return
    now = datetime.now(UTC)
    for source in await work.commands.list_outstanding_normal(
        run_id=paused.id, exclude_command_id=resume.id
    ):
        if source.command_type != f"approve_{gate}":
            raise CommandRecoveryRequired("paused approval has unrelated outstanding work")
        if (
            source.expected_run_version != pause.expected_run_version
            or source.run_id != paused.id
            or source.actor_id is None
            or source.payload_schema_version != 1
            or set(source.payload) != {"approval_id"}
        ):
            raise CommandRecoveryRequired("paused approval delivery authority differs")
        try:
            approval_id = UUID(str(source.payload["approval_id"]))
        except ValueError:
            raise CommandRecoveryRequired("paused approval identity is invalid") from None
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval)
            or approval.run_id != paused.id
            or approval.gate != gate
            or approval.run_version != source.expected_run_version
            or approval.policy_version != paused.policy_version
            or approval.authenticated_actor_id != source.actor_id
            or approval.evidence_digest != restored.pending_evidence_digest
        ):
            raise CommandRecoveryRequired("paused approval evidence authority differs")
        stopped = (
            await work.commands.cancel_pending_unstarted(source)
            if source.status is CommandStatus.PENDING
            else await work.commands.cancel_expired_observed_lease(
                source, reason="approval superseded by paused gate restoration"
            )
        )
        if stopped is None:
            raise CommandRecoveryRequired("paused approval delivery is still active or changed")
        await work.events.append(
            RunEvent(
                run_id=paused.id,
                run_version=paused.version,
                event_type="approval.superseded_by_pause",
                actor_class="worker",
                actor_id=resume.actor_id,
                occurred_at=now,
                payload={
                    "command_id": str(source.id),
                    "approval_id": str(approval_id),
                    "evidence_digest": approval.evidence_digest,
                    "approval_run_version": approval.run_version,
                    "pause_command_id": str(pause.id),
                    "resume_command_id": str(resume.id),
                },
            )
        )
    invalidate = work.auth.invalidate_pr_gate if gate == "pr" else work.auth.invalidate_merge_gate
    await invalidate(run_id=paused.id, run_version=pause.expected_run_version, at=now)
