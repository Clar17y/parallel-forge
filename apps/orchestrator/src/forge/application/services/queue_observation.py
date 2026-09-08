"""Receipt-bound durable queue polling and authoritative merge completion."""

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
from forge.domain.merge_queue import QUEUE_OBSERVATION_FIELDS, MergeQueueReceipt
from forge.domain.operation import (
    OperationStatus,
    canonical_digest,
)
from forge.domain.release import GitHubPullRequest
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.release.controller import ReleaseReconciliationRequired, _validate_intent
from forge.release.github_client import GitHubClientError
from forge.release.github_write import GitHubWriteError
from forge.release.merge import MergeController, ObservedMergeOperation, StaleMergeEvidence
from forge.release.queue import EnqueueOperation

_FIELDS = QUEUE_OBSERVATION_FIELDS


class QueueObservationService:
    def __init__(
        self, evidence: MergeEvidenceValidator, controller: MergeController,
        queue: GitHubMergeQueuePort, executor: OperationExecutor, *, clock: Clock | None = None,
    ) -> None:
        self._evidence, self._controller, self._queue = evidence, controller, queue
        self._executor = executor
        self._clock = clock or SystemClock()

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if (
            command.command_type != "observe_merge_queue" or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1 or set(command.payload) - RESUME_FIELDS != _FIELDS
            or type(command.payload["poll"]) is not int or command.payload["poll"] < 1
        ):
            raise CommandRecoveryRequired("queue observation command differs")
        poll = int(str(command.payload["poll"]))
        try:
            source_id = UUID(str(command.payload["merge_command_id"]))
            approval_id = UUID(str(command.payload["approval_id"]))
            enqueue_id = UUID(str(command.payload["enqueue_intent_id"]))
        except ValueError:
            raise CommandRecoveryRequired("queue observation identity differs") from None
        await _fence_command(command, work)
        origin = await resumed_release_origin(work, command)
        core_payload = {key: value for key, value in command.payload.items() if key not in RESUME_FIELDS}
        run = await work.runs.get_for_update(command.run_id)
        source = await work.commands.get(source_id)
        merge_origin = await resumed_release_origin(work, source)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval) or approval.run_id != run.id or approval.gate != "merge"
            or source.run_id != run.id or source.command_type != "merge_pr"
            or {key: value for key, value in source.payload.items() if key not in RESUME_FIELDS}
            != {"approval_id": str(approval_id)}
            or source.actor_id != command.actor_id or approval.authenticated_actor_id != command.actor_id
            or source.expected_run_version > origin.expected_run_version
            or approval.run_version + 1 != merge_origin.expected_run_version
        ):
            raise CommandRecoveryRequired("queue observation source differs")
        await verify_merge_delivery(source, work, approval)
        deadline = await work.runs.duration_deadline(run.id)
        if (
            command.payload["deadline"] != deadline.isoformat()
            or origin.idempotency_key != f"{run.id}:observe-merge-queue:{enqueue_id}:{poll}"
        ):
            raise CommandRecoveryRequired("queue observation deadline or key differs")
        events = await work.events.list_after(run.id, 0)
        parents = [e for e in events
                   if e.event_type in {"run.merge_queue_enqueued", "run.merge_queue_observed"}
                   and e.payload.get("queued_command_id") == str(origin.id)]
        if len(parents) != 1:
            raise CommandRecoveryRequired("queue observation has no causal scheduling event")
        parent = parents[0]
        if (
            parent.actor_class != "worker" or parent.actor_id != command.actor_id
            or parent.run_version != origin.expected_run_version
            or parent.payload.get("queued_key") != origin.idempotency_key
            or {key: parent.payload.get(key) for key in _FIELDS} != core_payload
            or (poll == 1) != (parent.event_type == "run.merge_queue_enqueued")
        ):
            raise CommandRecoveryRequired("queue observation scheduling binding differs")
        approved = await self._evidence.for_recovery(work, run.id, approval_id)
        if not await self._evidence.queue_required(work, run.id, approval_id, approved):
            raise CommandRecoveryRequired("queue observation approval mode differs")
        record = await work.releases.get_for_run(run.id)
        if record is None:
            raise CommandRecoveryRequired("queue observation PR is absent")

        async def no_new_authority() -> MergeApprovalEvidence:
            raise CommandRecoveryRequired("queue observation cannot authorize a mutation")

        enqueue = EnqueueOperation(self._controller, self._queue, record, approval_id, approved, no_new_authority)
        intent = await work.operations.get(enqueue_id)
        _validate_intent(intent, enqueue.request)
        if intent.status is not OperationStatus.SUCCEEDED or intent.outcome is None:
            raise CommandRecoveryRequired("queue admission receipt is absent")
        receipt = MergeQueueReceipt.model_validate(dict(intent.outcome))
        enqueue.validate_receipt(receipt)
        if (
            intent.remote_resource_id != receipt.entry_id
            or canonical_digest(receipt.model_dump()) != command.payload["receipt_digest"]
        ):
            raise CommandRecoveryRequired("queue observation receipt differs")
        settled = [e for e in events if e.event_type == "run.merge_queue_poll_settled"
                   and e.payload.get("observation_command_id") == str(command.id)]
        if settled:
            event = settled[0]
            if (
                len(settled) != 1 or event.actor_class != "worker" or event.actor_id != command.actor_id
                or event.run_version != run.version or event.payload.get("target") != run.state.value
                or event.payload.get("command_payload") != command.payload
            ):
                raise CommandRecoveryRequired("queue observation replay differs")
            await work.commit()
            return
        if (
            run.state is not RunState.MERGING or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("queue observation awaits control settlement")
        completion = ObservedMergeOperation(self._controller, record, approval_id, approved, no_new_authority)
        request = completion.request
        completed_intent = await work.operations.get_by_idempotency_key(request.idempotency_key)
        completed_pull = None
        if completed_intent is not None:
            _validate_intent(completed_intent, request)
            if completed_intent.status is OperationStatus.SUCCEEDED:
                completed_pull = GitHubPullRequest(**dict(completed_intent.outcome or {}))  # type: ignore[arg-type]
                canonical = self._controller.outcome(record, approved, completed_pull)
                if (
                    canonical.payload != completed_intent.outcome
                    or canonical.remote_resource_id != completed_intent.remote_resource_id
                ):
                    raise CommandRecoveryRequired("completed queue merge receipt differs")
        await work.commit()
        pull = None
        reason = "queue_approval_invalidated" if approval.invalidated_at is not None else None
        if reason is None:
            try:
                # A canonical persisted completion survives GitHub outages after a crash.
                pull = completed_pull or await self._controller.observe_pull(record, approved)
                if not pull.merged:
                    if pull.state != "open":
                        reason = "queue_pull_request_closed"
                    elif self._clock.now() >= deadline:
                        reason = "queue_duration_exhausted"
                    elif not await self._controller.queue_required(approved):
                        reason = "queue_protection_changed"
                    else:
                        observed = await self._queue.observe(
                            approved.repository, approved.pull_request_number,
                            receipt.pull_request_node_id, approved.head_sha,
                        )
                        if observed != receipt:
                            reason = "queue_entry_changed_or_removed"
            except StaleMergeEvidence:
                reason = "queue_evidence_drift"
            except (GitHubWriteError, GitHubClientError) as error:
                if error.category not in {"unavailable", "rate_limited"}:
                    reason = "queue_observation_unverified"
                elif self._clock.now() >= deadline:
                    reason = "queue_duration_exhausted"
        await _fence_command(command, work)
        latest = await work.runs.get_for_update(run.id)
        if latest != run or await pending_current_control_stop(work, latest):
            raise CommandRecoveryRequired("queue observation settlement fence changed")
        if pull is not None and pull.merged:
            merged_intent = await work.operations.begin(
                run_id=run.id, operation_type=request.kind, idempotency_key=request.idempotency_key,
                request_digest=request.request_digest, request_payload=request.request_payload,
                execution_owner=f"forge-queue-completion-{uuid4().hex}", execution_lease_seconds=30,
            )
            await work.commit()
            try:
                outcome = await self._executor.execute_admitted(merged_intent, completion)
            except (GitHubWriteError, GitHubClientError, StaleMergeEvidence, ReleaseReconciliationRequired):
                await _fence_command(command, work)
                if await work.runs.get_for_update(run.id) != run or await pending_current_control_stop(work, run):
                    raise CommandRecoveryRequired("queue completion failure fence changed") from None
                unresolved = await work.operations.get(merged_intent.id)
                _validate_intent(unresolved, request)
                if unresolved.status is not OperationStatus.NEEDS_RECONCILIATION or unresolved.execution_owner is not None:
                    raise CommandRecoveryRequired("queue completion failure receipt differs") from None
                reason = "queue_completion_unresolved"
            else:
                await _fence_command(command, work)
                if await work.runs.get_for_update(run.id) != run or await pending_current_control_stop(work, run):
                    raise CommandRecoveryRequired("queue completion fence changed")
                merged = GitHubPullRequest(**dict(outcome.payload))  # type: ignore[arg-type]
                await work.releases.record_merge(run.id, merged, merged_intent.id)
                latest = await work.runs.transition(
                    run.id, run.version, RunState.COMPLETED, "run.merge_completed",
                    {"source_command_id": str(source.id), "approval_id": str(approval_id),
                     "pull_request_id": str(record.id), "merge_intent_id": str(merged_intent.id),
                     "merge_sha": merged.merge_sha},
                    actor_class="worker", actor_id=command.actor_id, occurred_at=self._clock.now(),
                )
        if reason is not None:
            await work.auth.invalidate_merge_gate(run_id=run.id, run_version=approval.run_version, at=self._clock.now())
            latest = await work.runs.intervene(
                run.id, run.version, "run.merge_queue_intervention",
                {"observation_command_id": str(command.id), "enqueue_intent_id": str(enqueue_id), "reason": reason,
                 **({"merge_intent_id": str(merged_intent.id)} if reason == "queue_completion_unresolved" else {})},
                actor_class="worker", actor_id=command.actor_id, occurred_at=self._clock.now(),
            )
        elif latest.state is not RunState.COMPLETED:
            payload = {**core_payload, "poll": poll + 1}
            key = f"{run.id}:observe-merge-queue:{enqueue_id}:{poll + 1}"
            queued = await work.commands.enqueue(
                run_id=run.id, command_type="observe_merge_queue", idempotency_key=key, payload=payload,
                expected_run_version=run.version, actor_id=command.actor_id,
                available_at=min(self._clock.now() + timedelta(seconds=min(120, 15 * 2 ** min(poll, 3))), deadline),
            )
            await work.events.append(RunEvent(
                run_id=run.id, run_version=run.version, event_type="run.merge_queue_observed",
                actor_class="worker", actor_id=command.actor_id, occurred_at=self._clock.now(),
                payload={**payload, "queued_command_id": str(queued.id), "queued_key": key},
            ))
        await work.events.append(RunEvent(
            run_id=run.id, run_version=latest.version, event_type="run.merge_queue_poll_settled",
            actor_class="worker", actor_id=command.actor_id, occurred_at=self._clock.now(),
            payload={"observation_command_id": str(command.id), "command_payload": dict(command.payload),
                     "target": latest.state.value},
        ))
        await work.commit()
