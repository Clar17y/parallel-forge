"""Accepted subscription evidence participates in durable publication recovery."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.merge import ApproveMergeHandler
from forge.application.services.merge import MergeService
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.approval import SubscriptionMergeApprovalEvidence, decode_pr_approval_evidence
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.run import RunState
from forge.persistence.models import Approval, RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.release.fake_github import FakeGitHub
from forge.release.fake_github_write import FakeGitHubWrite, FakeGitHubWriteCrash
from forge.release.merge import MergeController
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_publication import approved_publication_case


async def published_subscription_case(session_factory, tmp_path):
    factory, proposal, dispatch, validator, _, outcome, runner, _ = await approved_publication_case(
        session_factory, tmp_path
    )
    commands = PostgresCommandRepository(session_factory)
    command = await commands.claim_next(worker_id="publisher", lease_seconds=120)
    assert command is not None and command.command_type == "publish_pr"
    writes = FakeGitHubWrite()
    writes.branch_shas[proposal.policy.github_repository, "main"] = proposal.worktree.base_sha
    pushes = []

    class Push:
        async def push(self, worktree, policy, approved_sha):
            async with factory() as observed:
                assert any(
                    intent.kind == "push_branch" and intent.run_id == command.run_id
                    for intent in await observed.operations.list_unresolved()
                )
            pushes.append(approved_sha)
            writes.branch_shas[policy.github_repository, worktree.identity.branch] = approved_sha

    service = ReleaseService(
        validator,
        writes,
        lambda _: Push(),
        OperationExecutor(
            PostgresOperationRepository(session_factory),
            execution_lease_seconds=1,
        ),
    )
    writes.crash_next_write = True
    async with factory() as work:
        with pytest.raises(FakeGitHubWriteCrash):
            await service.publish(command, work)
    for _ in range(2):
        async with factory() as work:
            await service.publish(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        record = await work.releases.get_for_run(run.id)
        assert run.state is RunState.MONITORING_PR
        assert record.pull_request.number == 1
        assert record.pull_request.head_sha == proposal.review.candidate.head_sha
        assert await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:1") is not None
        assert not await work.operations.list_unresolved()
    assert pushes == [proposal.review.candidate.head_sha]
    assert len(writes.pull_requests) == runner.calls == runner.runner.calls == 1
    return factory, proposal, dispatch, validator, commands, writes, command, outcome


@pytest.mark.integration
async def test_subscription_pr_creation_recovers_without_repeating_effects(
    session_factory, tmp_path
):
    await published_subscription_case(session_factory, tmp_path)


@pytest.mark.integration
async def test_subscription_merge_gate_and_recovery_retain_the_actual_acceptance(
    session_factory, tmp_path
):
    (
        factory,
        proposal,
        dispatch,
        validator,
        commands,
        writes,
        published,
        outcome,
    ) = await published_subscription_case(session_factory, tmp_path)
    await commands.complete(published.id, worker_id=published.lease_owner)
    async with factory() as work:
        queued = await work.commands.get_by_idempotency_key(f"{published.run_id}:monitor-pr:1")
        (await work.session.get(RunCommand, queued.id)).available_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        await work.commit()
    poll = await commands.claim_next(worker_id="monitor", lease_seconds=120)
    assert poll is not None and poll.command_type == "monitor_pr"
    reads = FakeGitHub()
    repository = proposal.policy.github_repository.casefold()
    head = proposal.review.candidate.head_sha
    reads.bases[repository, "main"] = proposal.worktree.base_sha
    reads.checks[repository, head] = (CheckSnapshot("ci", "completed", "success", head_sha=head),)
    reads.merge_protections[repository, "main"] = MergeProtection(
        True,
        False,
        False,
        "classic",
        required_check_names=("ci",),
    )
    monitor = ReleaseMonitor(dispatch._store, validator, reads, writes)
    for _ in range(2):
        async with factory() as work:
            await monitor(poll, work)
    merge_controller = MergeController(reads, writes)
    merge_evidence = MergeEvidenceValidator(dispatch._store, validator, merge_controller)
    pr_evidence = decode_pr_approval_evidence(
        await dispatch._store.open_bytes(outcome.pr_evidence_digest)
    )
    async with factory() as work:
        run = await work.runs.get(published.run_id)
        assert run.state is RunState.AWAITING_MERGE_APPROVAL
        evidence = await merge_evidence.validate(work, run.id)
        assert isinstance(evidence, SubscriptionMergeApprovalEvidence)
        assert evidence.acceptance_digest == pr_evidence.acceptance_digest
        assert evidence.candidate_tree_digest == proposal.review.candidate.tree_digest
        assert "review_digest" not in evidence.model_dump()
        assert not writes.pull_requests[proposal.policy.github_repository, 1].merged
    await commands.complete(poll.id, worker_id=poll.lease_owner)
    # This is a test-only human approval bound to the actual displayed merge gate.
    approval_id, actor = uuid4(), uuid4()
    async with factory() as work:
        run = await work.runs.get(published.run_id)
        work.session.add(
            Approval(
                id=approval_id,
                run_id=run.id,
                gate="merge",
                evidence_digest=run.pending_evidence_digest,
                run_version=run.version,
                policy_version=run.policy_version,
                authenticated_actor_id=actor,
            )
        )
        await work.commands.enqueue(
            run_id=run.id,
            command_type="approve_merge",
            idempotency_key=f"{run.id}:approve-merge",
            payload={"approval_id": str(approval_id)},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    approve = await commands.claim_next(worker_id="merge-approval", lease_seconds=120)
    assert approve is not None and approve.command_type == "approve_merge"
    for _ in range(2):
        async with factory() as work:
            await ApproveMergeHandler(merge_evidence)(approve, work)
    await commands.complete(approve.id, worker_id=approve.lease_owner)
    merge = await commands.claim_next(worker_id="merger", lease_seconds=120)
    assert merge is not None and merge.command_type == "merge_pr"
    service = MergeService(
        merge_evidence,
        merge_controller,
        OperationExecutor(
            PostgresOperationRepository(session_factory),
            execution_lease_seconds=1,
        ),
    )
    calls = []
    original = writes.merge_pull_request

    async def merge_once(*args):
        calls.append(args)
        return await original(*args)

    writes.merge_pull_request = merge_once
    writes.crash_next_write = True
    async with factory() as work:
        with pytest.raises(FakeGitHubWriteCrash):
            await service.execute(merge, work)
    for _ in range(2):
        async with factory() as work:
            await service.execute(merge, work)
    async with factory() as work:
        assert (await work.runs.get(merge.run_id)).state is RunState.COMPLETED
        assert await merge_evidence.for_recovery(work, merge.run_id, approval_id) == evidence
        assert not await work.operations.list_unresolved()
    assert len(calls) == 1 and writes.pull_requests[proposal.policy.github_repository, 1].merged
