"""Read-only authority checks before resuming a scheduled queue observation."""

from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.merge_authority import verify_merge_delivery
from forge.application.services.release_resume import resumed_release_origin
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.merge_queue import QUEUE_OBSERVATION_FIELDS, MergeQueueReceipt
from forge.domain.operation import OperationStatus, canonical_digest
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Approval


async def settle_queue_admission_deliveries(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot
) -> None:
    """Acknowledge an expired admission only after proving its scheduled successor."""
    if paused.suspended_state is not RunState.MERGING:
        return
    events = await work.events.list_after(paused.id, 0)
    for source in await work.commands.list_outstanding_normal(
        run_id=paused.id, exclude_command_id=resume.id
    ):
        if source.command_type != "merge_pr":
            continue
        receipts = [e for e in events if e.event_type == "run.merge_queue_enqueued"
                    and e.payload.get("source_command_id") == str(source.id)]
        if not receipts:
            continue
        if len(receipts) != 1 or source.status is not CommandStatus.LEASED:
            raise CommandRecoveryRequired("queue admission acknowledgement is ambiguous")
        receipt = receipts[0]
        try:
            observer_id = UUID(str(receipt.payload.get("queued_command_id")))
            approval_id = UUID(str(receipt.payload.get("approval_id")))
        except ValueError:
            raise CommandRecoveryRequired("queue admission acknowledgement identifiers differ") from None
        observer = await work.commands.get(observer_id)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval) or approval.run_id != paused.id
            or approval.gate != "merge" or approval.policy_version != paused.policy_version
            or approval.authenticated_actor_id != source.actor_id
            or approval.invalidated_at is not None
            or observer.run_id != paused.id or observer.command_type != "observe_merge_queue"
            or observer.payload_schema_version != 1 or observer.status is not CommandStatus.PENDING
            or observer.attempt != 0 or observer.actor_id != source.actor_id
            or set(observer.payload) != QUEUE_OBSERVATION_FIELDS
            or observer.payload.get("merge_command_id") != str(source.id)
            or observer.payload.get("poll") != 1
            or source.expected_run_version != paused.version - 1
            or observer.expected_run_version != source.expected_run_version
        ):
            raise CommandRecoveryRequired("queue admission acknowledgement authority differs")
        await verify_queue_resume(work, observer, approval)
        if await work.commands.complete_expired_observed_lease(source) is None:
            raise CommandRecoveryRequired("queue admission lease is active or changed")
        await work.events.append(RunEvent(
            run_id=paused.id, run_version=paused.version,
            event_type="queue_admission.acknowledged_on_resume", actor_class="worker",
            actor_id=resume.actor_id, payload={
                "source_command_id": str(source.id), "resume_command_id": str(resume.id),
                "admission_event_id": str(receipt.event_id), "queued_command_id": str(observer.id),
            },
        ))


async def verify_queue_resume(work: UnitOfWork, origin: CommandEnvelope, approval: Approval) -> None:
    try:
        merge_id = UUID(str(origin.payload["merge_command_id"]))
        enqueue_id = UUID(str(origin.payload["enqueue_intent_id"]))
    except ValueError, KeyError:
        raise CommandRecoveryRequired("queue resume identifiers differ") from None
    merge = await work.commands.get(merge_id)
    merge_origin = await resumed_release_origin(work, merge)
    if (
        merge.run_id != origin.run_id or merge.command_type != "merge_pr"
        or merge.actor_id != origin.actor_id or merge.expected_run_version > origin.expected_run_version
        or merge_origin.payload != {"approval_id": str(approval.id)}
        or merge_origin.expected_run_version != approval.run_version + 1
        or origin.payload["approval_id"] != str(approval.id)
    ):
        raise CommandRecoveryRequired("queue resume merge authority differs")
    await verify_merge_delivery(merge, work, approval)
    intent = await work.operations.get(enqueue_id)
    request = intent.request_payload
    if (
        intent.run_id != origin.run_id or intent.kind != "enqueue_pr"
        or intent.status is not OperationStatus.SUCCEEDED or intent.outcome is None
        or request.get("approval_id") != str(approval.id)
        or request.get("approval_digest") != approval.evidence_digest
        or request.get("policy_version") != approval.policy_version
        or intent.request_digest != canonical_digest(request)
        or intent.idempotency_key != f"{origin.run_id}:enqueue_pr:{intent.request_digest}"
    ):
        raise CommandRecoveryRequired("queue resume intent differs")
    try:
        receipt = MergeQueueReceipt.model_validate(dict(intent.outcome))
    except ValueError:
        raise CommandRecoveryRequired("queue resume receipt is invalid") from None
    if (
        intent.remote_resource_id != receipt.entry_id
        or request.get("repository") != receipt.repository
        or request.get("pull_request_number") != receipt.pull_request_number
        or request.get("node_id") != receipt.pull_request_node_id
        or request.get("head_sha") != receipt.head_sha
        or request.get("merge_method") != receipt.merge_method
        or canonical_digest(receipt.model_dump()) != origin.payload["receipt_digest"]
        or origin.payload["deadline"] != (await work.runs.duration_deadline(origin.run_id)).isoformat()
        or origin.idempotency_key != f"{origin.run_id}:observe-merge-queue:{intent.id}:{origin.payload['poll']}"
    ):
        raise CommandRecoveryRequired("queue resume receipt binding differs")
    parents = [e for e in await work.events.list_after(origin.run_id, 0)
               if e.event_type in {"run.merge_queue_enqueued", "run.merge_queue_observed"}
               and e.payload.get("queued_command_id") == str(origin.id)]
    if (
        len(parents) != 1 or parents[0].actor_class != "worker" or parents[0].actor_id != origin.actor_id
        or parents[0].run_version != origin.expected_run_version
        or parents[0].payload.get("queued_key") != origin.idempotency_key
        or {key: parents[0].payload.get(key) for key in QUEUE_OBSERVATION_FIELDS} != dict(origin.payload)
        or (origin.payload["poll"] == 1) != (parents[0].event_type == "run.merge_queue_enqueued")
    ):
        raise CommandRecoveryRequired("queue resume scheduling authority differs")
