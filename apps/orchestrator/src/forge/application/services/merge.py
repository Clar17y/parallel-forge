"""Intent-before-effect merge execution and observation-only crash recovery."""

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
from forge.domain.operation import (
    OperationIntent,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.release import GitHubPullRequest
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Approval
from forge.release.controller import ReleaseReconciliationRequired, _validate_intent
from forge.release.github_client import GitHubClientError
from forge.release.github_write import GitHubWriteError
from forge.release.merge import MergeController, MergeOperation, StaleMergeEvidence


class MergeService:
    def __init__(
        self,
        evidence: MergeEvidenceValidator,
        controller: MergeController,
        executor: OperationExecutor,
        *,
        clock: Clock | None = None,
        queue: GitHubMergeQueuePort | None = None,
    ) -> None:
        self._evidence, self._controller, self._executor = evidence, controller, executor
        self._clock = clock or SystemClock()
        self._queue = queue

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if (
            command.command_type != "merge_pr"
            or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1
            or set(command.payload) - RESUME_FIELDS != {"approval_id"}
        ):
            raise CommandRecoveryRequired("merge command is invalid")
        approval_id = UUID(str(command.payload["approval_id"]))
        await _fence_command(command, work)
        origin = await resumed_release_origin(work, command)
        run = await work.runs.get_for_update(command.run_id)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval)
            or approval.run_id != run.id
            or approval.gate != "merge"
            or approval.authenticated_actor_id != command.actor_id
            or approval.run_version + 1 != origin.expected_run_version
        ):
            raise CommandRecoveryRequired("merge command approval differs")
        await verify_merge_delivery(command, work, approval)
        record = await work.releases.get_for_run(run.id)
        if record is None:
            raise CommandRecoveryRequired("merge PR is absent")
        events = await work.events.list_after(run.id, 0)
        evidence_rejections = [
            e
            for e in events
            if e.event_type == "run.merge_evidence_rejected"
            and e.payload.get("source_command_id") == str(command.id)
        ]
        if evidence_rejections:
            event = evidence_rejections[0]
            await verify_merge_delivery(command, work, approval)
            if (
                len(evidence_rejections) != 1
                or run.state is not RunState.AWAITING_HUMAN_INTERVENTION
                or run.version != command.expected_run_version + 1
                or event.run_version != run.version
                or event.actor_class != "worker"
                or event.actor_id != command.actor_id
                or approval.invalidated_at is None
                or event.payload != _evidence_rejection_payload(command, approval, record.id)
            ):
                raise CommandRecoveryRequired("merge evidence rejection replay differs")
            await work.commit()
            return
        if self._queue is not None:
            # Historical evidence selects the route even after an uncertain
            # admission invalidated its gate. The selected service independently
            # checks current authority before permitting any new external effect.
            try:
                historical = await self._evidence.for_recovery(work, run.id, approval_id)
            except StaleMergeEvidence:
                raise CommandRecoveryRequired("merge routing evidence is unavailable") from None
            queued_mode = await self._evidence.queue_required(
                work, run.id, approval_id, historical
            )
            if queued_mode:
                from forge.application.services.queue_admission import QueueAdmissionService

                await QueueAdmissionService(
                    self._evidence, self._controller, self._queue, self._executor,
                    clock=self._clock,
                ).execute(command, work)
                return
        interventions = [
            e
            for e in events
            if e.event_type == "run.merge_intervention"
            and e.payload.get("source_command_id") == str(command.id)
        ]
        if interventions:
            event = interventions[0]
            intent = await work.operations.get_by_idempotency_key(
                str(event.payload.get("operation_key"))
            )
            if (
                len(interventions) != 1
                or intent is None
                or not _unresolved_matches(intent, approval)
                or run.state is not RunState.AWAITING_HUMAN_INTERVENTION
                or run.version != command.expected_run_version + 1
                or event.run_version != run.version
                or event.actor_class != "worker"
                or event.actor_id != command.actor_id
                or approval.invalidated_at is None
                or event.payload != _unresolved_payload(command, approval, record.id, intent)
            ):
                raise CommandRecoveryRequired("merge intervention replay differs")
            await work.commit()
            return
        rejected = [
            e
            for e in events
            if e.event_type == "run.merge_rejected"
            and e.payload.get("source_command_id") == str(command.id)
        ]
        if rejected:
            event = rejected[0]
            poll = event.payload.get("poll")
            queued = await work.commands.get_by_idempotency_key(
                str(event.payload.get("monitor_key"))
            )
            rejection_intent = await work.operations.get_by_idempotency_key(
                str(event.payload.get("operation_key"))
            )
            if (
                len(rejected) != 1
                or type(poll) is not int
                or poll < 1
                or run.state is not RunState.MONITORING_PR
                or run.version != command.expected_run_version + 1
                or event.run_version != run.version
                or event.actor_class != "worker"
                or event.actor_id != command.actor_id
                or approval.invalidated_at is None
                or queued is None
                or queued.command_type != "monitor_pr"
                or queued.idempotency_key != f"{run.id}:monitor-pr:{poll + 1}"
                or queued.payload != {"pull_request_id": str(record.id), "poll": poll + 1}
                or queued.expected_run_version != run.version
                or queued.actor_id != command.actor_id
                or event.payload.get("approval_id") != str(approval_id)
                or event.payload.get("pull_request_id") != str(record.id)
                or event.payload.get("monitor_command_id") != str(queued.id)
                or event.payload.get("evidence_digest") != approval.evidence_digest
                or not _rejection_matches(
                    rejection_intent, event.payload.get("merge_intent_id"), approval
                )
            ):
                raise CommandRecoveryRequired("rejected merge replay differs")
            await work.commit()
            return
        completed = [
            event
            for event in events
            if event.event_type == "run.merge_completed"
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if completed:
            event = completed[0]
            if (
                len(completed) != 1
                or run.state is not RunState.COMPLETED
                or run.version != command.expected_run_version + 1
                or event.run_version != run.version
                or event.actor_class != "worker"
                or event.actor_id != command.actor_id
                or event.payload
                != {
                    "source_command_id": str(command.id),
                    "approval_id": str(approval_id),
                    "pull_request_id": str(record.id),
                    "merge_intent_id": str(record.merge_intent_id),
                    "merge_sha": record.pull_request.merge_sha,
                }
                or not record.pull_request.merged
            ):
                raise CommandRecoveryRequired("merge completion replay differs")
            await work.commit()
            return
        if (
            run.state is not RunState.MERGING
            or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("merge awaits control reconciliation")
        try:
            approved = await self._evidence.consumed(work, run.id, approval_id, recheck=False)
        except StaleMergeEvidence:
            await _fence_command(command, work)
            current_run = await work.runs.get_for_update(run.id)
            if current_run != run or await pending_current_control_stop(work, current_run):
                raise CommandRecoveryRequired(
                    "merge evidence rejection awaits control reconciliation"
                ) from None
            await verify_merge_delivery(command, work, approval)
            await work.auth.invalidate_merge_gate(
                run_id=run.id, run_version=approval.run_version, at=self._clock.now()
            )
            # Without the frozen gate we cannot reconstruct the operation request
            # or safely assume that a previous delivery had no external effect.
            await work.runs.intervene(
                run.id,
                run.version,
                "run.merge_evidence_rejected",
                _evidence_rejection_payload(command, approval, record.id),
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=self._clock.now(),
            )
            await work.commit()
            return

        async def current() -> MergeApprovalEvidence:
            await _fence_command(command, work)
            current_run = await work.runs.get_for_update(run.id)
            if current_run != run or await pending_current_control_stop(work, current_run):
                raise CommandRecoveryRequired("merge admission awaits control reconciliation")
            result = await self._evidence.consumed(work, run.id, approval_id, recheck=True)
            await work.commit()
            return result

        adapter = MergeOperation(self._controller, record, approval_id, approved, current)
        existing = await work.operations.get_by_idempotency_key(adapter.request.idempotency_key)
        if existing is not None and existing.status is OperationStatus.FAILED:
            await self._reject_without_merge(
                command, work, run, approval, record.id, adapter.request
            )
            return
        if existing is None:
            try:
                await current()
            except (
                StaleMergeEvidence,
                GitHubClientError,
                GitHubWriteError,
                ReleaseReconciliationRequired,
            ):
                await self._reject_without_merge(
                    command, work, run, approval, record.id, adapter.request
                )
                return
        await _fence_command(command, work)
        current_run = await work.runs.get_for_update(run.id)
        if current_run != run or await pending_current_control_stop(work, current_run):
            raise CommandRecoveryRequired("merge admission awaits control reconciliation")
        intent = await work.operations.begin(
            run_id=run.id,
            operation_type=adapter.request.kind,
            idempotency_key=adapter.request.idempotency_key,
            request_digest=adapter.request.request_digest,
            request_payload=adapter.request.request_payload,
            execution_owner=f"forge-merge-{uuid4().hex}",
            execution_lease_seconds=30,
        )
        await work.commit()

        try:
            outcome = await self._executor.execute_admitted(intent, adapter)
        except ReleaseReconciliationRequired, GitHubWriteError, GitHubClientError:
            await self._intervene_unresolved(
                command, work, run, approval, record.id, adapter.request
            )
            return
        if outcome.status is OperationStatus.FAILED:
            await self._reject_without_merge(
                command, work, run, approval, record.id, adapter.request
            )
            return
        await _fence_command(command, work)
        current_run = await work.runs.get_for_update(run.id)
        if current_run != run or await pending_current_control_stop(work, current_run):
            raise CommandRecoveryRequired("merged outcome awaits control reconciliation")
        pull = GitHubPullRequest(**dict(outcome.payload))  # type: ignore[arg-type]
        recorded = await work.releases.record_merge(run.id, pull, intent.id)
        await work.runs.transition(
            run.id,
            run.version,
            RunState.COMPLETED,
            "run.merge_completed",
            {
                "source_command_id": str(command.id),
                "approval_id": str(approval_id),
                "pull_request_id": str(recorded.id),
                "merge_intent_id": str(intent.id),
                "merge_sha": pull.merge_sha,
            },
            actor_class="worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commit()

    async def _intervene_unresolved(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        run: RunSnapshot,
        approval: Approval,
        record_id: UUID,
        request: OperationRequest,
    ) -> None:
        await _fence_command(command, work)
        current = await work.runs.get_for_update(run.id)
        intent = await work.operations.get_by_idempotency_key(request.idempotency_key)
        if intent is None:
            raise CommandRecoveryRequired("merge intent is absent")
        _validate_intent(intent, request)
        if (
            current != run
            or await pending_current_control_stop(work, current)
            or not _unresolved_matches(intent, approval)
        ):
            raise CommandRecoveryRequired("merge uncertainty awaits reconciliation")
        await work.auth.invalidate_merge_gate(
            run_id=run.id, run_version=approval.run_version, at=self._clock.now()
        )
        await work.runs.intervene(
            run.id,
            run.version,
            "run.merge_intervention",
            _unresolved_payload(command, approval, record_id, intent),
            actor_class="worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commit()

    async def _reject_without_merge(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        run: RunSnapshot,
        approval: Approval,
        record_id: UUID,
        request: OperationRequest,
    ) -> None:
        await _fence_command(command, work)
        current = await work.runs.get_for_update(run.id)
        operation_key = request.idempotency_key
        intent = await work.operations.get_by_idempotency_key(operation_key)
        if intent is not None:
            _validate_intent(intent, request)
        if (
            current != run
            or await pending_current_control_stop(work, current)
            or not _rejection_matches(intent, str(intent.id) if intent else None, approval)
        ):
            raise CommandRecoveryRequired("stale merge settlement awaits reconciliation")
        events = await work.events.list_after(run.id, 0)
        polls = [e.payload.get("poll") for e in events if e.event_type == "run.pr_observed"]
        if not polls or any(type(p) is not int or p < 1 for p in polls):
            raise CommandRecoveryRequired("stale merge poll history differs")
        poll = max(int(str(p)) for p in polls)
        await work.auth.invalidate_merge_gate(
            run_id=run.id, run_version=approval.run_version, at=self._clock.now()
        )
        queued = await work.commands.enqueue(
            run_id=run.id,
            command_type="monitor_pr",
            idempotency_key=f"{run.id}:monitor-pr:{poll + 1}",
            payload={"pull_request_id": str(record_id), "poll": poll + 1},
            expected_run_version=run.version + 1,
            actor_id=command.actor_id,
            available_at=self._clock.now() + timedelta(seconds=15),
        )
        await work.runs.transition(
            run.id,
            run.version,
            RunState.MONITORING_PR,
            "run.merge_rejected",
            {
                "source_command_id": str(command.id),
                "approval_id": str(approval.id),
                "evidence_digest": approval.evidence_digest,
                "pull_request_id": str(record_id),
                "operation_key": operation_key,
                "merge_intent_id": str(intent.id) if intent else None,
                "poll": poll,
                "monitor_key": queued.idempotency_key,
                "monitor_command_id": str(queued.id),
                "reason": "merge_evidence_drift",
            },
            actor_class="worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commit()


def _rejection_matches(
    intent: OperationIntent | None, expected_id: object, approval: Approval
) -> bool:
    if intent is None:
        return expected_id is None
    request = intent.request_payload
    return (
        str(intent.id) == expected_id
        and intent.run_id == approval.run_id
        and intent.kind == "merge_pr"
        and intent.status is OperationStatus.FAILED
        and intent.error in {"merge_preflight_rejected", "merge_remote_rejected"}
        and request.get("approval_id") == str(approval.id)
        and request.get("approval_digest") == approval.evidence_digest
        and request.get("policy_version") == approval.policy_version
        and intent.request_digest == canonical_digest(request)
        and intent.idempotency_key == f"{approval.run_id}:merge_pr:{intent.request_digest}"
    )


def _unresolved_matches(intent: OperationIntent, approval: Approval) -> bool:
    request = intent.request_payload
    return (
        intent.run_id == approval.run_id
        and intent.kind == "merge_pr"
        and intent.status is OperationStatus.NEEDS_RECONCILIATION
        and intent.execution_owner is None
        and request.get("approval_id") == str(approval.id)
        and request.get("approval_digest") == approval.evidence_digest
        and request.get("policy_version") == approval.policy_version
        and intent.request_digest == canonical_digest(request)
        and intent.idempotency_key == f"{approval.run_id}:merge_pr:{intent.request_digest}"
    )


def _unresolved_payload(
    command: CommandEnvelope, approval: Approval, record_id: UUID, intent: OperationIntent
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "approval_id": str(approval.id),
        "evidence_digest": approval.evidence_digest,
        "pull_request_id": str(record_id),
        "operation_key": intent.idempotency_key,
        "merge_intent_id": str(intent.id),
        "reason": "merge_outcome_unresolved",
    }


def _evidence_rejection_payload(
    command: CommandEnvelope, approval: Approval, record_id: UUID
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "approval_id": str(approval.id),
        "evidence_digest": approval.evidence_digest,
        "pull_request_id": str(record_id),
        "reason": "merge_evidence_unavailable",
    }
