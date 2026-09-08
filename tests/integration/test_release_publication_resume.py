"""Publication resume retains approval and external operation identity."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.command import CommandStatus
from forge.domain.github import MergeProtection
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.fake_github_write import FakeGitHubWrite
from test_pr_approval import authorized
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def resumed_release(
    case, source, factory, *, continued_type=None, renewed=False, after_pause=None
):
    commands = PostgresCommandRepository(factory)
    actor = uuid4()
    async with PostgresUnitOfWork(factory) as work:
        row = await work.session.get(RunCommand, source.id)
        row.available_at = datetime.now(UTC) + timedelta(hours=1)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        run = await work.runs.get(case.run_id)
        await work.commands.enqueue(
            run_id=case.run_id,
            command_type="pause",
            idempotency_key=f"{case.run_id}:pause:{run.version}",
            payload={},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    pause = await commands.claim_next(
        worker_id="control", lease_seconds=120, lane=CommandLane.CONTROL
    )
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
    await commands.complete(pause.id, worker_id=pause.lease_owner)
    if after_pause is not None:
        await after_pause()
    async with PostgresUnitOfWork(factory) as work:
        paused = await work.runs.get(case.run_id)
        await work.commands.enqueue(
            run_id=case.run_id,
            command_type="resume",
            idempotency_key=f"{case.run_id}:resume:{paused.version}",
            payload={},
            expected_run_version=paused.version,
            actor_id=actor,
        )
        await work.commit()
    resume = await commands.claim_next(worker_id="resume", lease_seconds=120)
    assert resume.command_type == "resume"
    if renewed:
        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.get(RunCommand, source.id)
            row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=120)
            await work.commit()
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
    await commands.complete(resume.id, worker_id=resume.lease_owner)
    continued = await commands.claim_next(worker_id="continued", lease_seconds=120)
    assert continued.command_type == (continued_type or source.command_type)
    assert continued.id != source.id
    assert continued.actor_id == source.actor_id
    return continued


@pytest.mark.parametrize(
    "phase",
    [
        "before_effect",
        "settled_push",
        "drift",
        "repeated",
        "published",
        "published_renewed",
        "published_receipt",
        "startup_push",
        "startup_pr",
        "startup_push_paused",
        "startup_pr_paused",
        "startup_pr_composed",
        "startup_bad_intent",
    ],
)
async def test_resumed_publication_preserves_operation_authority(
    tmp_path, workflow_session_factory, monkeypatch, phase
):
    factory = workflow_session_factory
    case, git, read, approve, approval_command, approval_id = await authorized(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        await approve(approval_command, work)
        policy = (await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)).policy
    commands = PostgresCommandRepository(factory)
    await commands.complete(approval_command.id, worker_id=approval_command.lease_owner)
    source = await commands.claim_next(worker_id="publisher", lease_seconds=120)
    writes = FakeGitHubWrite()
    writes.branch_shas[policy.github_repository, "main"] = git.worktree.base_sha
    pushes = []

    class Push:
        async def push(self, worktree, policy, approved_sha):
            pushes.append(approved_sha)
            writes.branch_shas[policy.github_repository, worktree.identity.branch] = approved_sha

    validator = PrEvidenceValidator(
        case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _: git, read
    )
    service = ReleaseService(
        validator,
        writes,
        lambda _: Push(),
        OperationExecutor(PostgresOperationRepository(factory), execution_lease_seconds=1),
    )
    recover_after_pause = None
    if phase.startswith("startup_"):
        from dataclasses import replace

        from forge.application.services.recovery import RecoveryError, RecoveryService
        from forge.persistence.models import OperationIntent
        from forge.release.controller import ReleaseReconciliationRequired
        from forge.worker.publication_recovery import publication_recovery_adapters
        from sqlalchemy import select

        operations = service._executor._operations
        complete = operations.complete
        kind = "create_pr" if phase.startswith("startup_pr") else "push_branch"

        async def crash_receipt(intent_id, *args, **kwargs):
            intent = await operations.get(intent_id)
            if intent.kind == kind:
                raise RuntimeError("after publication effect before receipt")
            return await complete(intent_id, *args, **kwargs)

        monkeypatch.setattr(operations, "complete", crash_receipt)
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(RuntimeError, match="before receipt"):
                await service.publish(source, work)
        monkeypatch.setattr(operations, "complete", complete)
        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.scalar(
                select(OperationIntent).where(
                    OperationIntent.run_id == case.run_id,
                    OperationIntent.operation_kind == kind,
                )
            )
            row.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await work.commit()
        adapters = publication_recovery_adapters(factory, validator, writes)
        composed = None
        if phase == "startup_pr_composed":
            from forge.settings import Settings
            from forge.worker.composition import ReleaseDependencies, compose_worker_handlers
            from forge.worker.delivery_runtime import DeliveryRuntime

            class Runtime(DeliveryRuntime):
                def git(self, policy):
                    return git

            def forbidden_push(policy):
                raise AssertionError("startup recovery must not acquire a push capability")

            settings = Settings(data_root=tmp_path, prompt_root=tmp_path / "prompts")
            composed = compose_worker_handlers(
                settings,
                factory,
                agent_gateway=case.gateway,
                delivery_runtime=Runtime(settings, factory, case.artifact_store),
                release_dependencies=ReleaseDependencies(read, writes, forbidden_push),
            )
            adapters = composed.recovery_adapters
        pending = (await operations.list_unresolved())[0]
        with pytest.raises(RecoveryError, match="cannot invoke"):
            await adapters[kind].invoke(pending)
        if phase == "startup_bad_intent":
            # A reviewed push or altered request must not inherit original publication authority.
            altered = replace(pending, request_payload=dict(pending.request_payload) | {"extra": True})
            with pytest.raises(ReleaseReconciliationRequired):
                await adapters[kind].reconcile(altered)

        async def recover_startup():
            recovered = await RecoveryService(operations).reconcile_all(adapters)
            assert len(recovered) == 1
            assert len(pushes) == 1
            assert len(writes.pull_requests) == (1 if kind == "create_pr" else 0)

        if phase.endswith("_paused"):
            recover_after_pause = recover_startup
        else:
            await recover_startup()
        if composed is not None:
            await composed.aclose()
    if phase == "settled_push":
        original = service._current
        calls = 0

        async def crash(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("after settled push")
            return await original(*args)

        monkeypatch.setattr(service, "_current", crash)
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(RuntimeError, match="settled push"):
                await service.publish(source, work)
        monkeypatch.setattr(service, "_current", original)
    if phase.startswith("published"):
        async with PostgresUnitOfWork(factory) as work:
            await service.publish(source, work)
        if phase != "published":
            if phase == "published_receipt":
                async with PostgresUnitOfWork(factory) as work:
                    record = await work.releases.get_for_run(case.run_id)
                    from forge.persistence.models import OperationIntent

                    row = await work.session.get(OperationIntent, record.publication_intent_id)
                    row.outcome_payload = {}
                    await work.commit()
            with pytest.raises(CommandRecoveryRequired):
                await resumed_release(
                    case,
                    source,
                    factory,
                    continued_type="monitor_pr",
                    renewed=phase == "published_renewed",
                )
            async with PostgresUnitOfWork(factory) as work:
                assert (await work.commands.get(source.id)).status is CommandStatus.LEASED
                assert (await work.runs.get(case.run_id)).state is RunState.PAUSED
            assert len(pushes) == 1
            assert len(writes.pull_requests) == 1
            return
        continued = await resumed_release(case, source, factory, continued_type="monitor_pr")

        async with PostgresUnitOfWork(factory) as work:
            assert (await work.commands.get(source.id)).status is CommandStatus.COMPLETED
            await ReleaseMonitor(case.artifact_store, validator, read, writes)(continued, work)
        assert len(pushes) == 1
        assert len(writes.pull_requests) == 1
        return
    continued = await resumed_release(case, source, factory, after_pause=recover_after_pause)
    if phase == "repeated":
        continued = await resumed_release(case, continued, factory)
    if phase == "drift":
        git.head = "f" * 40
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await service.publish(continued, work)
    assert len(pushes) == (0 if phase == "drift" else 1)
    assert len(writes.pull_requests) == (0 if phase == "drift" else 1)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is (
            RunState.AWAITING_HUMAN_INTERVENTION if phase == "drift" else RunState.MONITORING_PR
        )
        if phase == "drift":
            assert (
                await work.auth.get_approval(approval_id=approval_id)
            ).invalidated_at is not None
            return
        assert (
            await validator.validate_published(work, run.id, approval_id)
        ).candidate_head == git.head
        queued = await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:1")
        row = await work.session.get(RunCommand, queued.id)
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    await commands.complete(continued.id, worker_id=continued.lease_owner)
    poll = await commands.claim_next(worker_id="monitor", lease_seconds=120)
    read.merge_protections[policy.github_repository.casefold(), "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )
    async with PostgresUnitOfWork(factory) as work:
        await ReleaseMonitor(case.artifact_store, validator, read, writes)(poll, work)
