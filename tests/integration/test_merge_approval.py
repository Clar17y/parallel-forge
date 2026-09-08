from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from forge.application.handlers.merge import ApproveMergeHandler
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.merge import MergeController
from test_pr_monitoring import published
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "head",
        "base",
        "check",
        "local",
        "artifact",
        "protection",
        "execution_check",
        "execution_conflict",
        "execution_refused",
        "execution_uncertain",
        "execution_mismatch",
        "reconcile_mismatch",
        "intervention_crash",
        "preflight_unavailable",
        "consumed_artifact",
        "consumed_missing",
    ],
)
async def test_merge_approval_consumes_exact_gate_or_invalidates_without_merge(
    tmp_path, workflow_session_factory, drift
):
    factory = workflow_session_factory
    accepted = drift in (
        None,
        "execution_check",
        "execution_conflict",
        "execution_refused",
        "execution_uncertain",
        "execution_mismatch",
        "reconcile_mismatch",
        "intervention_crash",
        "preflight_unavailable",
        "consumed_artifact",
        "consumed_missing",
    )
    case, git, read, writes, pr_validator, poll, policy, _ = await published(tmp_path, factory)
    key = policy.github_repository.casefold()
    read.checks[key, git.head] = (CheckSnapshot("ci", "completed", "success", head_sha=git.head),)
    read.merge_protections[key, "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )
    async with PostgresUnitOfWork(factory) as work:
        await ReleaseMonitor(case.artifact_store, pr_validator, read, writes)(poll, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(poll.id, worker_id=poll.lease_owner)
    approval_id, actor = uuid4(), uuid4()
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
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
    command = await commands.claim_next(worker_id="merge-approval", lease_seconds=120)
    if drift == "head":
        writes.pull_requests[policy.github_repository, 1] = replace(
            writes.pull_requests[policy.github_repository, 1], head_sha="f" * 40
        )
    elif drift == "base":
        read.bases[key, "main"] = "f" * 40
    elif drift == "check":
        read.checks[key, git.head] = ()
    elif drift == "local":
        git.head = "f" * 40
    elif drift == "protection":
        read.merge_protections[key, "main"] = MergeProtection(
            True, False, True, "classic", required_check_names=("ci",)
        )
    elif drift == "artifact":
        original = case.artifact_store.open_bytes

        async def corrupt(digest, **kwargs):
            return (
                b"invalid"
                if digest == run.pending_evidence_digest
                else await original(digest, **kwargs)
            )

        case.artifact_store.open_bytes = corrupt
    handler = ApproveMergeHandler(
        MergeEvidenceValidator(case.artifact_store, pr_validator, MergeController(read, writes))
    )
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await handler(command, work)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is (RunState.MERGING if accepted else RunState.MONITORING_PR)
        approval = await work.auth.get_approval(approval_id=approval_id)
        consumed_digest = approval.evidence_digest
        assert (approval.invalidated_at is not None) == (not accepted)
        merge = await work.commands.get_by_idempotency_key(f"{run.id}:merge-pr:{run.version}")
        assert (merge is not None) == accepted
        assert not writes.pull_requests[policy.github_repository, 1].merged
    if accepted:
        from forge.release.fake_github_write import FakeGitHubWriteCrash
        from forge.settings import Settings
        from forge.worker.composition import ReleaseDependencies, compose_worker_handlers
        from forge.worker.delivery_runtime import DeliveryRuntime

        await commands.complete(command.id, worker_id=command.lease_owner)
        merge_command = await commands.claim_next(worker_id="merger", lease_seconds=120)

        class Runtime(DeliveryRuntime):
            def git(self, policy):
                return git

        def unused_push(policy):
            raise AssertionError("merge execution must not push")

        settings = Settings(data_root=tmp_path, prompt_root=tmp_path / "prompts")
        handlers = compose_worker_handlers(
            settings,
            factory,
            agent_gateway=case.gateway,
            delivery_runtime=Runtime(settings, factory, case.artifact_store),
            release_dependencies=ReleaseDependencies(read, writes, unused_push),
        )
        if drift in {"consumed_artifact", "consumed_missing"}:
            from forge.artifacts._errors import ArtifactIntegrityError

            merge_store = handlers["merge_pr"].__self__._evidence._store
            original_open = merge_store.open_bytes

            async def unavailable_evidence(digest, **kwargs):
                if digest == consumed_digest:
                    if drift == "consumed_missing":
                        raise ArtifactIntegrityError("missing test artifact")
                    return b"invalid"
                return await original_open(digest, **kwargs)

            merge_store.open_bytes = unavailable_evidence
            for _ in range(2):
                async with PostgresUnitOfWork(factory) as work:
                    await handlers["merge_pr"](merge_command, work)
            async with PostgresUnitOfWork(factory) as work:
                run = await work.runs.get(case.run_id)
                assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
                assert run.version == merge_command.expected_run_version + 1
                assert (
                    await work.auth.get_approval(approval_id=approval_id)
                ).invalidated_at is not None
                events = [
                    e
                    for e in await work.events.list_after(run.id, 0)
                    if e.event_type == "run.merge_evidence_rejected"
                ]
                assert len(events) == 1
                assert events[0].payload["evidence_digest"] == consumed_digest
                assert (await work.releases.get_for_run(run.id)).merge_intent_id is None
                assert await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:2") is None
            assert not writes.pull_requests[policy.github_repository, 1].merged
            await handlers.aclose()
            return
        if drift == "preflight_unavailable":
            from forge.release.github_client import GitHubClientError

            merge_calls = []
            original_merge = writes.merge_pull_request

            async def count_merge(*args):
                merge_calls.append(args)
                return await original_merge(*args)

            async def unavailable_pull_request(*_args):
                raise GitHubClientError("unavailable")

            writes.merge_pull_request = count_merge
            writes.get_pull_request = unavailable_pull_request
            for _ in range(2):
                async with PostgresUnitOfWork(factory) as work:
                    await handlers["merge_pr"](merge_command, work)
            async with PostgresUnitOfWork(factory) as work:
                run = await work.runs.get(case.run_id)
                assert run.state is RunState.MONITORING_PR
                assert (
                    await work.auth.get_approval(approval_id=approval_id)
                ).invalidated_at is not None
                assert await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:2")
                assert not writes.pull_requests[policy.github_repository, 1].merged
            assert merge_calls == []
            await handlers.aclose()
            return
        if drift in {
            "execution_uncertain",
            "execution_mismatch",
            "reconcile_mismatch",
            "intervention_crash",
        }:
            from forge.domain.operation import OperationStatus
            from forge.release.github_write import GitHubWriteError

            calls = []
            original_merge = writes.merge_pull_request

            async def uncertain_merge(*args):
                calls.append(args)
                if drift in {"execution_uncertain", "intervention_crash"}:
                    raise GitHubWriteError("uncertain")
                pull = await original_merge(*args)
                if drift == "execution_mismatch":
                    return replace(pull, node_id="wrong-node")
                return pull

            writes.merge_pull_request = uncertain_merge
            if drift == "intervention_crash":
                async with PostgresUnitOfWork(factory) as work:

                    async def crash_intervention(*args, **kwargs):
                        raise RuntimeError("intervention commit crash")

                    work.runs.intervene = crash_intervention
                    with pytest.raises(RuntimeError, match="intervention commit crash"):
                        await handlers["merge_pr"](merge_command, work)
            if drift == "reconcile_mismatch":
                writes.crash_next_write = True
                async with PostgresUnitOfWork(factory) as work:
                    with pytest.raises(FakeGitHubWriteCrash):
                        await handlers["merge_pr"](merge_command, work)
                writes.pull_requests[policy.github_repository, 1] = replace(
                    writes.pull_requests[policy.github_repository, 1], node_id="wrong-node"
                )
            for _ in range(2):
                async with PostgresUnitOfWork(factory) as work:
                    await handlers["merge_pr"](merge_command, work)
            async with PostgresUnitOfWork(factory) as work:
                run = await work.runs.get(case.run_id)
                assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
                assert run.version == merge_command.expected_run_version + 1
                events = [
                    e
                    for e in await work.events.list_after(run.id, 0)
                    if e.event_type == "run.merge_intervention"
                ]
                assert len(events) == 1
                intent = await work.operations.get_by_idempotency_key(
                    events[0].payload["operation_key"]
                )
                assert intent.status is OperationStatus.NEEDS_RECONCILIATION
                assert intent.id == UUID(events[0].payload["merge_intent_id"])
                assert (
                    await work.auth.get_approval(approval_id=approval_id)
                ).invalidated_at is not None
                assert (await work.releases.get_for_run(run.id)).merge_intent_id is None
                assert await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:2") is None
            assert len(calls) == 1
            await handlers.aclose()
            return
        if drift in {"execution_check", "execution_conflict", "execution_refused"}:
            merge_calls = []
            if drift == "execution_check":
                read.checks[key, git.head] = ()
            else:
                from forge.release.github_write import GitHubWriteError

                async def rejected_merge(*args):
                    merge_calls.append(args)
                    raise GitHubWriteError("rejected" if drift == "execution_refused" else "stale")

                writes.merge_pull_request = rejected_merge
                # The operation failure commits separately from run settlement.
                # A crash here must replay the failed receipt without another PUT.
                async with PostgresUnitOfWork(factory) as work:

                    async def crash_settlement(*args, **kwargs):
                        raise RuntimeError("settlement crash")

                    work.runs.transition = crash_settlement
                    with pytest.raises(RuntimeError, match="settlement crash"):
                        await handlers["merge_pr"](merge_command, work)
            for _ in range(2):
                async with PostgresUnitOfWork(factory) as work:
                    await handlers["merge_pr"](merge_command, work)
            async with PostgresUnitOfWork(factory) as work:
                run = await work.runs.get(case.run_id)
                assert run.state is RunState.MONITORING_PR
                approval = await work.auth.get_approval(approval_id=approval_id)
                assert approval.invalidated_at is not None
                queued = await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:2")
                assert queued is not None and queued.expected_run_version == run.version
                assert not writes.pull_requests[policy.github_repository, 1].merged
                events = await work.events.list_after(run.id, 0)
                assert len([e for e in events if e.event_type == "run.merge_rejected"]) == 1
                rejected = next(e for e in events if e.event_type == "run.merge_rejected")
                intent = await work.operations.get_by_idempotency_key(
                    rejected.payload["operation_key"]
                )
                if drift == "execution_check":
                    assert intent is None and merge_calls == []
                else:
                    from forge.domain.operation import OperationStatus

                    assert intent.status is OperationStatus.FAILED
                    assert intent.error == "merge_remote_rejected"
                    assert len(merge_calls) == 1
            read.checks[key, git.head] = ()
            from datetime import UTC, datetime, timedelta

            from forge.persistence.models import RunCommand

            await commands.complete(merge_command.id, worker_id=merge_command.lease_owner)
            async with PostgresUnitOfWork(factory) as work:
                row = await work.session.get(RunCommand, queued.id)
                row.available_at = datetime.now(UTC) - timedelta(seconds=1)
                await work.commit()
            next_poll = await commands.claim_next(
                worker_id="monitor-after-rejection", lease_seconds=120
            )
            async with PostgresUnitOfWork(factory) as work:
                await handlers["monitor_pr"](next_poll, work)
            async with PostgresUnitOfWork(factory) as work:
                run = await work.runs.get(case.run_id)
                assert run.state is RunState.MONITORING_PR
                assert (
                    await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:3") is not None
                )
            await handlers.aclose()
            return
        writes.crash_next_write = True
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(FakeGitHubWriteCrash):
                await handlers["merge_pr"](merge_command, work)
        # The base can advance after a successful remote merge. Recovery must
        # observe the completed effect rather than rerun the old open-PR gate.
        read.bases[key, "main"] = "e" * 40
        for _ in range(2):
            async with PostgresUnitOfWork(factory) as work:
                await handlers["merge_pr"](merge_command, work)
        await handlers.aclose()
        async with PostgresUnitOfWork(factory) as work:
            run = await work.runs.get(case.run_id)
            assert run.state is RunState.COMPLETED
            recorded = await work.releases.get_for_run(run.id)
            assert recorded.pull_request.merged
            assert recorded.pull_request.merge_sha != git.head
            assert (
                recorded.pull_request.merge_sha
                == writes.pull_requests[policy.github_repository, 1].merge_sha
            )
            assert recorded.merge_intent_id is not None
