"""Publication effects run only after durable intent admission."""

import pytest
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.fake_github_write import FakeGitHubWrite, FakeGitHubWriteCrash
from test_pr_approval import authorized
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("after_push", [False, True])
@pytest.mark.parametrize("drift_type", ["base", "content", "read"])
async def test_publication_evidence_drift_settles_once_without_another_write(
    tmp_path, workflow_session_factory, after_push, drift_type
):
    factory = workflow_session_factory
    case, git, read, approve, approval_command, approval_id = await authorized(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        await approve(approval_command, work)
        policy = (await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)).policy
    commands = PostgresCommandRepository(factory)
    await commands.complete(approval_command.id, worker_id=approval_command.lease_owner)
    command = await commands.claim_next(worker_id="publisher", lease_seconds=120)
    writes = FakeGitHubWrite()
    writes.branch_shas[policy.github_repository, "main"] = git.worktree.base_sha
    pushes = []

    def drift():
        if drift_type == "content":
            git.head = "f" * 40
        elif drift_type == "read":
            from forge.release.github_client import GitHubClientError

            async def unavailable(*args):
                raise GitHubClientError("unavailable")

            read.get_base = unavailable
        else:
            for key in read.bases:
                read.bases[key] = "f" * 40

    class Push:
        async def push(self, worktree, policy, approved_sha):
            pushes.append(approved_sha)
            writes.branch_shas[policy.github_repository, worktree.identity.branch] = approved_sha
            drift()

    if not after_push:
        drift()
    service = ReleaseService(
        PrEvidenceValidator(
            case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _: git, read
        ),
        writes,
        lambda _: Push(),
        OperationExecutor(PostgresOperationRepository(factory), execution_lease_seconds=1),
    )
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await service.publish(command, work)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        assert run.version == command.expected_run_version + 1
        assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is not None
        events = [
            e
            for e in await work.events.list_after(run.id, 0)
            if e.event_type == "run.publication_evidence_rejected"
        ]
        assert len(events) == 1
        assert (
            events[0].payload["reason"]
            == {
                "base": "remote_base_drift",
                "content": "content_drift",
                "read": "remote_read_failed",
            }[drift_type]
        )
        assert await work.releases.get_for_run(run.id) is None
        assert await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:1") is None
    assert len(pushes) == int(after_push)
    assert writes.pull_requests == {}


@pytest.mark.parametrize(
    "failure",
    [None, "push_uncertain", "create_uncertain", "ambiguous_reconcile", "intervention_crash"],
)
async def test_crash_after_pr_creation_reconciles_once_before_monitoring(
    tmp_path, workflow_session_factory, failure
):
    factory = workflow_session_factory
    case, git, read, approve, approval_command, approval_id = await authorized(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        await approve(approval_command, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(approval_command.id, worker_id=approval_command.lease_owner)
    command = await commands.claim_next(worker_id="publisher", lease_seconds=120)
    assert command.command_type == "publish_pr"
    writes = FakeGitHubWrite()
    async with PostgresUnitOfWork(factory) as work:
        repository = (
            await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)
        ).policy.github_repository
    writes.branch_shas[repository, "main"] = git.worktree.base_sha
    pushes = []

    class Push:
        async def push(self, worktree, policy, approved_sha):
            # The effect observes its intent through a different DB connection.
            async with PostgresUnitOfWork(factory) as observed:
                unresolved = await observed.operations.list_unresolved()
                assert any(i.kind == "push_branch" and i.run_id == case.run_id for i in unresolved)
            pushes.append(approved_sha)
            if failure == "push_uncertain":
                from forge.release.git_push import ManagedPushError

                raise ManagedPushError()
            writes.branch_shas[policy.github_repository, worktree.identity.branch] = approved_sha
            writes.branch_shas[policy.github_repository, "main"] = git.worktree.base_sha

    service = ReleaseService(
        PrEvidenceValidator(
            case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _: git, read
        ),
        writes,
        lambda _: Push(),
        OperationExecutor(PostgresOperationRepository(factory), execution_lease_seconds=1),
    )
    from forge.release.github_write import GitHubWriteError

    calls = []
    original_create = writes.create_pull_request

    async def create(*args, **kwargs):
        calls.append(args)
        if failure in {"create_uncertain", "intervention_crash"}:
            raise GitHubWriteError("uncertain")
        return await original_create(*args, **kwargs)

    writes.create_pull_request = create
    if failure in {None, "ambiguous_reconcile"}:
        writes.crash_next_write = True
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(FakeGitHubWriteCrash):
                await service.publish(command, work)
        if failure == "ambiguous_reconcile":
            from dataclasses import replace

            writes.pull_requests[repository, 2] = replace(
                writes.pull_requests[repository, 1], number=2, node_id="PR_2"
            )
    if failure == "intervention_crash":
        async with PostgresUnitOfWork(factory) as work:

            async def crash(*args, **kwargs):
                raise RuntimeError("intervention crash")

            work.runs.intervene = crash
            with pytest.raises(RuntimeError, match="intervention crash"):
                await service.publish(command, work)
    async with PostgresUnitOfWork(factory) as work:
        await service.publish(command, work)
    async with PostgresUnitOfWork(factory) as work:
        await service.publish(command, work)
        run = await work.runs.get(case.run_id)
        record = await work.releases.get_for_run(case.run_id)
        if failure:
            from forge.domain.operation import OperationStatus

            assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
            assert run.version == command.expected_run_version + 1
            assert record is None
            events = [
                e
                for e in await work.events.list_after(run.id, 0)
                if e.event_type == "run.publication_intervention"
            ]
            assert len(events) == 1
            intent = await work.operations.get_by_idempotency_key(
                events[0].payload["operation_key"]
            )
            assert intent.status is OperationStatus.NEEDS_RECONCILIATION
            assert (
                await work.auth.get_approval(approval_id=approval_id)
            ).invalidated_at is not None
            assert await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:1") is None
            assert len(pushes) == 1 and len(calls) == (0 if failure == "push_uncertain" else 1)
            return
        assert run.state is RunState.MONITORING_PR
        assert record.pull_request.number == 1
    assert len(pushes) == len(writes.pull_requests) == 1
