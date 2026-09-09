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
from forge.application.services.release_resume import resumed_release_origin
from forge.application.services.resume_source import RESUME_FIELDS
from forge.application.services.validation import _fence_command
from forge.domain.approval import MergeApprovalEvidence
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.merge_queue import MergeQueueReceipt
from forge.domain.operation import OperationIntent, OperationStatus, canonical_digest
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.release.controller import ReleaseReconciliationRequired, _validate_intent
from forge.release.github_client import GitHubClientError
from forge.release.github_write import GitHubWriteError
from forge.release.merge import MergeController, ObservedMergeOperation, StaleMergeEvidence
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
            or command.payload_schema_version != 1 or set(command.payload) - RESUME_FIELDS != {"approval_id"}
        ):
            raise CommandRecoveryRequired("queue source command differs")
        try:
            approval_id = UUID(str(command.payload["approval_id"]))
        except ValueError:
            raise CommandRecoveryRequired("queue approval identifier differs") from None
        await _fence_command(command, work)
        origin = await resumed_release_origin(work, command)
        run = await work.runs.get_for_update(command.run_id)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval) or approval.run_id != run.id
            or approval.gate != "merge" or approval.authenticated_actor_id != command.actor_id
            or approval.run_version + 1 != origin.expected_run_version
        ):
            raise CommandRecoveryRequired("queue admission authority differs")
        await verify_merge_delivery(command, work, approval)
        events = await work.events.list_after(run.id, 0)
        interventions = [
            event for event in events
            if event.event_type in {
                "run.merge_queue_admission_rejected", "run.merge_queue_admission_uncertain"
            }
            and event.payload.get("source_command_id") == str(command.id)
        ]
        completions = [event for event in events if event.event_type == "run.merge_completed"
                       and event.payload.get("source_command_id") == str(command.id)]
        if not (interventions or completions) and (
            run.state is not RunState.MERGING or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("queue admission awaits control settlement")
        approved = (
            await self._evidence.for_recovery(work, run.id, approval_id)
            if interventions or completions
            else await self._evidence.consumed(work, run.id, approval_id, recheck=False)
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
            ):
                raise CommandRecoveryRequired("queue admission awaits control settlement")
            if self._clock.now() >= deadline:
                raise StaleMergeEvidence()
            result = await self._evidence.consumed(work, run.id, approval_id, recheck=True)
            await work.commit()
            return result

        adapter = EnqueueOperation(self._controller, self._queue, record, approval_id, approved, current)
        request = adapter.request
        existing = await work.operations.get_by_idempotency_key(request.idempotency_key)
        if completions:
            if existing is None:
                raise CommandRecoveryRequired("resolved enqueue intent is absent")
            _validate_intent(existing, request)
            if (
                len(completions) != 1 or interventions or existing.status is not OperationStatus.SUCCEEDED
                or adapter.merged_pull(existing.to_outcome()) != record.pull_request
                or run.state is not RunState.COMPLETED or run.version != command.expected_run_version + 1
                or record.merge_intent_id is None or completions[0].run_version != run.version
                or completions[0].actor_class != "worker" or completions[0].actor_id != command.actor_id
                or completions[0].payload != {
                    "source_command_id": str(command.id), "approval_id": str(approval_id),
                    "pull_request_id": str(record.id), "merge_intent_id": str(record.merge_intent_id),
                    "merge_sha": record.pull_request.merge_sha,
                }
            ):
                raise CommandRecoveryRequired("resolved queue completion replay differs")
            await work.releases.record_merge(run.id, record.pull_request, record.merge_intent_id)
            await work.commit()
            return
        if interventions:
            if existing is None:
                raise CommandRecoveryRequired("queue rejection receipt is absent")
            _validate_intent(existing, request)
            event = interventions[0]
            uncertain = event.event_type == "run.merge_queue_admission_uncertain"
            if (
                len(interventions) != 1
                or (not uncertain and not _conclusive_rejection(existing))
                or (uncertain and (
                    existing.status not in {OperationStatus.NEEDS_RECONCILIATION, OperationStatus.SUCCEEDED}
                    or existing.execution_owner is not None
                ))
                or run.state is not RunState.AWAITING_HUMAN_INTERVENTION
                or run.version != command.expected_run_version + 1
                or approval.invalidated_at is None or event.run_version != run.version
                or event.actor_class != "worker" or event.actor_id != command.actor_id
                or event.payload != _intervention_payload(command, approval, existing, uncertain=uncertain)
            ):
                raise CommandRecoveryRequired("queue rejection replay differs")
            await work.commit()
            return
        preflight_rejected = False
        if existing is None:
            try:
                await current()
            except StaleMergeEvidence, GitHubClientError, GitHubWriteError:
                preflight_rejected = True
        else:
            _validate_intent(existing, request)
            if existing.status is OperationStatus.FAILED:
                await self._settle_intervention(command, work, approval, adapter)
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
        if preflight_rejected and intent.is_new:
            # This delivery has made no queue call. Never overwrite a pre-existing
            # intent: it may represent a durable effect from a competing delivery.
            await work.operations.fail(
                intent.id, error="queue_preflight_rejected", owner_id=intent.execution_owner
            )
            await work.commit()
            await self._settle_intervention(command, work, approval, adapter)
            return
        await work.commit()
        if intent.status is OperationStatus.FAILED:
            await self._settle_intervention(command, work, approval, adapter)
            return
        try:
            outcome = await self._executor.execute_admitted(intent, adapter)
        except ReleaseReconciliationRequired, GitHubWriteError, GitHubClientError:
            await self._settle_intervention(command, work, approval, adapter, uncertain=True)
            return
        if outcome.status is OperationStatus.FAILED:
            await self._settle_intervention(command, work, approval, adapter)
            return
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise CommandRecoveryRequired("queue admission requires settlement")
        merged = adapter.merged_pull(outcome)
        if merged is not None:
            await _fence_command(command, work)
            latest = await work.runs.get_for_update(run.id)
            if latest != run or await pending_current_control_stop(work, latest):
                raise CommandRecoveryRequired("resolved queue merge awaits control settlement")
            completion = ObservedMergeOperation(self._controller, record, approval_id, approved, current)
            final_request = completion.request
            final_intent = await work.operations.begin(
                run_id=run.id, operation_type=final_request.kind,
                idempotency_key=final_request.idempotency_key, request_digest=final_request.request_digest,
                request_payload=final_request.request_payload,
                execution_owner=f"forge-resolved-queue-{uuid4().hex}", execution_lease_seconds=30,
            )
            # The merged observation is already durable in the enqueue resolution.
            # This step is database-only and commits the canonical final receipt
            # together with the PR and run transition, without another remote read.
            if final_intent.is_new:
                await work.operations.complete(
                    final_intent.id, self._controller.outcome(record, approved, merged),
                    owner_id=final_intent.execution_owner,
                )
            elif final_intent.status is not OperationStatus.SUCCEEDED:
                raise CommandRecoveryRequired("existing merge completion requires recovery")
            await work.releases.record_merge(run.id, merged, final_intent.id)
            await work.runs.transition(
                run.id, run.version, RunState.COMPLETED, "run.merge_completed",
                {"source_command_id": str(command.id), "approval_id": str(approval_id),
                 "pull_request_id": str(record.id), "merge_intent_id": str(final_intent.id),
                 "merge_sha": merged.merge_sha},
                actor_class="worker", actor_id=command.actor_id, occurred_at=self._clock.now(),
            )
            await work.commit()
            return
        receipt = MergeQueueReceipt.model_validate(dict(outcome.payload))
        adapter.validate_receipt(receipt)
        if outcome.remote_resource_id != receipt.entry_id:
            raise CommandRecoveryRequired("queue receipt resource differs")
        await _fence_command(command, work)
        latest = await work.runs.get_for_update(run.id)
        if latest != run or await pending_current_control_stop(work, latest):
            raise CommandRecoveryRequired("queue receipt awaits control settlement")
        payload = {
            "merge_command_id": str(command.id), "approval_id": str(approval_id),
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
                or previous[0].payload != {**payload, "source_command_id": str(command.id),
                                          "queued_command_id": str(queued.id), "queued_key": key}
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
            payload={**payload, "source_command_id": str(command.id),
                     "queued_command_id": str(queued.id), "queued_key": key},
        ))
        await work.commit()

    async def _settle_intervention(
        self, command: CommandEnvelope, work: UnitOfWork, approval: Approval, adapter: EnqueueOperation,
        *, uncertain: bool = False,
    ) -> None:
        await _fence_command(command, work)
        run = await work.runs.get_for_update(command.run_id)
        intent = await work.operations.get_by_idempotency_key(adapter.request.idempotency_key)
        if intent is None:
            raise CommandRecoveryRequired("queue rejection operation is absent")
        _validate_intent(intent, adapter.request)
        matches = (
            intent.status is OperationStatus.NEEDS_RECONCILIATION and intent.execution_owner is None
            if uncertain else _conclusive_rejection(intent)
        )
        if (
            not matches or run.state is not RunState.MERGING
            or run.version != command.expected_run_version or approval.invalidated_at is not None
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("queue rejection settlement differs")
        await verify_merge_delivery(command, work, approval)
        await work.auth.invalidate_merge_gate(
            run_id=run.id, run_version=approval.run_version, at=self._clock.now()
        )
        await work.runs.intervene(
            run.id, run.version,
            "run.merge_queue_admission_uncertain" if uncertain else "run.merge_queue_admission_rejected",
            _intervention_payload(command, approval, intent, uncertain=uncertain),
            actor_class="worker", actor_id=command.actor_id, occurred_at=self._clock.now(),
        )
        await work.commit()


def _conclusive_rejection(intent: OperationIntent) -> bool:
    return (
        intent.status is OperationStatus.FAILED
        and intent.error in {"queue_preflight_rejected", "queue_remote_rejected"}
        and intent.execution_owner is None
    )


def _intervention_payload(
    command: CommandEnvelope, approval: Approval, intent: OperationIntent, *, uncertain: bool = False,
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id), "approval_id": str(approval.id),
        "evidence_digest": approval.evidence_digest, "enqueue_intent_id": str(intent.id),
        "operation_key": intent.idempotency_key,
        "reason": "queue_outcome_unresolved" if uncertain else intent.error,
    }
