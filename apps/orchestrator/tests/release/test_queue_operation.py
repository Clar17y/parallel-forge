from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from forge.domain.merge_queue import MergeQueueReceipt
from forge.domain.operation import OperationStatus, canonical_digest
from forge.release.controller import ReleaseReconciliationRequired
from forge.release.merge import MergeController, StaleMergeEvidence
from forge.release.queue import EnqueueOperation

from apps.orchestrator.tests.release.test_merge_controller import ready
from apps.orchestrator.tests.release.test_publication_operations import intent


class Queue:
    receipt = None
    writes = 0
    crash = False

    async def enqueue(self, repository, number, node_id, head_sha, merge_method, correlation_id):
        self.writes += 1
        self.receipt = MergeQueueReceipt(
            repository=repository, pull_request_number=number,
            pull_request_node_id=node_id, head_sha=head_sha,
            merge_method=merge_method, entry_id="MQ_1",
        )
        if self.crash:
            raise RuntimeError("simulated loss after acceptance")
        return self.receipt

    async def observe(self, repository, number, node_id, head_sha):
        return self.receipt


def queued():
    read, write, evidence, record = ready()
    key = evidence.repository.casefold(), "main"
    protection = replace(read.merge_protections[key], merge_queue_enabled=True,
                         merge_queue_method=evidence.merge_method)
    read.merge_protections[key] = protection
    evidence = evidence.model_copy(update={"protection_digest": canonical_digest(asdict(protection))})
    return read, write, evidence, record


async def test_enqueue_receipt_is_distinct_from_completed_merge_and_recovery_never_writes():
    read, write, evidence, record = queued()
    queue = Queue()

    async def current():
        return evidence

    operation = EnqueueOperation(MergeController(read, write), queue, record, uuid4(), evidence, current)
    assert operation.request.kind == "enqueue_pr"
    admitted = intent(operation.request)
    queue.crash = True
    with pytest.raises(RuntimeError):
        await operation.invoke(admitted)
    recovered = await operation.reconcile(admitted)
    assert recovered.status is OperationStatus.SUCCEEDED
    assert recovered.payload["entry_id"] == "MQ_1"
    assert "merged" not in recovered.payload
    assert queue.writes == 1
    assert not write.pull_requests[evidence.repository, evidence.pull_request_number].merged


async def test_unknown_enqueue_without_entry_retains_uncertainty():
    read, write, evidence, record = queued()
    queue = Queue()

    async def no_authority():
        raise AssertionError("recovery must not reauthorize")

    operation = EnqueueOperation(MergeController(read, write), queue, record, uuid4(), evidence, no_authority)
    with pytest.raises(ReleaseReconciliationRequired):
        await operation.reconcile(intent(operation.request))
    assert queue.writes == 0


async def test_queue_required_branch_never_uses_direct_merge():
    read, write, evidence, _record = queued()
    with pytest.raises(StaleMergeEvidence):
        await MergeController(read, write).merge(evidence)
    assert not write.pull_requests[evidence.repository, evidence.pull_request_number].merged


async def test_strict_only_protection_rejects_enqueue_without_effect():
    read, write, evidence, record = ready()
    queue = Queue()

    async def current():
        return evidence

    operation = EnqueueOperation(MergeController(read, write), queue, record, uuid4(), evidence, current)
    result = await operation.invoke(intent(operation.request))
    assert result.status is OperationStatus.FAILED
    assert result.error == "queue_preflight_rejected"
    assert queue.writes == 0


@pytest.mark.parametrize("field,value", [
    ("repository", "other/repo"), ("pull_request_number", 99),
    ("pull_request_node_id", "PR_other"), ("head_sha", "c" * 40),
    ("merge_method", "rebase"),
])
async def test_recovery_rejects_queue_receipt_for_different_authority(field, value):
    read, write, evidence, record = queued()
    queue = Queue()
    queue.receipt = MergeQueueReceipt(
        repository=evidence.repository, pull_request_number=evidence.pull_request_number,
        pull_request_node_id=record.pull_request.node_id, head_sha=evidence.head_sha,
        merge_method=evidence.merge_method, entry_id="MQ_1",
    ).model_copy(update={field: value})

    async def current():
        raise AssertionError("read-only recovery cannot acquire new authority")

    operation = EnqueueOperation(MergeController(read, write), queue, record, uuid4(), evidence, current)
    with pytest.raises(ReleaseReconciliationRequired):
        await operation.reconcile(intent(operation.request))
    assert queue.writes == 0
