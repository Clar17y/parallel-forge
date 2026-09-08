"""Merge continuation retains the original approved operation across a pause."""

from uuid import uuid4

import pytest
from forge.application.handlers.merge import ApproveMergeHandler
from forge.application.services.merge import MergeService
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.merge import MergeController
from test_pr_monitoring import published
from test_release_publication_resume import resumed_release
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize(
    "phase",
    [
        "before_effect",
        "settled_merge",
        "drift",
        "repeated",
        "terminal_ack",
        "terminal_corrupt",
        "terminal_renewed",
        "startup_merge",
        "startup_paused",
        "startup_bad_artifact",
    ],
)
async def test_resumed_merge_preserves_approved_operation(
    tmp_path, workflow_session_factory, phase, monkeypatch
):
    factory = workflow_session_factory
    case, git, read, writes, validator, poll, policy, _ = await published(tmp_path, factory)
    repository = policy.github_repository.casefold()
    read.checks[repository, git.head] = (
        CheckSnapshot("ci", "completed", "success", head_sha=git.head),
    )
    read.merge_protections[repository, "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )
    async with PostgresUnitOfWork(factory) as work:
        await ReleaseMonitor(case.artifact_store, validator, read, writes)(poll, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(poll.id, worker_id=poll.lease_owner)
    actor, approval_id = uuid4(), uuid4()
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
    approved = await commands.claim_next(worker_id="approver", lease_seconds=120)
    controller = MergeController(read, writes)
    evidence = MergeEvidenceValidator(case.artifact_store, validator, controller)
    async with PostgresUnitOfWork(factory) as work:
        await ApproveMergeHandler(evidence)(approved, work)
    await commands.complete(approved.id, worker_id=approved.lease_owner)
    source = await commands.claim_next(worker_id="merger", lease_seconds=120)
    service = MergeService(
        evidence,
        controller,
        OperationExecutor(PostgresOperationRepository(factory), execution_lease_seconds=1),
    )
    calls = []
    original_merge = writes.merge_pull_request

    async def merge(*args, **kwargs):
        calls.append(args)
        return await original_merge(*args, **kwargs)

    writes.merge_pull_request = merge
    recover_after_pause = None
    if phase.startswith("startup_"):
        from datetime import UTC, datetime, timedelta

        from forge.application.services.recovery import RecoveryError, RecoveryService
        from forge.persistence.models import OperationIntent
        from forge.worker.release_recovery import merge_recovery_adapters
        from sqlalchemy import select

        operations = service._executor._operations
        complete = operations.complete

        async def crash_receipt(*_args, **_kwargs):
            raise RuntimeError("after remote merge before receipt")

        operations.complete = crash_receipt
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(RuntimeError, match="before receipt"):
                await service.execute(source, work)
        operations.complete = complete
        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.scalar(
                select(OperationIntent).where(
                    OperationIntent.run_id == case.run_id,
                    OperationIntent.operation_kind == "merge_pr",
                )
            )
            row.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await work.commit()
        adapters = merge_recovery_adapters(factory, evidence, controller)
        pending = (await operations.list_unresolved())[0]
        with pytest.raises(RecoveryError, match="cannot invoke"):
            await adapters["merge_pr"].invoke(pending)

        async def recover_startup():
            recovered = await RecoveryService(operations).reconcile_all(adapters)
            assert len(recovered) == 1 and len(calls) == 1

        if phase == "startup_bad_artifact":
            async with PostgresUnitOfWork(factory) as work:
                digest = (await work.auth.get_approval(approval_id=approval_id)).evidence_digest
            original_open = case.artifact_store.open_bytes

            async def corrupt(digest_arg, **kwargs):
                return b"{}" if digest_arg == digest else await original_open(digest_arg, **kwargs)

            monkeypatch.setattr(case.artifact_store, "open_bytes", corrupt)
            with pytest.raises(RecoveryError, match="unresolved"):
                await recover_startup()
            assert len(calls) == 1
            return
        if phase == "startup_paused":
            recover_after_pause = recover_startup
        else:
            await recover_startup()
    if phase == "settled_merge":
        async with PostgresUnitOfWork(factory) as work:

            async def crash(*args, **kwargs):
                raise RuntimeError("after settled merge")

            work.runs.transition = crash
            with pytest.raises(RuntimeError, match="settled merge"):
                await service.execute(source, work)
    continued = await resumed_release(case, source, factory, after_pause=recover_after_pause)
    if phase == "repeated":
        continued = await resumed_release(case, continued, factory)
    if phase == "drift":
        read.checks[repository, git.head] = ()
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await service.execute(continued, work)
    assert len(calls) == (0 if phase == "drift" else 1)
    if phase.startswith("terminal_"):
        from datetime import UTC, datetime, timedelta

        from forge.application.ports.commands import CommandRecoveryRequired
        from forge.application.services.terminal_recovery import TerminalMergeRecovery
        from forge.domain.command import CommandStatus
        from forge.persistence.models import OperationIntent, RunCommand

        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.get(RunCommand, continued.id)
            row.lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=120 if phase == "terminal_renewed" else -1
            )
            if phase == "terminal_corrupt":
                record = await work.releases.get_for_run(case.run_id)
                intent = await work.session.get(OperationIntent, record.merge_intent_id)
                intent.outcome_payload = {}
            await work.commit()
        assert await commands.claim_next(worker_id="restart", lease_seconds=120) is None
        recovery = TerminalMergeRecovery(factory)
        if phase in {"terminal_corrupt", "terminal_renewed"}:
            if phase == "terminal_corrupt":
                with pytest.raises(CommandRecoveryRequired, match="receipt differs"):
                    await recovery.reconcile_all()
            else:
                assert await recovery.reconcile_all() == ()
            async with PostgresUnitOfWork(factory) as work:
                assert (await work.commands.get(continued.id)).status is CommandStatus.LEASED
            assert len(calls) == 1
            return
        assert await recovery.reconcile_all() == (continued.id,)
        assert await recovery.reconcile_all() == ()
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.commands.get(continued.id)).status is CommandStatus.COMPLETED
        assert len(calls) == 1
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is (RunState.MONITORING_PR if phase == "drift" else RunState.COMPLETED)
        if phase != "drift":
            record = await work.releases.get_for_run(run.id)
            assert (
                record.pull_request.merge_sha
                == writes.pull_requests[policy.github_repository, 1].merge_sha
            )
            assert record.pull_request.merge_sha != git.head
