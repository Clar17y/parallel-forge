"""PostgreSQL-backed integration coverage for durable setup orchestration."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from forge.application.ports.runner import (
    CommandResult,
    CommandTerminalResult,
    RunCommandRequest,
)
from forge.application.ports.worktrees import (
    DatabaseBinding,
    EnvironmentFileEvidence,
    EnvironmentStagingInspection,
    ManagedWorktree,
)
from forge.domain.policy import (
    CommandSpec,
    DatabaseProvisioningPolicy,
    ProjectPolicy,
    RunnerMode,
    StepKind,
)
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import OperationIntent as OperationIntentRecord
from forge.persistence.models import Run
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.runner import command_spec_digest
from forge.tools.worktree import WorktreeProvisioner
from sqlalchemy import select, update

BASE_SHA = "a" * 40


class _Git:
    repository_path = Path.cwd()

    def __init__(self) -> None:
        self.create_calls = 0
        self.handle: ManagedWorktree | None = None

    def expected_worktree(
        self,
        identity: WorktreeIdentity,
        base_sha: str,
    ) -> ManagedWorktree:
        return ManagedWorktree(
            identity=identity,
            path=self.repository_path / ".worktrees" / identity.worktree_name,
            base_sha=base_sha,
        )

    def inspect_worktree(
        self,
        identity: WorktreeIdentity,
        base_sha: str,
    ) -> ManagedWorktree | None:
        del identity, base_sha
        return self.handle

    def create_worktree(
        self,
        identity: WorktreeIdentity,
        base_sha: str,
    ) -> ManagedWorktree:
        self.create_calls += 1
        self.handle = self.expected_worktree(identity, base_sha)
        return self.handle


class _DisabledDatabase:
    def validate_binding(
        self,
        identity: WorktreeIdentity,
        binding: DatabaseBinding,
    ) -> DatabaseBinding:
        del identity, binding
        raise AssertionError("disabled setup must not validate a database")

    async def verify_active(self, *args: Any, **kwargs: Any) -> UUID:
        raise AssertionError("disabled setup must not verify a database")

    async def rematerialize_active(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> DatabaseBinding:
        raise AssertionError("disabled setup must not rematerialize a database")

    async def provision(self, *args: Any, **kwargs: Any) -> DatabaseBinding:
        raise AssertionError("disabled setup must not provision a database")


class _Plan:
    evidence = (
        EnvironmentFileEvidence(
            path_digest="1" * 64,
            source_digest="2" * 64,
            output_digest="3" * 64,
            byte_count=41,
        ),
    )


class _Stager:
    def __init__(self) -> None:
        self.plan = _Plan()
        self.publish_calls = 0

    def build_plan(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int | None = None,
    ) -> _Plan:
        del worktree, policy, resource, policy_version
        return self.plan

    def publish(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        plan: _Plan,
    ) -> tuple[EnvironmentFileEvidence, ...]:
        del worktree, policy
        self.publish_calls += 1
        return plan.evidence

    def inspect(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        plan: _Plan,
    ) -> EnvironmentStagingInspection:
        del worktree, policy
        return EnvironmentStagingInspection(
            present=True,
            evidence=plan.evidence,
        )


class _Runner:
    def __init__(
        self,
        commands: dict[str, CommandSpec],
        policy_version: int,
    ) -> None:
        self.commands = commands
        self.policy_version = policy_version
        self.calls: Counter[str] = Counter()

    async def run_terminal(
        self,
        request: RunCommandRequest,
    ) -> CommandTerminalResult:
        self.calls[request.command_name] += 1
        await asyncio.sleep(0.02)
        command = self.commands[request.command_name]
        return CommandTerminalResult(
            result=CommandResult(
                command_name=command.name,
                kind=command.kind,
                command_digest=command_spec_digest(command),
                policy_version=self.policy_version,
                exit_code=0,
                timed_out=False,
                started_at=datetime.now(UTC),
                duration_ms=1,
                stdout_digest="4" * 64,
                stderr_digest="5" * 64,
                runner_mode=RunnerMode.TRUSTED_HOST,
                image_digest=None,
                network_enabled=True,
                stdout_original_byte_count=0,
                stderr_original_byte_count=0,
                stdout_truncated=False,
                stderr_truncated=False,
                unsandboxed=True,
            ),
            caller_cancelled=False,
        )

    async def run(self, request: RunCommandRequest) -> CommandResult:
        return (await self.run_terminal(request)).result


class _RunnerFactory:
    def __init__(self, runner: _Runner) -> None:
        self.runner = runner

    def create(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> _Runner:
        del worktree, policy
        return self.runner


async def _seed_preparing_run(
    session_factory: Any,
    run: RunSnapshot,
    branch: str,
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Run)
            .where(Run.id == run.id)
            .values(
                state=RunState.PREPARING_WORKTREE.value,
                branch_name=branch,
                base_ref="refs/heads/main",
                base_sha=BASE_SHA,
            )
        )


def _policy(run: RunSnapshot, git: _Git) -> ProjectPolicy:
    commands = (
        CommandSpec(
            kind=StepKind.BOOTSTRAP,
            name="integration-bootstrap",
            argv=("forge-test-command", "bootstrap"),
            timeout_seconds=30,
        ),
        CommandSpec(
            kind=StepKind.INSTALL,
            name="integration-install",
            argv=("forge-test-command", "install"),
            timeout_seconds=30,
            required=False,
        ),
    )
    return ProjectPolicy(
        id=run.project_id,
        version=run.policy_version,
        repository_path=str(git.repository_path),
        github_repository=f"Clar17y/forge-{run.project_id}",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        database=DatabaseProvisioningPolicy(enabled=False),
        commands=commands,
    )


@pytest.mark.integration
async def test_postgres_concurrent_setup_prepares_share_effects_and_converge(
    session_factory: Any,
    operation_repository: Any,
    persisted_run: RunSnapshot,
) -> None:
    branch = "feature/e2c-postgres-concurrent"
    await _seed_preparing_run(session_factory, persisted_run, branch)
    git = _Git()
    stager = _Stager()
    policy = _policy(persisted_run, git)
    runner = _Runner(
        {command.name: command for command in policy.commands},
        policy.version,
    )
    provisioner = WorktreeProvisioner(
        lambda: PostgresUnitOfWork(session_factory),
        operations=operation_repository,
        git=git,
        database=_DisabledDatabase(),
        environment_stager=stager,
        runner_factory=_RunnerFactory(runner),
    )

    first, second = await asyncio.gather(
        provisioner.prepare(persisted_run.id, policy),
        provisioner.prepare(persisted_run.id, policy),
    )

    assert first == second
    assert git.create_calls == 1
    assert stager.publish_calls == 1
    assert runner.calls == {
        "integration-bootstrap": 1,
        "integration-install": 1,
    }

    async with PostgresUnitOfWork(session_factory) as work:
        stored_run = await work.runs.get(persisted_run.id)
        events = await work.events.list_after(persisted_run.id, 0)
    async with session_factory() as session:
        intents = list(
            (
                await session.execute(
                    select(OperationIntentRecord)
                    .where(OperationIntentRecord.run_id == persisted_run.id)
                    .order_by(
                        OperationIntentRecord.created_at,
                        OperationIntentRecord.id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert stored_run.state is RunState.PREPARING_WORKTREE
    assert [event.event_type for event in events] == [
        "operation.intent_created",
        "resource.worktree_preparing",
        "resource.worktree_created",
        "operation.intent_created",
        "resource.environment_staged",
        "operation.intent_created",
        "resource.setup_step_completed",
        "operation.intent_created",
        "resource.setup_step_completed",
        "resource.worktree_prepared",
    ]
    assert [intent.operation_kind for intent in intents] == [
        "worktree.create",
        "worktree.environment.stage",
        "worktree.setup.command",
        "worktree.setup.command",
    ]
    assert all(intent.status == "SUCCEEDED" for intent in intents)
    assert sum(event.event_type == "resource.worktree_prepared" for event in events) == 1
