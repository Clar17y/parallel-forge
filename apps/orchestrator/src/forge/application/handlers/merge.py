"""Consume one stored human merge approval after exact evidence revalidation."""

from datetime import timedelta
from uuid import UUID

from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.validation import _fence_command
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.release.merge import StaleMergeEvidence


class ApproveMergeHandler:
    def __init__(self, evidence: MergeEvidenceValidator, *, clock: Clock | None = None) -> None:
        self._evidence, self._clock = evidence, clock or SystemClock()

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if (
            command.command_type != "approve_merge"
            or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1
            or command.actor_id is None
            or set(command.payload) != {"approval_id"}
        ):
            raise CommandRecoveryRequired("merge approval command is invalid")
        try:
            approval_id = UUID(str(command.payload["approval_id"]))
        except ValueError:
            raise CommandRecoveryRequired("merge approval identity is invalid") from None
        await _fence_command(command, work)
        run = await work.runs.get_for_update(command.run_id)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval)
            or approval.run_id != run.id
            or approval.gate != "merge"
            or approval.authenticated_actor_id != command.actor_id
            or approval.run_version != command.expected_run_version
        ):
            raise CommandRecoveryRequired("merge approval authority differs")
        events = await work.events.list_after(run.id, 0)
        settled = [
            e
            for e in events
            if e.event_type == "run.merge_approval_consumed"
            and e.payload.get("source_command_id") == str(command.id)
        ]
        if settled:
            event = settled[0]
            queued = await work.commands.get_by_idempotency_key(
                str(event.payload.get("queued_key"))
            )
            if (
                len(settled) != 1
                or event.actor_id != command.actor_id
                or event.actor_class != "worker"
                or event.payload.get("approval_id") != str(approval_id)
                or event.payload.get("evidence_digest") != approval.evidence_digest
                or event.run_version != command.expected_run_version + 1
                or run.version != event.run_version
                or event.payload.get("target") != run.state.value
                or event.payload.get("invalidated") != (approval.invalidated_at is not None)
                or queued is None
                or event.payload.get("queued_command_id") != str(queued.id)
                or event.payload.get("queued_type") != queued.command_type
                or event.payload.get("queued_payload") != queued.payload
                or queued.expected_run_version != run.version
                or queued.actor_id != command.actor_id
            ):
                raise CommandRecoveryRequired("merge approval replay differs")
            await work.commit()
            return
        if (
            approval.invalidated_at is not None
            or approval.policy_version != run.policy_version
            or run.state is not RunState.AWAITING_MERGE_APPROVAL
            or run.version != approval.run_version
            or run.pending_evidence_digest != approval.evidence_digest
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("merge approval is not current")
        stale = False
        try:
            await self._evidence.validate(work, run.id)
        except StaleMergeEvidence:
            stale = True
        await _fence_command(command, work)
        current = await work.runs.get_for_update(run.id)
        if current != run or await pending_current_control_stop(work, current):
            raise CommandRecoveryRequired("merge approval settlement awaits control reconciliation")
        payload: dict[str, object] = {
            "source_command_id": str(command.id),
            "approval_id": str(approval_id),
            "evidence_digest": approval.evidence_digest,
            "invalidated": stale,
        }
        if stale:
            await work.auth.invalidate_merge_gate(
                run_id=run.id, run_version=run.version, at=self._clock.now()
            )
            record = await work.releases.get_for_run(run.id)
            polls = [e.payload.get("poll") for e in events if e.event_type == "run.pr_observed"]
            if record is None or not polls or any(type(p) is not int or p < 1 for p in polls):
                raise CommandRecoveryRequired("merge approval poll history differs")
            poll = max(int(str(p)) for p in polls)
            queued = await work.commands.enqueue(
                run_id=run.id,
                command_type="monitor_pr",
                idempotency_key=f"{run.id}:monitor-pr:{poll + 1}",
                payload={"pull_request_id": str(record.id), "poll": poll + 1},
                expected_run_version=run.version + 1,
                actor_id=command.actor_id,
                available_at=self._clock.now() + timedelta(seconds=15),
            )
            payload.update(
                {
                    "poll": poll,
                    "pull_request_id": str(record.id),
                    "monitor_command_id": str(queued.id),
                    "reason": "merge_evidence_drift",
                }
            )
            target, event_type = RunState.MONITORING_PR, "approval.stale"
        else:
            queued = await work.commands.enqueue(
                run_id=run.id,
                command_type="merge_pr",
                idempotency_key=f"{run.id}:merge-pr:{run.version + 1}",
                payload={"approval_id": str(approval_id)},
                expected_run_version=run.version + 1,
                actor_id=command.actor_id,
            )
            target, event_type = RunState.MERGING, "run.merge_approved"
        payload.update(
            {
                "queued_command_id": str(queued.id),
                "queued_key": queued.idempotency_key,
                "queued_type": queued.command_type,
                "queued_payload": dict(queued.payload),
                "target": target.value,
            }
        )
        changed = await work.runs.transition(
            run.id,
            run.version,
            target,
            event_type,
            payload,
            actor_class="operator" if not stale else "worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.events.append(
            RunEvent(
                run_id=run.id,
                run_version=changed.version,
                event_type="run.merge_approval_consumed",
                payload=payload,
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=self._clock.now(),
            )
        )
        await work.commit()
