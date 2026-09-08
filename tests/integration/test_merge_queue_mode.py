from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from forge.application.handlers.merge import ApproveMergeHandler
from forge.application.services.merge_evidence import MergeEvidenceValidator
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


@pytest.fixture
async def composed_queue_handlers(tmp_path):
    from contextlib import AsyncExitStack

    from forge.settings import Settings
    from forge.worker.composition import ReleaseDependencies, compose_worker_handlers
    from forge.worker.delivery_runtime import DeliveryRuntime

    async with AsyncExitStack() as resources:
        def compose(case, factory, git, read, writes, queue):
            class Runtime(DeliveryRuntime):
                def git(self, policy):
                    return git

            def unused_push(policy):
                raise AssertionError("queue processing must not push")

            settings = Settings(data_root=tmp_path, prompt_root=tmp_path / "prompts")
            handlers = compose_worker_handlers(
                settings, factory, agent_gateway=case.gateway,
                delivery_runtime=Runtime(settings, factory, case.artifact_store),
                release_dependencies=ReleaseDependencies(read, writes, unused_push, queue=queue),
            )
            resources.push_async_callback(handlers.aclose)
            return handlers

        yield compose


@pytest.mark.parametrize("queue,crash,ending", [
    (False, None, "merged"), (True, None, "merged"),
    (True, "after_acceptance", "merged"), (True, "before_scheduling", "merged"),
    (True, None, "evicted"), (True, None, "closed"), (True, None, "protection"),
    (True, None, "merged_receipt_crash"), (True, None, "merged_transaction_crash"),
    (True, None, "deadline"), (True, None, "deadline_unavailable"),
    (True, None, "admission_rejected"),
    (True, None, "admission_rejected_crash"),
    (True, None, "admission_preflight_unavailable"),
    (True, None, "admission_preflight_drift"), (True, None, "admission_expired"),
    (True, "before_scheduling", "merged_admission_race"),
    (True, "after_acceptance", "merged_before_entry_receipt"),
    (True, "after_acceptance", "merged_before_entry_receipt_commit_crash"),
    (True, None, "admission_uncertain"), (True, None, "admission_uncertain_crash"),
    (True, None, "admission_uncertain_startup"),
    (True, None, "merged_admission_resume"),
    (True, "before_scheduling", "merged_admission_resume_after_receipt"),
    (True, None, "merged_admission_resume_twice"),
    (True, None, "merged_poll_resume"), (True, None, "merged_poll_resume_twice"),
    (True, None, "merged_admission_ack"),
    (True, None, "merged_admission_ack_renewed"),
    (True, None, "routing_history_unavailable"), (True, None, "routing_mode_unavailable"),
    (True, None, "merged_completion_unavailable"),
    (True, None, "merged_protection_unavailable"),
    (True, None, "admission_rejected_graphql"),
    (True, None, "revoked_approval"),
])
async def test_consumed_merge_mode_comes_from_approved_observation(
    tmp_path, workflow_session_factory, composed_queue_handlers, queue, crash, ending
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
        read.merge_protections[key, "main"] = original_protection
        await commands.complete(command.id, worker_id=command.lease_owner)
        source = await commands.claim_next(worker_id="direct-merge", lease_seconds=120)
        unused_queue = Queue()
        handlers = composed_queue_handlers(case, factory, git, read, writes, unused_queue)
        async with PostgresUnitOfWork(factory) as work:
            await handlers["merge_pr"](source, work)
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.runs.get(case.run_id)).state is RunState.COMPLETED
        assert unused_queue.writes == 0
        return
    read.merge_protections[key, "main"] = original_protection
    await commands.complete(command.id, worker_id=command.lease_owner)
    source = await commands.claim_next(worker_id="queue-admission", lease_seconds=120)
    if ending in {"merged_admission_resume", "merged_admission_resume_twice"}:
        from test_release_publication_resume import resumed_release
        source = await resumed_release(case, source, factory)
        if ending == "merged_admission_resume_twice":
            source = await resumed_release(case, source, factory)
    enqueue_attempts = []
    class InspectCommittedQueue(Queue):
        async def enqueue(self, *args):
            # Independent connection must see the intent before the external effect.
            admitted = await PostgresOperationRepository(factory).get(UUID(args[-1]))
            assert admitted.status is OperationStatus.PENDING
            assert admitted.kind == "enqueue_pr"
            assert admitted.request_payload["approval_id"] == str(approval_id)
            assert admitted.request_payload["head_sha"] == git.head
            enqueue_attempts.append(admitted.id)
            if ending == "admission_rejected_graphql":
                from forge.release.github_queue import GitHubMergeQueue

                async def request_error(method, path, **kwargs):
                    assert method == "POST" and path == "/graphql"
                    return {"errors": [{"message": "request validation failed"}]}

                adapter = GitHubMergeQueue(SimpleNamespace(
                    get_pull_request=writes.get_pull_request, _json=request_error,
                ))
                return await adapter.enqueue(*args)
            if ending.startswith("admission_uncertain"):
                from forge.release.github_write import GitHubWriteError
                raise GitHubWriteError("uncertain")
            if ending.startswith("admission_rejected"):
                from forge.release.github_write import GitHubWriteError
                raise GitHubWriteError("rejected")
            return await super().enqueue(*args)

    queue_port = InspectCommittedQueue()
    queue_port.crash = crash == "after_acceptance"
    handlers = composed_queue_handlers(case, factory, git, read, writes, queue_port)
    service = handlers["merge_pr"].__self__
    validator = service._evidence
    if ending.startswith("routing_"):
        from forge.release.merge import StaleMergeEvidence

        method = "for_recovery" if ending == "routing_history_unavailable" else "queue_required"
        with patch.object(validator, method, side_effect=StaleMergeEvidence()):
            for _ in range(2):
                async with PostgresUnitOfWork(factory) as work:
                    await handlers["merge_pr"](source, work)
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.runs.get(case.run_id)).state is RunState.AWAITING_HUMAN_INTERVENTION
            events = await work.events.list_after(case.run_id, 0)
            assert len([e for e in events if e.event_type == "run.merge_evidence_rejected"]) == 1
            assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is not None
            assert not (await work.releases.get_for_run(case.run_id)).pull_request.merged
        assert not enqueue_attempts and queue_port.writes == 0
        return
    if ending == "admission_preflight_unavailable":
        from forge.release.github_write import GitHubWriteError

        async def unavailable_preflight(*args, **kwargs):
            raise GitHubWriteError("unavailable")
        writes.get_pull_request = unavailable_preflight
    elif ending == "admission_preflight_drift":
        read.merge_protections[key, "main"] = replace(original_protection, merge_queue_enabled=False)
    elif ending == "admission_expired":
        async with PostgresUnitOfWork(factory) as work:
            expired_at = await work.runs.duration_deadline(case.run_id)
        service._clock = SimpleNamespace(now=lambda: expired_at)
    if crash:
        async with PostgresUnitOfWork(factory) as work:
            if crash == "before_scheduling":
                async def fail_scheduling(**kwargs):
                    raise RuntimeError("scheduling crash")
                work.commands.enqueue = fail_scheduling
            with pytest.raises(RuntimeError):
                await handlers["merge_pr"](source, work)
    if ending in {"admission_rejected_crash", "admission_uncertain_crash"}:
        async with PostgresUnitOfWork(factory) as work:
            async def fail_rejection(*args, **kwargs):
                raise RuntimeError("rejection settlement crash")
            work.runs.intervene = fail_rejection
            with pytest.raises(RuntimeError, match="rejection settlement crash"):
                await handlers["merge_pr"](source, work)
    if ending.startswith("merged_before_entry_receipt"):
        writes.pull_requests[policy.github_repository, 1] = replace(
            writes.pull_requests[policy.github_repository, 1], state="closed", merged=True,
            merge_sha="d" * 40,
        )
        queue_port.receipt = None
    if ending == "merged_admission_resume_after_receipt":
        from test_release_publication_resume import resumed_release
        source = await resumed_release(case, source, factory)
    saved_pull_read = writes.get_pull_request
    if ending == "merged_before_entry_receipt_commit_crash":
        async with PostgresUnitOfWork(factory) as work:
            async def fail_resolved_completion(*args, **kwargs):
                raise RuntimeError("resolved completion crash")
            work.runs.transition = fail_resolved_completion
            with pytest.raises(RuntimeError, match="resolved completion crash"):
                await handlers["merge_pr"](source, work)

        async def no_more_remote_reads(*args, **kwargs):
            raise AssertionError("durable merge resolution must survive remote unavailability")
        writes.get_pull_request = no_more_remote_reads
        queue_port.observe = no_more_remote_reads
    if ending == "merged_admission_race":
        from forge.release.github_write import GitHubWriteError

        async def changed_preflight(*args, **kwargs):
            raise GitHubWriteError("unavailable")
        writes.get_pull_request = changed_preflight
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            if ending == "merged_admission_race":
                actual_lookup = work.operations.get_by_idempotency_key
                first_lookup = True

                async def stale_initial_lookup(key, actual_lookup=actual_lookup):
                    nonlocal first_lookup
                    if first_lookup:
                        first_lookup = False
                        return None
                    return await actual_lookup(key)
                work.operations.get_by_idempotency_key = stale_initial_lookup
            await handlers["merge_pr"](source, work)
    if ending == "merged_admission_race":
        writes.get_pull_request = saved_pull_read
    if ending.startswith("merged_before_entry_receipt"):
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.runs.get(case.run_id)).state is RunState.COMPLETED
            record = await work.releases.get_for_run(case.run_id)
            admitted = await work.operations.get(enqueue_attempts[0])
            assert admitted.status is OperationStatus.SUCCEEDED
            assert set(admitted.outcome) == {"merged_pull_request"}
            assert admitted.outcome["merged_pull_request"]["merge_sha"] == "d" * 40
            completed = await work.operations.get(record.merge_intent_id)
            assert completed.kind == "merge_pr" and completed.id != admitted.id
            assert completed.status is OperationStatus.SUCCEEDED
            assert await work.commands.get_by_idempotency_key(
                f"{case.run_id}:observe-merge-queue:{admitted.id}:1"
            ) is None
        assert len(enqueue_attempts) == queue_port.writes == 1
        return
    if ending.startswith("admission_"):
        from forge.persistence.queries.dashboard import DashboardQuery

        projected = await DashboardQuery(factory).run_projection(case.run_id)
        assert projected["pull_request"]["queue_admission"] == (
            "uncertain" if ending.startswith("admission_uncertain") else "rejected"
        )
        assert projected["pull_request"]["merge_sha"] is None
        async with PostgresUnitOfWork(factory) as work:
            current_run = await work.runs.get(case.run_id)
            assert current_run.state is RunState.AWAITING_HUMAN_INTERVENTION
            assert current_run.version == source.expected_run_version + 1
            assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is not None
            uncertain = ending.startswith("admission_uncertain")
            rejections = [e for e in await work.events.list_after(case.run_id, 0)
                          if e.event_type == (
                              "run.merge_queue_admission_uncertain" if uncertain
                              else "run.merge_queue_admission_rejected"
                          )]
            assert len(rejections) == 1
            rejected = await work.operations.get(UUID(rejections[0].payload["enqueue_intent_id"]))
            assert rejected.status is (
                OperationStatus.NEEDS_RECONCILIATION if uncertain else OperationStatus.FAILED
            )
            if not uncertain:
                assert rejected.error == (
                    "queue_remote_rejected" if ending.startswith("admission_rejected")
                    else "queue_preflight_rejected"
                )
            assert await work.commands.get_by_idempotency_key(
                f"{case.run_id}:observe-merge-queue:{rejected.id}:1"
            ) is None
        assert len(enqueue_attempts) == int(ending.startswith(("admission_rejected", "admission_uncertain")))
        assert queue_port.writes == 0
        if ending == "admission_uncertain_startup":
            from forge.application.services.recovery import RecoveryError, RecoveryService
            from forge.domain.merge_queue import MergeQueueReceipt

            queue_port.receipt = MergeQueueReceipt(
                repository=approved.repository, pull_request_number=approved.pull_request_number,
                pull_request_node_id="PR_1", head_sha=approved.head_sha,
                merge_method=approved.merge_method, entry_id="late-entry",
            )
            recovery_adapter = handlers.recovery_adapters["enqueue_pr"]
            with pytest.raises(RecoveryError):
                await recovery_adapter.invoke(rejected)
            await RecoveryService(PostgresOperationRepository(factory)).reconcile(rejected.id, recovery_adapter)
            async with PostgresUnitOfWork(factory) as work:
                await handlers["merge_pr"](source, work)
            async with PostgresUnitOfWork(factory) as work:
                assert (await work.operations.get(rejected.id)).status is OperationStatus.SUCCEEDED
                assert (await work.runs.get(case.run_id)).state is RunState.AWAITING_HUMAN_INTERVENTION
            assert len(enqueue_attempts) == 1 and queue_port.writes == 0
        return
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
    from forge.api.schemas.projections import PullRequestSection
    from forge.persistence.queries.dashboard import DashboardQuery

    projected = await DashboardQuery(factory).run_projection(case.run_id)
    projected_pr = PullRequestSection.model_validate(projected["pull_request"])
    assert projected_pr.queue_admission == "accepted"
    assert projected_pr.merge_sha is None
    assert queue_port.writes == 1

    continued_observer = None
    if ending.startswith("merged_admission_ack"):
        from test_release_publication_resume import resumed_release
        if ending.endswith("renewed"):
            from forge.application.ports.commands import CommandRecoveryRequired

            with pytest.raises(CommandRecoveryRequired, match="lease is active or changed"):
                await resumed_release(
                    case, source, factory, continued_type="observe_merge_queue", renewed=True
                )
            async with PostgresUnitOfWork(factory) as work:
                assert (await work.commands.get(source.id)).status.value == "leased"
                assert (await work.commands.get(observer.id)).status.value == "pending"
                assert not [e for e in await work.events.list_after(case.run_id, 0)
                            if e.event_type == "queue_admission.acknowledged_on_resume"]
            return
        continued_observer = await resumed_release(
            case, source, factory, continued_type="observe_merge_queue"
        )
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.commands.get(source.id)).status.value == "completed"
    else:
        await commands.complete(source.id, worker_id=source.lease_owner)
    observation_service = handlers["observe_merge_queue"].__self__
    with patch("forge.persistence.repositories.commands._utc_now", return_value=datetime.now(UTC) + timedelta(seconds=20)):
        first_poll = continued_observer or await commands.claim_next(worker_id="queue-observer", lease_seconds=120)
    if ending == "merged_poll_resume":
        from forge.application.ports.commands import CommandRecoveryRequired
        from forge.application.services.queue_resume import verify_queue_resume

        substitutions = {
            "merge_command_id": str(command.id),
            "enqueue_intent_id": str(record.publication_intent_id),
            "receipt_digest": "0" * 64,
            "deadline": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "poll": 2,
        }
        for field, value in substitutions.items():
            async with PostgresUnitOfWork(factory) as work:
                approval = await work.auth.get_approval(approval_id=approval_id)
                changed = replace(first_poll, payload={**dict(first_poll.payload), field: value})
                with pytest.raises(CommandRecoveryRequired):
                    await verify_queue_resume(work, changed, approval)
                assert (await work.commands.get(first_poll.id)).status == first_poll.status
    if ending.startswith("merged_poll_resume"):
        from test_release_publication_resume import resumed_release
        first_poll = await resumed_release(case, first_poll, factory)
        if ending == "merged_poll_resume_twice":
            first_poll = await resumed_release(case, first_poll, factory)
    if ending == "merged_protection_unavailable":
        from forge.release.github_client import GitHubClientError

        original_protection_read = read.get_merge_protection

        async def unavailable_protection(*args):
            raise GitHubClientError("unavailable")

        read.get_merge_protection = unavailable_protection
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await handlers["observe_merge_queue"](first_poll, work)
    if ending == "merged_protection_unavailable":
        read.get_merge_protection = original_protection_read
    async with PostgresUnitOfWork(factory) as work:
        assert (await work.runs.get(case.run_id)).state is RunState.MERGING
    await commands.complete(first_poll.id, worker_id=first_poll.lease_owner)
    if ending.startswith("merged"):
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
    elif ending == "protection":
        read.merge_protections[key, "main"] = replace(original_protection, merge_queue_enabled=False)
    elif ending == "revoked_approval":
        async with PostgresUnitOfWork(factory) as work:
            approval = await work.auth.get_approval(approval_id=approval_id)
            await work.auth.invalidate_merge_gate(
                run_id=case.run_id, run_version=approval.run_version,
                at=datetime.now(UTC),
            )
            await work.commit()

        async def no_remote_read(*args):
            raise AssertionError("invalidated queue approval must settle without remote reads")

        writes.get_pull_request = no_remote_read
    with patch("forge.persistence.repositories.commands._utc_now", return_value=datetime.now(UTC) + timedelta(seconds=40)):
        final_poll = await commands.claim_next(worker_id="queue-final", lease_seconds=120)
    if ending.startswith("deadline"):
        observation_service._clock = SimpleNamespace(
            now=lambda: datetime.fromisoformat(final_poll.payload["deadline"])
        )
        if ending == "deadline_unavailable":
            from forge.release.github_write import GitHubWriteError

            async def timed_out(*args, **kwargs):
                raise GitHubWriteError("unavailable")
            writes.get_pull_request = timed_out
    if ending == "merged_completion_unavailable":
        from forge.release.github_write import GitHubWriteError

        actual_read = writes.get_pull_request
        completion_reads = 0

        async def completion_unavailable(*args):
            nonlocal completion_reads
            completion_reads += 1
            if completion_reads == 2:
                raise GitHubWriteError("unavailable")
            return await actual_read(*args)

        writes.get_pull_request = completion_unavailable
        for _ in range(2):
            async with PostgresUnitOfWork(factory) as work:
                await handlers["observe_merge_queue"](final_poll, work)
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.runs.get(case.run_id)).state is RunState.AWAITING_HUMAN_INTERVENTION
            assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is not None
            events = await work.events.list_after(case.run_id, 0)
            interventions = [e for e in events if e.event_type == "run.merge_queue_intervention"]
            assert len(interventions) == 1
            assert interventions[0].payload["reason"] == "queue_completion_unresolved"
            unresolved = await work.operations.get(UUID(interventions[0].payload["merge_intent_id"]))
            assert unresolved.kind == "merge_pr"
            assert unresolved.status is OperationStatus.NEEDS_RECONCILIATION
            assert unresolved.execution_owner is None
            assert (await work.releases.get_for_run(case.run_id)).merge_intent_id is None
        assert completion_reads == 2 and queue_port.writes == 1
        return
    if ending in {"merged_receipt_crash", "merged_transaction_crash"}:
        async with PostgresUnitOfWork(factory) as work:
            async def fail_completion(*args, **kwargs):
                raise RuntimeError("completion transaction crash")
            if ending == "merged_receipt_crash":
                work.releases.record_merge = fail_completion
            else:
                work.runs.transition = fail_completion
            with pytest.raises(RuntimeError, match="completion transaction crash"):
                await handlers["observe_merge_queue"](final_poll, work)
        from forge.release.github_write import GitHubWriteError

        async def remote_unavailable(*args, **kwargs):
            raise AssertionError("persisted successful receipt must not require another remote read")
        writes.get_pull_request = remote_unavailable
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await handlers["observe_merge_queue"](final_poll, work)
    async with PostgresUnitOfWork(factory) as work:
        assert (await work.runs.get(case.run_id)).state is (
            RunState.COMPLETED if ending.startswith("merged") else RunState.AWAITING_HUMAN_INTERVENTION
        )
        record = await work.releases.get_for_run(case.run_id)
        if ending.startswith("merged"):
            final_intent = await work.operations.get(record.merge_intent_id)
            assert final_intent.kind == "merge_pr" and final_intent.status is OperationStatus.SUCCEEDED
            assert final_intent.outcome["merge_sha"] == "d" * 40
            assert final_intent.id != intent.id
        else:
            assert record.merge_intent_id is None
            assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is not None
    assert queue_port.writes == 1
    projected = await DashboardQuery(factory).run_projection(case.run_id)
    assert projected["pull_request"]["merge_sha"] == (
        "d" * 40 if ending.startswith("merged") else None
    )
