"""Transactional queue admission and durable observation scheduling."""

from datetime import timedelta
from uuid import UUID, uuid4

from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.github_write import GitHubMergeQueuePort
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.merge_authority import verify_merge_delivery
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.recovery import OperationExecutor
from forge.application.services.validation import _fence_command
from forge.domain.approval import MergeApprovalEvidence
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.merge_queue import MergeQueueReceipt
from forge.domain.operation import OperationIntent, OperationStatus, canonical_digest
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.release.controller import _validate_intent
from forge.release.merge import MergeController
from forge.release.queue import EnqueueOperation


class QueueAdmissionService:
    def __init__(
        self, evidence: MergeEvidenceValidator, controller: MergeController,
        queue: GitHubMergeQueuePort, executor: OperationExecutor, *, clock: Clock | None = None,
    ) -> None:
        self._evidence, self._controller, self._queue = evidence, controller, queue
        self._executor = executor
        self._clock = clock or SystemClock()

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if (
            command.command_type != "merge_pr" or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1 or set(command.payload) != {"approval_id"}
        ):
            raise CommandRecoveryRequired("queue source command differs")
        try:
            approval_id = UUID(str(command.payload["approval_id"]))
        except ValueError:
            raise CommandRecoveryRequired("queue approval identifier differs") from None
        await _fence_command(command, work)
        run = await work.runs.get_for_update(command.run_id)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval) or approval.run_id != run.id
            or approval.gate != "merge" or approval.authenticated_actor_id != command.actor_id
            or approval.run_version + 1 != command.expected_run_version
        ):
            raise CommandRecoveryRequired("queue admission authority differs")
        await verify_merge_delivery(command, work, approval)
        rejections = [
            event for event in await work.events.list_after(run.id, 0)
            if event.event_type == "run.merge_queue_admission_rejected"
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if not rejections and (
            run.state is not RunState.MERGING or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("queue admission awaits control settlement")
        approved = (
            await self._evidence.for_recovery(work, run.id, approval_id)
            if rejections else await self._evidence.consumed(work, run.id, approval_id, recheck=False)
        )
        if not await self._evidence.queue_required(work, run.id, approval_id, approved):
            raise CommandRecoveryRequired("approval does not authorize queue admission")
        record = await work.releases.get_for_run(run.id)
        if record is None:
            raise CommandRecoveryRequired("queue PR identity is absent")
        deadline = await work.runs.duration_deadline(run.id)

        async def current() -> MergeApprovalEvidence:
            await _fence_command(command, work)
            latest = await work.runs.get_for_update(run.id)
            if (
                latest != run or await pending_current_control_stop(work, latest)
                or self._clock.now() >= deadline
            ):
                raise CommandRecoveryRequired("queue admission awaits control or deadline settlement")
            result = await self._evidence.consumed(work, run.id, approval_id, recheck=True)
            await work.commit()
            return result

        adapter = EnqueueOperation(self._controller, self._queue, record, approval_id, approved, current)
        request = adapter.request
        existing = await work.operations.get_by_idempotency_key(request.idempotency_key)
        if rejections:
            if existing is None:
                raise CommandRecoveryRequired("queue rejection receipt is absent")
            _validate_intent(existing, request)
            event = rejections[0]
            if (
                len(rejections) != 1 or not _conclusive_rejection(existing)
                or run.state is not RunState.AWAITING_HUMAN_INTERVENTION
                or run.version != command.expected_run_version + 1
                or approval.invalidated_at is None or event.run_version != run.version
                or event.actor_class != "worker" or event.actor_id != command.actor_id
                or event.payload != _rejection_payload(command, approval, existing)
            ):
                raise CommandRecoveryRequired("queue rejection replay differs")
            await work.commit()
            return
        if existing is None:
            await current()
        else:
            _validate_intent(existing, request)
            if existing.status is OperationStatus.FAILED:
                await self._settle_rejection(command, work, approval, adapter)
                return
        await _fence_command(command, work)
        latest = await work.runs.get_for_update(run.id)
        if latest != run or await pending_current_control_stop(work, latest):
            raise CommandRecoveryRequired("queue admission fence changed")
        intent = await work.operations.begin(
            run_id=run.id, operation_type=request.kind, idempotency_key=request.idempotency_key,
            request_digest=request.request_digest, request_payload=request.request_payload,
            execution_owner=f"forge-enqueue-{uuid4().hex}", execution_lease_seconds=30,
        )
        await work.commit()
        outcome = await self._executor.execute_admitted(intent, adapter)
        if outcome.status is OperationStatus.FAILED:
            await self._settle_rejection(command, work, approval, adapter)
            return
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise CommandRecoveryRequired("queue admission requires settlement")
        receipt = MergeQueueReceipt.model_validate(dict(outcome.payload))
        adapter.validate_receipt(receipt)
        if outcome.remote_resource_id != receipt.entry_id:
            raise CommandRecoveryRequired("queue receipt resource differs")
        await _fence_command(command, work)
        latest = await work.runs.get_for_update(run.id)
        if latest != run or await pending_current_control_stop(work, latest):
            raise CommandRecoveryRequired("queue receipt awaits control settlement")
        payload = {
            "source_command_id": str(command.id), "approval_id": str(approval_id),
            "enqueue_intent_id": str(intent.id), "receipt_digest": canonical_digest(receipt.model_dump()),
            "deadline": deadline.isoformat(), "poll": 1,
        }
        key = f"{run.id}:observe-merge-queue:{intent.id}:1"
        previous = [
            event for event in await work.events.list_after(run.id, 0)
            if event.event_type == "run.merge_queue_enqueued"
            and event.payload.get("source_command_id") == str(command.id)
        ]
        queued = await work.commands.get_by_idempotency_key(key)
        if previous:
            if (
                len(previous) != 1 or queued is None
                or queued.command_type != "observe_merge_queue" or queued.payload != payload
                or queued.actor_id != command.actor_id or queued.expected_run_version != run.version
                or previous[0].run_version != run.version or previous[0].actor_class != "worker"
                or previous[0].actor_id != command.actor_id
                or previous[0].payload != {**payload, "queued_command_id": str(queued.id), "queued_key": key}
            ):
                raise CommandRecoveryRequired("queue scheduling replay differs")
            await work.commit()
            return
        if queued is not None:
            raise CommandRecoveryRequired("queue observation has no causal admission event")
        queued = await work.commands.enqueue(
            run_id=run.id, command_type="observe_merge_queue", idempotency_key=key, payload=payload,
            expected_run_version=run.version, actor_id=command.actor_id,
            available_at=min(self._clock.now() + timedelta(seconds=15), deadline),
        )
        await work.events.append(RunEvent(
            run_id=run.id, run_version=run.version, event_type="run.merge_queue_enqueued",
            actor_class="worker", actor_id=command.actor_id, occurred_at=self._clock.now(),
            payload={**payload, "queued_command_id": str(queued.id), "queued_key": key},
        ))
        await work.commit()

    async def _settle_rejection(
        self, command: CommandEnvelope, work: UnitOfWork, approval: Approval, adapter: EnqueueOperation
    ) -> None:
        await _fence_command(command, work)
        run = await work.runs.get_for_update(command.run_id)
        intent = await work.operations.get_by_idempotency_key(adapter.request.idempotency_key)
        if intent is None:
            raise CommandRecoveryRequired("queue rejection operation is absent")
        _validate_intent(intent, adapter.request)
        if (
            not _conclusive_rejection(intent) or run.state is not RunState.MERGING
            or run.version != command.expected_run_version or approval.invalidated_at is not None
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("queue rejection settlement differs")
        await verify_merge_delivery(command, work, approval)
        await work.auth.invalidate_merge_gate(
            run_id=run.id, run_version=approval.run_version, at=self._clock.now()
        )
        await work.runs.intervene(
            run.id, run.version, "run.merge_queue_admission_rejected",
            _rejection_payload(command, approval, intent),
            actor_class="worker", actor_id=command.actor_id, occurred_at=self._clock.now(),
        )
        await work.commit()


def _conclusive_rejection(intent: OperationIntent) -> bool:
    return (
        intent.status is OperationStatus.FAILED
        and intent.error in {"queue_preflight_rejected", "queue_remote_rejected"}
        and intent.execution_owner is None
    )


def _rejection_payload(
    command: CommandEnvelope, approval: Approval, intent: OperationIntent
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id), "approval_id": str(approval.id),
        "evidence_digest": approval.evidence_digest, "enqueue_intent_id": str(intent.id),
        "operation_key": intent.idempotency_key, "reason": intent.error,
    }
