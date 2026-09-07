"""PostgreSQL delivery coverage for the first Developer execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from forge.agents.prompt_loader import PromptLoader
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.worktrees import GitCandidateDiff, GitDiff, ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.development import DevelopmentService
from forge.domain.agent import AgentFinishStatus, AgentResult, DeveloperOutput
from forge.domain.run import RunState
from forge.observability.usage import UsageRecord
from forge.persistence.models import AgentExecution, Artifact, ArtifactLineage, Run, RunCommand
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_preparation import _PersistingProvisioner, _prepared_command
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)

HEAD = "b" * 40
DIFF = "diff --git a/README.md b/README.md\n+change\n"


class _Git:
    def inspect_worktree(self, identity, base_sha):
        return ManagedWorktree(identity=identity, path=self.path, base_sha=base_sha)

    def candidate_diff(self, worktree):
        return GitCandidateDiff(
            head_sha=HEAD,
            diff=GitDiff(text=DIFF, original_byte_count=len(DIFF), truncated=False),
            changed_paths=("README.md",),
        )

    def is_ancestor(self, worktree):
        return True


class _Reader:
    def read_instructions(self, target):
        return ()


class _Gateway:
    def __init__(self, factory):
        self.factory, self.requests, self.admitted = factory, [], False

    async def execute(self, request):
        self.requests.append(request)
        async with self.factory() as session:
            rows = (
                await session.scalars(
                    select(AgentExecution).where(AgentExecution.id == request.execution_id)
                )
            ).all()
            self.admitted = len(rows) == 1
        return AgentResult(
            execution_id=request.execution_id,
            role=request.role,
            finish_status=AgentFinishStatus.SUCCEEDED,
            provider=request.provider,
            model=request.model,
            instruction_digest=request.instruction_digest,
            output=DeveloperOutput(
                summary="done",
                changed_paths=("README.md",),
                tests_added_or_changed=(),
                named_checks_run=("unit",),
                local_commit_sha=HEAD,
                diff_digest=hashlib.sha256(DIFF.encode()).hexdigest(),
                unresolved_concerns=(),
                plan_deviations=(),
            ),
            usage=UsageRecord(
                provider=request.provider,
                model=request.model,
                prompt_version=request.instruction_version,
                input_tokens=3,
                output_tokens=2,
                pricing_version="fixture-v1",
                estimated_cost_minor=1,
                currency="USD",
            ),
            tool_call_count=0,
            duration_ms=0,
        )


async def test_identity_mismatch_intervenes_without_validation(tmp_path, workflow_session_factory):
    case, command, _commands, path = await _implement_command(tmp_path, workflow_session_factory)
    git = _Git()
    git.path = path

    class WrongIdentity(_Gateway):
        async def execute(self, request):
            result = await super().execute(request)
            return result.model_copy(update={"execution_id": request.run_id})

    gateway = WrongIdentity(workflow_session_factory)
    service = DevelopmentService(
        gateway,
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda policy: git,
        lambda policy, worktree: _Reader(),
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        validate = await session.scalar(
            select(RunCommand).where(RunCommand.idempotency_key == f"{case.run_id}:validate:1")
        )
    assert run is not None and run.state == RunState.AWAITING_HUMAN_INTERVENTION.value
    assert validate is None


async def _implement_command(tmp_path, factory):
    case, _approval_id, prepare, commands = await _prepared_command(tmp_path, factory)
    path = tmp_path / "worktree"
    provisioner = _PersistingProvisioner(factory, path)
    service = __import__(
        "forge.application.services.delivery_preparation", fromlist=["DeliveryPreparationService"]
    ).DeliveryPreparationService(ApprovedPlanLoader(case.artifact_store), provisioner)
    async with PostgresUnitOfWork(factory) as work:
        await service.execute(prepare, work)
    await commands.complete(prepare.id, worker_id="test-worker")
    command = await commands.claim_next(worker_id="developer", lease_seconds=60)
    assert command is not None and command.command_type == "implement"
    return case, command, commands, path


async def test_developer_admits_before_gateway_and_queues_validation(
    tmp_path, workflow_session_factory
):
    case, command, _commands, path = await _implement_command(tmp_path, workflow_session_factory)
    git = _Git()
    git.path = path
    gateway = _Gateway(workflow_session_factory)
    service = DevelopmentService(
        gateway,
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda policy: git,
        lambda policy, worktree: _Reader(),
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
    assert gateway.admitted is True
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        validate = await session.scalar(
            select(RunCommand).where(RunCommand.idempotency_key == f"{case.run_id}:validate:1")
        )
        execution = await session.scalar(
            select(AgentExecution).where(AgentExecution.id == gateway.requests[0].execution_id)
        )
    assert run is not None and run.state == RunState.VALIDATING.value
    assert validate is not None and validate.payload == {"semantic_attempt": 1}
    assert (
        execution is not None
        and execution.status == "SUCCEEDED"
        and execution.output_artifact_id is not None
    )


@pytest.mark.parametrize("target", (RunState.PAUSED, RunState.CANCELLED))
async def test_suspended_after_gateway_preserves_terminal_evidence_without_dispatch(
    tmp_path, workflow_session_factory, target
):
    case, command, _commands, path = await _implement_command(tmp_path, workflow_session_factory)
    git = _Git()
    git.path = path

    class SuspendedGateway(_Gateway):
        async def execute(self, request):
            result = await super().execute(request)
            async with PostgresUnitOfWork(self.factory) as work:
                run = await work.runs.get(case.run_id)
                if target is RunState.PAUSED:
                    await work.runs.pause(run.id, run.version, "test.paused", {})
                else:
                    await work.runs.transition(run.id, run.version, target, "test.cancelled", {})
                await work.commit()
            return result

    gateway = SuspendedGateway(workflow_session_factory)
    service = DevelopmentService(
        gateway,
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda policy: git,
        lambda policy, worktree: _Reader(),
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    async with workflow_session_factory() as session:
        assert (await session.get(Run, case.run_id)).state == target.value
        assert (
            await session.scalar(
                select(RunCommand).where(RunCommand.idempotency_key == f"{case.run_id}:validate:1")
            )
            is None
        )
        artifacts = (
            await session.execute(
                select(ArtifactLineage.producer_kind, Artifact.digest)
                .join(Artifact, Artifact.id == ArtifactLineage.artifact_id)
                .where(
                    ArtifactLineage.run_id == case.run_id,
                    ArtifactLineage.producer_id == gateway.requests[0].execution_id,
                )
            )
        ).all()
    produced = dict(artifacts)
    assert "developer_result" in produced
    assert "developer_late_usage" in produced
    usage = json.loads(await case.artifact_store.open_bytes(produced["developer_late_usage"]))
    assert usage["usage"][0]["input_tokens"] == 3
    assert usage["usage"][0]["output_tokens"] == 2


async def test_boolean_attempt_is_not_authorized_as_integer_attempt(
    tmp_path, workflow_session_factory
):
    case, command, _commands, path = await _implement_command(tmp_path, workflow_session_factory)
    git = _Git()
    git.path = path
    gateway = _Gateway(workflow_session_factory)
    service = DevelopmentService(
        gateway,
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda policy: git,
        lambda policy, worktree: _Reader(),
    )
    command = replace(command, payload={"semantic_attempt": True})
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert gateway.requests == []


async def test_unknown_provider_outcome_is_not_reinvoked(tmp_path, workflow_session_factory):
    case, command, _commands, path = await _implement_command(tmp_path, workflow_session_factory)
    git = _Git()
    git.path = path

    class UnknownGateway(_Gateway):
        async def execute(self, request):
            self.requests.append(request)
            raise RuntimeError("unknown provider outcome")

    gateway = UnknownGateway(workflow_session_factory)
    service = DevelopmentService(
        gateway,
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda policy: git,
        lambda policy, worktree: _Reader(),
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(RuntimeError):
            await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work)
    assert len(gateway.requests) == 1
