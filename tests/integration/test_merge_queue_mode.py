from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from forge.application.handlers.merge import ApproveMergeHandler
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.queue_admission import QueueAdmissionService
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.operation import OperationStatus
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.merge import MergeController
from test_pr_monitoring import published
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

from apps.orchestrator.tests.release.test_queue_operation import Queue

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("queue,crash,ending", [
    (False, None, "merged"), (True, None, "merged"),
    (True, "after_acceptance", "merged"), (True, "before_scheduling", "merged"),
    (True, None, "evicted"), (True, None, "closed"), (True, None, "protection"),
])
async def test_consumed_merge_mode_comes_from_approved_observation(
    tmp_path, workflow_session_factory, queue, crash, ending
):
    factory = workflow_session_factory
    case, git, read, writes, publication, poll, policy, _ = await published(tmp_path, factory)
    key = policy.github_repository.casefold()
    read.checks[key, git.head] = (CheckSnapshot("ci", "completed", "success", head_sha=git.head),)
    original_protection = read.merge_protections[key, "main"] = MergeProtection(
        True, queue, False, "classic", required_check_names=("ci",),
        merge_queue_method="squash" if queue else None,
    )
    async with PostgresUnitOfWork(factory) as work:
        await ReleaseMonitor(case.artifact_store, publication, read, writes)(poll, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(poll.id, worker_id=poll.lease_owner)
    approval_id, actor = uuid4(), uuid4()
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        work.session.add(Approval(
            id=approval_id, run_id=run.id, gate="merge",
            evidence_digest=run.pending_evidence_digest, run_version=run.version,
            policy_version=run.policy_version, authenticated_actor_id=actor,
        ))
        await work.commands.enqueue(
            run_id=run.id, command_type="approve_merge", idempotency_key=f"{run.id}:approve-merge",
            payload={"approval_id": str(approval_id)}, expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    validator = MergeEvidenceValidator(case.artifact_store, publication, MergeController(read, writes))
    command = await commands.claim_next(worker_id="queue-mode-approval", lease_seconds=120)
    async with PostgresUnitOfWork(factory) as work:
        await ApproveMergeHandler(validator)(command, work)
    # Mutable remote mode no longer selects the effect for this consumed approval.
    read.merge_protections[key, "main"] = MergeProtection(
        True, not queue, False, "changed", required_check_names=("ci",),
        merge_queue_method="squash" if not queue else None,
    )
    async with PostgresUnitOfWork(factory) as work:
        approved = await validator.consumed(work, case.run_id, approval_id, recheck=False)
        assert await validator.queue_required(work, case.run_id, approval_id, approved) is queue
    if not queue:
        return
    read.merge_protections[key, "main"] = original_protection
    await commands.complete(command.id, worker_id=command.lease_owner)
    source = await commands.claim_next(worker_id="queue-admission", lease_seconds=120)
    class InspectCommittedQueue(Queue):
        async def enqueue(self, *args):
            # Independent connection must see the intent before the external effect.
            admitted = await PostgresOperationRepository(factory).get(UUID(args[-1]))
            assert admitted.status is OperationStatus.PENDING
            assert admitted.kind == "enqueue_pr"
            assert admitted.request_payload["approval_id"] == str(approval_id)
            assert admitted.request_payload["head_sha"] == git.head
            return await super().enqueue(*args)

    queue_port = InspectCommittedQueue()
    queue_port.crash = crash == "after_acceptance"
    service = QueueAdmissionService(
        validator, MergeController(read, writes), queue_port,
        OperationExecutor(PostgresOperationRepository(factory)),
    )
    if crash:
        async with PostgresUnitOfWork(factory) as work:
            if crash == "before_scheduling":
                async def fail_scheduling(**kwargs):
                    raise RuntimeError("scheduling crash")
                work.commands.enqueue = fail_scheduling
            with pytest.raises(RuntimeError):
                await service.execute(source, work)
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await service.execute(source, work)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.MERGING
        assert run.version == source.expected_run_version
        record = await work.releases.get_for_run(run.id)
        assert record.merge_intent_id is None and not record.pull_request.merged
        events = [e for e in await work.events.list_after(run.id, 0)
                  if e.event_type == "run.merge_queue_enqueued"]
        assert len(events) == 1
        event = events[0]
        intent = await work.operations.get(UUID(event.payload["enqueue_intent_id"]))
        assert intent.status is OperationStatus.SUCCEEDED
        assert intent.kind == "enqueue_pr" and intent.outcome["entry_id"] == "MQ_1"
        observer = await work.commands.get(UUID(event.payload["queued_command_id"]))
        assert observer.command_type == "observe_merge_queue"
        assert observer.payload["receipt_digest"] == event.payload["receipt_digest"]
        assert observer.payload["deadline"] == (await work.runs.duration_deadline(run.id)).isoformat()
    assert queue_port.writes == 1
    from forge.application.services.queue_observation import QueueObservationService

    await commands.complete(source.id, worker_id=source.lease_owner)
    observation_service = QueueObservationService(
        validator, MergeController(read, writes), queue_port,
        OperationExecutor(PostgresOperationRepository(factory)),
    )
    with patch("forge.persistence.repositories.commands._utc_now", return_value=datetime.now(UTC) + timedelta(seconds=20)):
        first_poll = await commands.claim_next(worker_id="queue-observer", lease_seconds=120)
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await observation_service.execute(first_poll, work)
    async with PostgresUnitOfWork(factory) as work:
        assert (await work.runs.get(case.run_id)).state is RunState.MERGING
    await commands.complete(first_poll.id, worker_id=first_poll.lease_owner)
    if ending == "merged":
        writes.pull_requests[policy.github_repository, 1] = replace(
            writes.pull_requests[policy.github_repository, 1], state="closed", merged=True,
            merge_sha="d" * 40,
        )
        queue_port.receipt = None
    elif ending == "evicted":
        queue_port.receipt = None
    elif ending == "closed":
        writes.pull_requests[policy.github_repository, 1] = replace(
            writes.pull_requests[policy.github_repository, 1], state="closed",
        )
    else:
        read.merge_protections[key, "main"] = replace(original_protection, merge_queue_enabled=False)
    with patch("forge.persistence.repositories.commands._utc_now", return_value=datetime.now(UTC) + timedelta(seconds=40)):
        final_poll = await commands.claim_next(worker_id="queue-final", lease_seconds=120)
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await observation_service.execute(final_poll, work)
    async with PostgresUnitOfWork(factory) as work:
        assert (await work.runs.get(case.run_id)).state is (
            RunState.COMPLETED if ending == "merged" else RunState.AWAITING_HUMAN_INTERVENTION
        )
        record = await work.releases.get_for_run(case.run_id)
        if ending == "merged":
            final_intent = await work.operations.get(record.merge_intent_id)
            assert final_intent.kind == "merge_pr" and final_intent.status is OperationStatus.SUCCEEDED
            assert final_intent.outcome["merge_sha"] == "d" * 40
            assert final_intent.id != intent.id
        else:
            assert record.merge_intent_id is None
            assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is not None
    assert queue_port.writes == 1
