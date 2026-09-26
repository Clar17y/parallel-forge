"""Revised subscription publication preserves remote consent and historical recovery."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.approval import SubscriptionMergeApprovalEvidence, decode_pr_approval_evidence
from forge.domain.github import CheckSnapshot
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.release.fake_github_write import FakeGitHubWriteCrash
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_remote_publication import remote_acceptance_case


@pytest.mark.integration
@pytest.mark.parametrize("another_failure", [False, True])
@pytest.mark.parametrize("resume_before_repair", [False, True])
async def test_remote_return_updates_existing_pr_then_merges_only_at_a_human_gate(
    session_factory, tmp_path, another_failure, resume_before_repair
):
    case = await remote_acceptance_case(
        session_factory, tmp_path, resume_before_repair=resume_before_repair
    )
    async with case.factory() as work:
        accepted = await case.controller.validate(case.validation_command, work)
    await case.commands.complete(
        case.validation_command.id, worker_id=case.validation_command.lease_owner
    )
    push = await case.commands.claim_next(worker_id="reviewed-push", lease_seconds=120)
    assert push is not None and push.command_type == "push_reviewed_pr"
    pushes = []

    class Push:
        async def push(self, worktree, policy, head):
            pushes.append(head)
            if len(pushes) == 1:
                raise FakeGitHubWriteCrash()

    release = ReleaseService(
        case.validator,
        case.writes,
        lambda _: Push(),
        OperationExecutor(PostgresOperationRepository(session_factory), execution_lease_seconds=1),
    )
    async with case.factory() as work:
        with pytest.raises(FakeGitHubWriteCrash):
            await release.push_reviewed(push, work)
    for _ in range(2):
        async with case.factory() as work:
            await release.push_reviewed(push, work)
    async with case.factory() as work:
        run = await work.runs.get(push.run_id)
        assert run.state is RunState.MONITORING_PR
        record = await work.releases.get_for_run(run.id)
        original = await work.operations.get(record.publication_intent_id)
        assert record.candidate_evidence_digest == accepted.pr_evidence_digest
        assert record.reviewed_push_intent_id is not None
        verified = await case.validator.validate_published(
            work, run.id, UUID(original.request_payload["approval_id"])
        )
        first = decode_pr_approval_evidence(
            await case.dispatch._store.open_bytes(case.first.pr_evidence_digest)
        )
        assert verified.evidence.acceptance_digest != first.acceptance_digest
        assert (
            await case.repairs.replay(
                case.repair_command,
                work,
                await ApprovedPlanLoader(case.dispatch._store).load(work, run.id),
            )
            is not None
        )
        queued = await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:2")
        (await work.session.get(RunCommand, queued.id)).available_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        await work.commit()
    # A lost response is reconciled against the actual remote identity without
    # another push or PR creation, including an unchanged rechecked candidate.
    assert pushes == [case.original.review.candidate.head_sha]
    assert len(case.writes.pull_requests) == 1
    await case.commands.complete(push.id, worker_id=push.lease_owner)
    monitor = await case.commands.claim_next(worker_id="monitor-again", lease_seconds=120)
    assert monitor is not None and monitor.command_type == "monitor_pr"
    repository = case.original.policy.github_repository.casefold()
    head = case.original.review.candidate.head_sha
    case.reads.checks[repository, head] = (
        CheckSnapshot(
            "ci", "completed", "failure" if another_failure else "success", head_sha=head
        ),
    )
    async with case.factory() as work:
        await ReleaseMonitor(case.dispatch._store, case.validator, case.reads, case.writes)(
            monitor, work
        )
    async with case.factory() as work:
        run = await work.runs.get(push.run_id)
        assert not case.writes.pull_requests[case.original.policy.github_repository, 1].merged
        if not another_failure:
            assert run.state is RunState.AWAITING_MERGE_APPROVAL
            merge = SubscriptionMergeApprovalEvidence.model_validate_json(
                await case.dispatch._store.open_bytes(run.pending_evidence_digest)
            )
            assert merge.acceptance_digest == verified.evidence.acceptance_digest
            return
        assert run.state is RunState.REMEDIATING and run.remote_remediation_count == 2
    await case.commands.complete(monitor.id, worker_id=monitor.lease_owner)
    repair = await case.commands.claim_next(worker_id="repair-again", lease_seconds=120)
    assert repair is not None and repair.command_type == "remediate_remote"
    async with case.factory() as work:
        await case.repairs.execute(repair, work)
    async with case.factory() as work:
        scheduled = await work.session.get(
            SubscriptionScheduledTask, case.original.decision.task_id
        )
        scheduler = await work.session.get(SubscriptionSchedulerRun, repair.run_id)
        assert scheduled.state == "queued" and scheduled.repairs == 2
        assert scheduler.candidate_epoch == case.original.review.candidate_epoch + 3
