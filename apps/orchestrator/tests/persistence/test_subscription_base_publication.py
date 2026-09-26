"""An adopted candidate returns through fresh acceptance to the original PR."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.application.services.release_monitor import ReleaseMonitor
from forge.application.services.subscription_publication import SubscriptionPublicationController
from forge.application.services.subscription_remote_remediation import (
    SubscriptionRemoteRemediationController,
)
from forge.application.services.validation import ValidationService
from forge.domain.approval import decode_pr_approval_evidence
from forge.domain.github import CheckSnapshot
from forge.domain.run import RunState
from forge.domain.validation import command_spec_digest
from forge.persistence.models import RunCommand
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.release.fake_github_write import FakeGitHubWriteCrash
from subscription_changed_candidate_fixture import accept_changed_candidate
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_base_adoption import base_adoption_case

from apps.orchestrator.tests.application.test_controller_check_adapter import _setup_real_adapter


async def adopted_acceptance_case(session_factory, tmp_path, *, fail_after_repair=False):
    case = await base_adoption_case(session_factory, tmp_path)
    async with case.factory() as work:
        await case.service.execute(case.command, work)
    if fail_after_repair:
        from test_subscription_resume_controls import pause_and_resume

        await case.commands.fail(
            case.command.id,
            worker_id=case.command.lease_owner,
            error="delivery completion unavailable",
        )
        assert (
            await pause_and_resume(
                case.factory,
                session_factory,
                case.command.run_id,
                case.dispatch._store,
                repairs=case.repairs,
            )
            is None
        )
    else:
        await case.commands.complete(case.command.id, worker_id=case.command.lease_owner)
    case.selecting, case.accepting = await accept_changed_candidate(
        case.factory, session_factory, case.original, case.dispatch, case.git
    )
    case.validation_command = await case.commands.claim_next(
        worker_id="adopted-validation", lease_seconds=120
    )
    assert (
        case.validation_command is not None and case.validation_command.command_type == "validate"
    )
    _, _, runner, _, _ = await _setup_real_adapter(
        tmp_path / "artifacts",
        SimpleNamespace(id=case.command.run_id, project_id=case.original.policy.id),
        session_factory,
    )
    runner.runner.terminal = replace(
        runner.runner.terminal,
        result=replace(
            runner.runner.terminal.result,
            command_digest=command_spec_digest(case.original.policy.required_checks[0]),
        ),
    )

    async def environment(*args):
        return {}

    case.repairs = SubscriptionRemoteRemediationController(
        case.dispatch._store, case.validator, lambda _: case.git
    )
    case.controller = SubscriptionPublicationController(
        case.dispatch._store,
        validation=ValidationService(
            case.dispatch._store,
            uow_factory=case.factory,
            operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
            git_factory=lambda _: case.git,
            runner_factory=runner,
            environment_resolver=environment,
        ),
        git_factory=lambda _: case.git,
        remote_repairs=case.repairs,
    )
    case.runner = runner
    return case


@pytest.mark.integration
@pytest.mark.parametrize("followup", ["merge", "remote_failure", "base_advance"])
async def test_adopted_candidate_returns_to_original_pr_and_separate_merge_gate(
    session_factory, tmp_path, followup
):
    case = await adopted_acceptance_case(session_factory, tmp_path)
    async with case.factory() as work:
        accepted = await case.controller.validate(case.validation_command, work)
        assert accepted.state is RunState.MONITORING_PR
    await case.commands.complete(
        case.validation_command.id, worker_id=case.validation_command.lease_owner
    )
    push = await case.commands.claim_next(worker_id="adopted-push", lease_seconds=120)
    assert push is not None and push.command_type == "push_reviewed_pr"
    pushes = []

    class Push:
        async def push(self, tree, policy, head):
            pushes.append(head)
            assert head == case.new_head
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
    await case.commands.complete(push.id, worker_id=push.lease_owner)
    async with case.factory() as work:
        record = await work.releases.get_for_run(push.run_id)
        original = await work.operations.get(record.publication_intent_id)
        verified = await case.validator.validate_published(
            work, push.run_id, UUID(original.request_payload["approval_id"])
        )
        assert record.pull_request.base_sha == case.target
        assert record.pull_request.head_sha == case.new_head
        assert verified.evidence.base_sha == case.original.worktree.base_sha
        assert (
            verified.evidence.acceptance_digest
            != decode_pr_approval_evidence(
                await case.dispatch._store.open_bytes(case.first.pr_evidence_digest)
            ).acceptance_digest
        )
        assert await case.repairs.base_updates.replay(case.command, work) is not None
        queued = await work.commands.get_by_idempotency_key(f"{push.run_id}:monitor-pr:2")
        (await work.session.get(RunCommand, queued.id)).available_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        await work.commit()
    assert pushes == [case.new_head] and len(case.writes.pull_requests) == 1
    monitor = await case.commands.claim_next(worker_id="adopted-monitor", lease_seconds=120)
    assert monitor is not None and monitor.command_type == "monitor_pr"
    case.reads.checks[case.original.policy.github_repository.casefold(), case.new_head] = (
        CheckSnapshot(
            "ci",
            "completed",
            "failure" if followup == "remote_failure" else "success",
            head_sha=case.new_head,
        ),
    )
    repository = case.original.policy.github_repository
    next_base, next_head = "c" * 40, "d" * 40
    if followup == "base_advance":
        case.reads.bases[repository.casefold(), "main"] = next_base
        case.writes.branch_shas[repository, "main"] = next_base
        case.writes.pull_requests[repository, 1] = replace(
            case.writes.pull_requests[repository, 1], base_sha=next_base
        )
    async with case.factory() as work:
        await ReleaseMonitor(case.dispatch._store, case.validator, case.reads, case.writes)(
            monitor, work
        )
    async with case.factory() as work:
        run = await work.runs.get(push.run_id)
        assert run.state is (
            RunState.AWAITING_MERGE_APPROVAL if followup == "merge" else RunState.REMEDIATING
        )
        assert run.base_sha == case.original.worktree.base_sha
    assert not case.writes.pull_requests[case.original.policy.github_repository, 1].merged
    if followup == "merge":
        return
    await case.commands.complete(monitor.id, worker_id=monitor.lease_owner)
    command = await case.commands.claim_next(worker_id="adopted-followup", lease_seconds=120)
    assert command is not None
    if followup == "remote_failure":
        assert command.command_type == "remediate_remote"
        async with case.factory() as work:
            await case.repairs.execute(command, work)
    else:
        assert command.command_type == "update_base"

        async def update(repo, number, previous):
            assert previous == case.new_head
            updated = replace(case.writes.pull_requests[repo, number], head_sha=next_head)
            case.writes.pull_requests[repo, number] = updated
            case.writes.branch_shas[repo, updated.head_ref] = next_head
            return updated

        class Adoption:
            async def adopt(self, tree, policy, previous, head, base):
                assert previous == case.new_head and head == next_head and base == next_base
                case.snapshot[0] = replace(case.snapshot[0], head_sha=head)

            async def inspect(self, tree, policy, previous, head, base):
                assert case.snapshot[0].head_sha == head == next_head

        case.writes.update_branch = update
        case.service._adoption = lambda _: Adoption()
        async with case.factory() as work:
            await case.service.execute(command, work)
    async with case.factory() as work:
        run = await work.runs.get(push.run_id)
        scheduled = await work.session.get(
            SubscriptionScheduledTask, case.original.decision.task_id
        )
        assert run.state is RunState.REMEDIATING and run.remote_remediation_count == 2
        assert run.local_remediation_count == 0
        assert scheduled.state == "queued" and scheduled.repairs == 2
        # The earlier base adoption is still verifiable after another repair.
        assert await case.repairs.base_updates.replay(case.command, work) is not None
