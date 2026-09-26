"""Controlled base adoption requires a fresh bounded primary acceptance."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from forge.application.ports.worktrees import GitCandidateDiff, GitDiff
from forge.application.services.base_update import BaseUpdateService
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release_monitor import ReleaseMonitor
from forge.application.services.subscription_remote_remediation import (
    SubscriptionRemoteRemediationController,
)
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.release.fake_github import FakeGitHub
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_pr_publication import published_subscription_case


async def base_adoption_case(session_factory, tmp_path):
    (
        factory,
        proposal,
        dispatch,
        validator,
        commands,
        writes,
        published,
        first,
    ) = await published_subscription_case(session_factory, tmp_path)
    await commands.complete(published.id, worker_id=published.lease_owner)
    async with factory() as work:
        queued = await work.commands.get_by_idempotency_key(f"{published.run_id}:monitor-pr:1")
        (await work.session.get(RunCommand, queued.id)).available_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        original = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        accepted = (
            original.result_digest,
            original.application_digest,
            original.application_payload,
        )
        await work.commit()
    monitor = await commands.claim_next(worker_id="base-monitor", lease_seconds=120)
    assert monitor is not None and monitor.command_type == "monitor_pr"
    target, new_head = "f" * 40, "e" * 40
    repository = proposal.policy.github_repository
    reads = FakeGitHub()
    reads.bases[repository.casefold(), "main"] = target
    reads.checks[repository.casefold(), proposal.review.candidate.head_sha] = (
        CheckSnapshot("ci", "completed", "success", head_sha=proposal.review.candidate.head_sha),
    )
    reads.merge_protections[repository.casefold(), "main"] = MergeProtection(
        True,
        False,
        False,
        "strict",
        required_check_names=("ci",),
    )
    writes.branch_shas[repository, "main"] = target
    writes.pull_requests[repository, 1] = replace(
        writes.pull_requests[repository, 1], base_sha=target
    )
    validator._github = reads
    async with factory() as work:
        await ReleaseMonitor(dispatch._store, validator, reads, writes)(monitor, work)
    await commands.complete(monitor.id, worker_id=monitor.lease_owner)
    command = await commands.claim_next(worker_id="base-adoption", lease_seconds=120)
    assert command is not None and command.command_type == "update_base"
    git = validator._git_factory(proposal.policy)
    snapshot = [git.working_tree_snapshot(proposal.worktree)]
    git.working_tree_snapshot = lambda *args, **kwargs: snapshot[0]
    git.head_sha = lambda *args: snapshot[0].head_sha
    git.candidate_diff = lambda *args: GitCandidateDiff(
        head_sha=snapshot[0].head_sha,
        diff=GitDiff(text="", original_byte_count=0, truncated=False),
        changed_paths=(),
    )
    updates, adoptions = [], []

    async def update(repo, number, previous):
        updates.append(previous)
        pull = replace(writes.pull_requests[repo, number], head_sha=new_head)
        writes.pull_requests[repo, number] = pull
        writes.branch_shas[repo, pull.head_ref] = new_head
        return pull

    writes.update_branch = update

    class Adoption:
        async def adopt(self, tree, policy, previous, head, base):
            adoptions.append(head)
            assert previous == proposal.review.candidate.head_sha and base == target
            snapshot[0] = replace(snapshot[0], head_sha=head)

        async def inspect(self, tree, policy, previous, head, base):
            assert snapshot[0].head_sha == head

    repairs = SubscriptionRemoteRemediationController(dispatch._store, validator, lambda _: git)
    service = BaseUpdateService(
        dispatch._store,
        validator,
        reads,
        writes,
        lambda _: Adoption(),
        OperationExecutor(PostgresOperationRepository(session_factory)),
        subscription=repairs.base_updates,
    )
    return SimpleNamespace(
        factory=factory,
        original=proposal,
        dispatch=dispatch,
        validator=validator,
        commands=commands,
        writes=writes,
        command=command,
        first=first,
        reads=reads,
        git=git,
        snapshot=snapshot,
        updates=updates,
        adoptions=adoptions,
        service=service,
        target=target,
        new_head=new_head,
        accepted=accepted,
        repairs=repairs,
    )


@pytest.mark.integration
async def test_adopted_base_reopens_primary_without_rewriting_approved_baseline(
    session_factory, tmp_path
):
    case = await base_adoption_case(session_factory, tmp_path)
    factory, proposal, command = case.factory, case.original, case.command
    async with factory() as work:
        await case.service.execute(command, work)
    async with factory() as work:
        await case.service.execute(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.REMEDIATING
        assert run.base_sha == proposal.worktree.base_sha
        record = await work.releases.get_for_run(run.id)
        assert record.pull_request.base_sha == case.target
        assert record.pull_request.head_sha == case.new_head
        assert (
            record.base_update_intent_id is not None and record.base_adoption_intent_id is not None
        )
        scheduled = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, run.id)
        assert scheduled.state == "queued" and scheduled.repairs == 1
        assert scheduler.candidate_state == "open"
        assert scheduler.candidate_epoch == proposal.review.candidate_epoch + 1
        original = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert (
            original.result_digest,
            original.application_digest,
            original.application_payload,
        ) == case.accepted
        assert run.remote_remediation_count == 1 and run.local_remediation_count == 0
        assert not case.writes.pull_requests[proposal.policy.github_repository, 1].merged
    assert case.updates == [proposal.review.candidate.head_sha]
    assert case.adoptions == [case.new_head]
