"""Controller checks execute only after durable admission and publish exact evidence."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from forge.application.ports.runner import CommandResult, CommandTerminalResult
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.delivery_preparation import DeliveryPreparationService
from forge.application.services.recovery import OperationExecutor
from forge.application.services.validation import ValidationService
from forge.domain.evidence import decode_evidence_manifest
from forge.domain.policy import CommandSpec, RunnerMode, StepKind
from forge.domain.run import RunState
from forge.domain.validation import command_spec_digest
from forge.persistence.models import OperationIntent, Step
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_preparation import _PersistingProvisioner, _prepared_command
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


class _Git:
    def __init__(self, worktree):
        self.worktree = worktree
        self.head = "b" * 40

    def inspect_worktree(self, identity, base_sha):
        assert identity == self.worktree.identity and base_sha == self.worktree.base_sha
        return self.worktree

    def head_sha(self, worktree):
        assert worktree == self.worktree
        return self.head

    def is_ancestor(self, worktree):
        assert worktree == self.worktree
        return True


class _CheckingRunner:
    def __init__(self, factory, run_id, store):
        self.factory, self.run_id, self.store = factory, run_id, store
        self.calls = []
        self.policy = None

    def create(self, worktree, policy):
        self.policy = policy
        return self

    async def run_terminal(self, request):
        async with self.factory() as session:
            intent = await session.scalar(
                select(OperationIntent).where(
                    OperationIntent.run_id == self.run_id,
                    OperationIntent.operation_kind == "controller_named_check",
                    OperationIntent.status == "PENDING",
                    OperationIntent.request_payload["command_name"].astext == request.command_name,
                )
            )
            assert intent is not None and intent.status == "PENDING"
            step = await session.get(Step, intent.request_payload["step_id"])
            assert step is not None and step.kind == "validate" and step.status == "RUNNING"
        request.launch_ownership.accept_launch()
        self.calls.append(request.command_name)
        outputs = []
        for stream in ("stdout", "stderr"):
            wire = json.dumps(
                {
                    "captured_byte_count": 0,
                    "encoding": "utf-8-replacement",
                    "original_byte_count": 0,
                    "stream": stream,
                    "text": "",
                    "truncated": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            outputs.append(
                await self.store.put_bytes(
                    wire, media_type="application/vnd.forge.command-output+json"
                )
            )
        spec = next(
            command for command in self.policy.commands if command.name == request.command_name
        )
        return CommandTerminalResult(
            result=CommandResult(
                command_name=spec.name,
                kind=spec.kind,
                command_digest=command_spec_digest(spec),
                policy_version=self.policy.version,
                exit_code=0,
                timed_out=False,
                started_at=datetime.now(UTC),
                duration_ms=1,
                stdout_digest=outputs[0].digest,
                stderr_digest=outputs[1].digest,
                runner_mode=RunnerMode.DOCKER,
                image_digest="sha256:" + "d" * 64,
                network_enabled=False,
                stdout_original_byte_count=0,
                stderr_original_byte_count=0,
                stdout_truncated=False,
                stderr_truncated=False,
                unsandboxed=False,
            ),
            caller_cancelled=False,
        )


async def _case(tmp_path, factory):
    specs = tuple(
        CommandSpec(
            name=name,
            kind=StepKind.TEST,
            argv=("pytest",),
            timeout_seconds=10,
            required=name != "optional",
        )
        for name in ("unit", "optional", "lint")
    )
    case, _, prepare, commands = await _prepared_command(tmp_path, factory, commands=specs)
    provisioner = _PersistingProvisioner(factory, tmp_path / "worktree")
    async with PostgresUnitOfWork(factory) as work:
        await DeliveryPreparationService(
            ApprovedPlanLoader(case.artifact_store), provisioner
        ).execute(prepare, work)
    await commands.complete(prepare.id, worker_id="test-worker")
    implement = await commands.claim_next(worker_id="test-worker", lease_seconds=60)
    await commands.complete(implement.id, worker_id="test-worker")
    async with PostgresUnitOfWork(factory) as work:
        approved = await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)
        run = await work.runs.transition(
            case.run_id, approved.run.version, RunState.VALIDATING, "test.developer_completed", {}
        )
        await work.commit()
    from forge.application.ports.worktrees import ManagedWorktree
    from forge.domain.resource import WorktreeIdentity

    worktree = ManagedWorktree(
        identity=WorktreeIdentity.for_run(run.project_id, run.id, run.branch_name, False),
        path=tmp_path / "worktree",
        base_sha=run.base_sha,
    )
    queued = await commands.enqueue(
        run_id=case.run_id,
        command_type="validate",
        idempotency_key=f"{case.run_id}:validate:1",
        payload={"semantic_attempt": 1},
        expected_run_version=run.version,
        actor_id=approved.approval_actor_id,
    )
    command = await commands.claim_next(worker_id="test-worker", lease_seconds=60)
    assert command.id == queued.id
    runner, git = _CheckingRunner(factory, case.run_id, case.artifact_store), _Git(worktree)

    async def environment(run, policy, worktree):
        return {}

    service = ValidationService(
        case.artifact_store,
        uow_factory=lambda: PostgresUnitOfWork(factory),
        operation_executor=OperationExecutor(PostgresOperationRepository(factory)),
        git_factory=lambda policy: git,
        runner_factory=runner,
        environment_resolver=environment,
    )
    return case, command, service, runner


async def test_required_checks_have_committed_intents_and_publish_in_policy_order(
    tmp_path, workflow_session_factory
):
    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        evidence = await service.execute(command, work)
    assert runner.calls == ["unit", "lint"]
    manifest = decode_evidence_manifest(
        await case.artifact_store.open_bytes(evidence.manifest_digest)
    )
    assert {member.command_name for member in manifest.members} == {"unit", "lint"}
    assert manifest.head_sha == "b" * 40


async def test_validation_rejects_a_durably_queued_unapproved_actor(
    tmp_path, workflow_session_factory
):
    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.persistence.models import RunCommand

    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    async with workflow_session_factory() as session, session.begin():
        row = await session.get(RunCommand, command.id)
        row.actor_id = None
    delivered = replace(command, actor_id=None)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired, match="actor"):
            await service.execute(delivered, work)
    assert runner.calls == []
    async with workflow_session_factory() as session:
        assert (
            await session.scalar(
                select(OperationIntent).where(OperationIntent.run_id == case.run_id)
            )
            is None
        )


@pytest.mark.parametrize("prior", [None, "", "00000000-0000-0000-0000-000000000000", 1])
async def test_invalid_prior_review_binding_never_dispatches_checks(
    tmp_path, workflow_session_factory, prior
):
    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.persistence.models import RunCommand

    _case_data, command, service, runner = await _case(tmp_path, workflow_session_factory)
    payload = {"semantic_attempt": 1, "prior_review_evidence_set_id": prior}
    async with workflow_session_factory() as session, session.begin():
        row = await session.get(RunCommand, command.id)
        row.payload = payload
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired, match="prior review"):
            await service.execute(replace(command, payload=payload), work)
    assert runner.calls == []


async def test_validation_evidence_cannot_impersonate_prior_review(
    tmp_path, workflow_session_factory
):
    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.persistence.models import RunCommand

    _case_data, command, service, runner = await _case(tmp_path, workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        first = await service.execute(command, work)
    payload = {"semantic_attempt": 1, "prior_review_evidence_set_id": str(first.evidence_set_id)}
    async with workflow_session_factory() as session, session.begin():
        row = await session.get(RunCommand, command.id)
        row.payload = payload
    runner.calls.clear()
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired, match="prior review"):
            await service.execute(replace(command, payload=payload), work)
    assert runner.calls == []


async def test_completed_validation_replays_verified_receipts_without_runner(
    tmp_path, workflow_session_factory
):
    _case_data, command, service, runner = await _case(tmp_path, workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        first = await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        replay = await service.execute(command, work)
    assert replay == first
    assert runner.calls == ["unit", "lint"]


async def test_candidate_changed_during_manifest_storage_is_not_published(
    tmp_path, workflow_session_factory
):
    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.persistence.models import EvidenceSet

    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    git = service._git_factory(None)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        original = work.artifacts.record

        async def change_candidate(*args, **kwargs):
            artifact = await original(*args, **kwargs)
            if kwargs.get("producer_type") == "evidence_set":
                git.head = "c" * 40
            return artifact

        work.artifacts.record = change_candidate
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert runner.calls == ["unit", "lint"]
    async with workflow_session_factory() as session:
        assert (
            await session.scalar(select(EvidenceSet).where(EvidenceSet.run_id == case.run_id))
            is None
        )


async def test_pause_after_atomic_check_retains_receipt_and_prevents_next_check(
    tmp_path, workflow_session_factory
):
    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.persistence.models import ArtifactLineage, Run

    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    original = runner.run_terminal

    async def pause_after_check(request):
        result = await original(request)
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            run = await work.runs.get(case.run_id)
            await work.runs.pause(run.id, run.version, "test.paused", {})
            await work.commit()
        return result

    runner.run_terminal = pause_after_check
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert runner.calls == ["unit"]
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        assert run.state == "PAUSED"
        receipt = await session.scalar(
            select(ArtifactLineage).where(
                ArtifactLineage.run_id == case.run_id,
                ArtifactLineage.producer_kind == "controller_named_check",
            )
        )
        assert receipt is not None


async def test_changed_command_during_check_cannot_dispatch_next_check(
    tmp_path, workflow_session_factory
):
    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.persistence.models import RunCommand

    _case_data, command, service, runner = await _case(tmp_path, workflow_session_factory)
    original = runner.run_terminal

    async def change_command(request):
        result = await original(request)
        async with workflow_session_factory() as session, session.begin():
            row = await session.get(RunCommand, command.id)
            row.payload = {"semantic_attempt": 2}
        return result

    runner.run_terminal = change_command
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert runner.calls == ["unit"]


async def test_cancellation_joins_terminal_receipt_before_uow_closes(
    tmp_path, workflow_session_factory
):
    from forge.persistence.models import ArtifactLineage

    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    original = runner.run_terminal
    entered, cancelled, release, closed = (asyncio.Event() for _ in range(4))

    async def controlled_terminal(request):
        result = await original(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return replace(result, caller_cancelled=True)

    runner.run_terminal = controlled_terminal

    async def execute():
        try:
            async with PostgresUnitOfWork(workflow_session_factory) as work:
                await service.execute(command, work)
        finally:
            closed.set()

    task = asyncio.create_task(execute())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.wait_for(cancelled.wait(), 5)
    assert not task.done() and not closed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert closed.is_set() and runner.calls == ["unit"]
    async with workflow_session_factory() as session:
        receipt = await session.scalar(
            select(ArtifactLineage).where(
                ArtifactLineage.run_id == case.run_id,
                ArtifactLineage.producer_kind == "controller_named_check",
            )
        )
        assert receipt is not None


async def _expire_operation(factory, run_id):
    async with factory() as session, session.begin():
        intent = await session.scalar(
            select(OperationIntent).where(OperationIntent.run_id == run_id)
        )
        intent.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


async def test_unknown_check_outcome_is_not_blindly_reexecuted(tmp_path, workflow_session_factory):
    from forge.application.ports.commands import CommandRecoveryRequired

    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    original = runner.run_terminal

    async def uncertain(request):
        await original(request)
        raise RuntimeError("uncertain command effect")

    runner.run_terminal = uncertain
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    await _expire_operation(workflow_session_factory, case.run_id)
    runner.run_terminal = original
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert runner.calls == ["unit"]


@pytest.mark.parametrize("startup", [False, True])
async def test_receipt_committed_before_settlement_recovers_without_reexecution(
    tmp_path, workflow_session_factory, startup
):
    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:

        async def crash(*args, **kwargs):
            raise RuntimeError("crash before settlement")

        work.operations.complete = crash
        with pytest.raises(RuntimeError, match="crash before settlement"):
            await service.execute(command, work)
    assert runner.calls == ["unit"]
    await _expire_operation(workflow_session_factory, case.run_id)
    if startup:
        from dataclasses import replace

        from forge.application.services.recovery import RecoveryError, RecoveryService
        from forge.worker.recovery_adapters import local_recovery_adapters

        operations = PostgresOperationRepository(workflow_session_factory)
        adapters = local_recovery_adapters(
            workflow_session_factory, case.artifact_store, service._git_factory
        )
        pending = (await operations.list_unresolved())[0]
        with pytest.raises(RecoveryError, match="cannot invoke"):
            await adapters[pending.kind].invoke(pending)
        with pytest.raises(RecoveryError, match="identity differs"):
            await adapters[pending.kind].reconcile(replace(pending, request_digest="f" * 64))
        recovered = await RecoveryService(operations).reconcile_all(adapters)
        assert len(recovered) == 1
        assert runner.calls == ["unit"]
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        evidence = await service.execute(command, work)
    assert runner.calls == ["unit", "lint"]
    assert evidence.head_sha == "b" * 40
