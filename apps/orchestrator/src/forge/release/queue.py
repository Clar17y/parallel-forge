"""Durable enqueue adapter: success records admission, never completed merge."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID

from forge.application.ports.github_write import GitHubMergeQueuePort
from forge.application.ports.release import ReleaseRecord
from forge.domain.approval import MergeApprovalEvidence
from forge.domain.merge_queue import MergeQueueReceipt
from forge.domain.operation import OperationIntent, OperationOutcome, OperationStatus
from forge.release.controller import ReleaseReconciliationRequired, _validate_intent
from forge.release.github_client import GitHubClientError
from forge.release.github_write import GitHubWriteError
from forge.release.merge import MergeController, MergeOperation, StaleMergeEvidence


class EnqueueOperation:
    def __init__(
        self, controller: MergeController, queue: GitHubMergeQueuePort,
        record: ReleaseRecord, approval_id: UUID, approved: MergeApprovalEvidence,
        current_evidence: Callable[[], Awaitable[MergeApprovalEvidence]],
    ) -> None:
        self._controller, self._queue, self._record = controller, queue, record
        self._approved, self._current = approved, current_evidence
        # Identical evidence binding, separately namespaced effect and receipt.
        merge = MergeOperation(controller, record, approval_id, approved, current_evidence)
        self.request = replace(
            merge.request, kind="enqueue_pr",
            idempotency_key=f"{record.run_id}:enqueue_pr:{merge.request.request_digest}",
        )

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        try:
            await self._controller.preflight(self._record, self._approved, await self._current())
            if not await self._controller.queue_required(self._approved):
                raise StaleMergeEvidence()
        except StaleMergeEvidence, GitHubClientError, GitHubWriteError:
            return OperationOutcome(status=OperationStatus.FAILED, error="queue_preflight_rejected")
        approved = self._approved
        try:
            receipt = await self._queue.enqueue(
                approved.repository, approved.pull_request_number,
                self._record.pull_request.node_id, approved.head_sha,
                approved.merge_method, str(intent.id),
            )
        except GitHubWriteError as error:
            if error.category not in {"stale", "rejected"}:
                raise
            return OperationOutcome(status=OperationStatus.FAILED, error="queue_remote_rejected")
        return self._outcome(receipt)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        approved = self._approved
        receipt = await self._queue.observe(
            approved.repository, approved.pull_request_number,
            self._record.pull_request.node_id, approved.head_sha,
        )
        if receipt is None:
            # Absence cannot distinguish never accepted, evicted, or already merged.
            # A separate authoritative merge observation must settle completion.
            raise ReleaseReconciliationRequired()
        return self._outcome(receipt)

    def _outcome(self, receipt: MergeQueueReceipt) -> OperationOutcome:
        self.validate_receipt(receipt)
        return OperationOutcome(remote_resource_id=receipt.entry_id, payload=receipt.model_dump())

    def validate_receipt(self, receipt: MergeQueueReceipt) -> None:
        approved = self._approved
        if (
            receipt.repository != approved.repository
            or receipt.pull_request_number != approved.pull_request_number
            or receipt.pull_request_node_id != self._record.pull_request.node_id
            or receipt.head_sha != approved.head_sha
            or receipt.merge_method != approved.merge_method
        ):
            raise ReleaseReconciliationRequired()
