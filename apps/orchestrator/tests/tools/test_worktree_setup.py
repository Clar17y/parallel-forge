"""Durable persisted-run environment and setup orchestration contracts."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from forge.application.ports.runner import CommandResult, CommandTerminalResult, RunCommandRequest
from forge.application.ports.worktrees import (
    DatabaseBinding,
    EnvironmentFileEvidence,
    EnvironmentStagingInspection,
    ManagedWorktree,
)
from forge.application.services.state_engine import StateEngine
from forge.domain.event import RunEvent
from forge.domain.operation import OperationIntent, OperationOutcome, OperationStatus
from forge.domain.policy import (
    CommandSpec,
    DatabaseProvisioningPolicy,
    ProjectPolicy,
    RunnerMode,
    StepKind,
)
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.tools.runner import command_spec_digest

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
BASE_SHA = "a" * 40
RUNNER_ERROR_SENTINEL = "runner-os-error-path-branch-environment-sentinel"
BOUNDARY_ERROR_SENTINEL = "setup-boundary-error-path-branch-sentinel"
PERSISTENCE_ERROR_SENTINEL = (
    "postgresql://forge:hunter2@10.0.0.9/forge C:\\repo\\.worktrees\\sentinel"
)


@dataclass
class _Events:
    values: list[RunEvent] = field(default_factory=list)
    fail_list_on_call: int | None = None
    list_calls: int = 0

    async def append(self, event: RunEvent) -> RunEvent:
        self.values.append(event)
        return event

    async def list_after(self, run_id: UUID, sequence: int) -> list[RunEvent]:
        del run_id, sequence
        self.list_calls += 1
        if self.list_calls == self.fail_list_on_call:
            raise RuntimeError(PERSISTENCE_ERROR_SENTINEL)
        return list(self.values)


@dataclass
class _Runs:
    current: RunSnapshot
    events: _Events
    pause_event_type: str | None = None
    pause_entered: asyncio.Event = field(default_factory=asyncio.Event)
    pause_release: asyncio.Event = field(default_factory=asyncio.Event)
    fail_event_type: str | None = None

    async def get(self, run_id: UUID) -> RunSnapshot:
        assert run_id == self.current.id
        return self.current

    async def get_for_update(self, run_id: UUID) -> RunSnapshot:
        return await self.get(run_id)

    async def update_resource(
        self, run_id: UUID, expected_version: int, **values: Any
    ) -> RunSnapshot:
        assert run_id == self.current.id
        assert expected_version == self.current.version
        if values["event_type"] == self.fail_event_type:
            raise RuntimeError(PERSISTENCE_ERROR_SENTINEL)
        if values["event_type"] == self.pause_event_type:
            self.pause_event_type = None
            self.pause_entered.set()
            await self.pause_release.wait()
        self.current = self.current.with_resource(
            worktree_path=values["worktree_path"],
            database_state=values["database_state"],
            database_name=values["database_name"],
            database_role=values["database_role"],
            secret_id=values["secret_id"],
        )
        await self.events.append(
            RunEvent(
                run_id=run_id,
                run_version=self.current.version,
                event_type=values["event_type"],
                payload=values["event_payload"],
            )
        )
        return self.current


@dataclass
class _Uow:
    runs: _Runs
    events: _Events

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _UowFactory:
    def __init__(self, run: RunSnapshot) -> None:
        self.events = _Events()
        self.runs = _Runs(run, self.events)

    def __call__(self) -> _Uow:
        return _Uow(self.runs, self.events)


class _Operations:
    def __init__(self) -> None:
        self.intents: dict[str, OperationIntent] = {}

    async def begin(self, **values: Any) -> OperationIntent:
        existing = self.intents.get(values["idempotency_key"])
        if existing is not None:
            return replace(existing, is_new=False)
        now = datetime.now(UTC)
        intent = OperationIntent(
            id=uuid4(),
            run_id=values["run_id"],
            kind=values["operation_type"],
            idempotency_key=values["idempotency_key"],
            request_digest=values["request_digest"],
            request_payload=values["request_payload"],
            status=OperationStatus.PENDING,
            created_at=now,
            updated_at=now,
            is_new=True,
        )
        self.intents[intent.idempotency_key] = intent
        return intent

    async def complete(
        self, intent_id: UUID, outcome: OperationOutcome, **_: Any
    ) -> OperationIntent:
        for key, intent in self.intents.items():
            if intent.id == intent_id:
                completed = replace(
                    intent,
                    status=OperationStatus.SUCCEEDED,
                    remote_resource_id=outcome.remote_resource_id,
                    outcome=outcome.payload,
                    outcome_schema_version=outcome.outcome_schema_version,
                    completed_at=intent.created_at,
                    updated_at=intent.created_at,
                    is_new=False,
                )
                self.intents[key] = completed
                return completed
        raise AssertionError("unknown intent")

    async def get_by_idempotency_key(self, key: str) -> OperationIntent | None:
        return self.intents.get(key)

    async def get(self, intent_id: UUID) -> OperationIntent:
        for intent in self.intents.values():
            if intent.id == intent_id:
                return intent
        raise AssertionError("unknown intent")

    async def claim_for_recovery(self, intent_id: UUID, **_: Any) -> Any:
        from forge.domain.operation import OperationExecutionClaim

        return OperationExecutionClaim(intent=await self.get(intent_id), acquired=True)

    async def renew_execution(self, intent_id: UUID, **_: Any) -> OperationIntent:
        return await self.get(intent_id)

    async def fail(self, intent_id: UUID, **_: Any) -> OperationIntent:
        return await self.get(intent_id)

    async def list_unresolved(self) -> tuple[OperationIntent, ...]:
        return tuple(
            intent
            for intent in self.intents.values()
            if intent.status is not OperationStatus.SUCCEEDED
        )


class _Executor:
    def __init__(self, operations: _Operations) -> None:
        self.operations = operations

    async def execute(self, request: Any, adapter: Any) -> OperationOutcome:
        intent = await self.operations.begin(
            run_id=request.run_id,
            operation_type=request.kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
        )
        if intent.status is OperationStatus.SUCCEEDED:
            return intent.to_outcome()
        outcome = await adapter.invoke(intent) if intent.is_new else await adapter.reconcile(intent)
        if outcome.status is OperationStatus.SUCCEEDED:
            await self.operations.complete(intent.id, outcome)
        return outcome


class _Git:
    repository_path = Path.cwd()

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.handle: ManagedWorktree | None = None

    def expected_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        return ManagedWorktree(
            identity=identity,
            path=self.repository_path / ".worktrees" / identity.worktree_name,
            base_sha=base_sha,
        )

    def inspect_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree | None:
        del identity, base_sha
        self.log.append("git.inspect")
        return self.handle

    def create_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        self.log.append("git.create")
        self.handle = self.expected_worktree(identity, base_sha)
        return self.handle


class _Database:
    def validate_binding(
        self, identity: WorktreeIdentity, binding: DatabaseBinding
    ) -> DatabaseBinding:
        del identity
        return binding

    async def verify_active(self, *args: Any, **kwargs: Any) -> UUID:
        raise AssertionError("disabled setup must not verify a database")

    async def provision(self, *args: Any, **kwargs: Any) -> DatabaseBinding:
        raise AssertionError("disabled setup must not provision a database")


class _ActiveDatabase:
    def __init__(self) -> None:
        self.intent_id = uuid4()
        self.rematerialize_calls = 0
        self.environment_value = "postgresql://scoped-environment-sentinel"

    def validate_binding(
        self,
        identity: WorktreeIdentity,
        binding: DatabaseBinding,
    ) -> DatabaseBinding:
        assert binding.database_name == identity.database_name
        assert binding.database_role == identity.database_role
        if binding.state is ResourceState.ACTIVE or binding.secret_id is not None:
            assert binding.secret_id == (
                f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}"
            )
        return binding

    async def provision(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        del policy, policy_version
        return DatabaseBinding(
            state=ResourceState.ACTIVE,
            database_name=identity.database_name,
            database_role=identity.database_role,
            secret_id=f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}",
        )

    async def verify_active(self, *args: Any, **kwargs: Any) -> UUID:
        del args, kwargs
        return self.intent_id

    async def rematerialize_active(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        binding: DatabaseBinding,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        del policy_version
        self.validate_binding(identity, binding)
        self.rematerialize_calls += 1
        return replace(
            binding,
            environment={policy.injected_environment_key: self.environment_value},
        )


class _Plan:
    evidence = (
        EnvironmentFileEvidence(
            path_digest="1" * 64,
            source_digest="2" * 64,
            output_digest="3" * 64,
            byte_count=41,
        ),
    )
    file_count = 1


class _Stager:
    def __init__(
        self,
        log: list[str],
        *,
        cancel_on_publish: bool = False,
        call_threads: list[tuple[str, int]] | None = None,
        fail_inspect_on: int | None = None,
        missing_inspect_on: int | None = None,
    ) -> None:
        self.log = log
        self.plan = _Plan()
        self.cancel_on_publish = cancel_on_publish
        self.call_threads = call_threads
        self.fail_inspect_on = fail_inspect_on
        self.missing_inspect_on = missing_inspect_on
        self.inspect_calls = 0

    def build_plan(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int | None = None,
    ) -> _Plan:
        del worktree, policy, resource, policy_version
        self.log.append("stage.plan")
        if self.call_threads is not None:
            self.call_threads.append(("plan", threading.get_ident()))
        return self.plan

    def publish(
        self, worktree: ManagedWorktree, policy: ProjectPolicy, plan: _Plan
    ) -> tuple[EnvironmentFileEvidence, ...]:
        del worktree, policy
        self.log.append("stage.publish")
        if self.call_threads is not None:
            self.call_threads.append(("publish", threading.get_ident()))
        if self.cancel_on_publish:
            raise asyncio.CancelledError()
        return plan.evidence

    def inspect(
        self, worktree: ManagedWorktree, policy: ProjectPolicy, plan: _Plan
    ) -> EnvironmentStagingInspection:
        del worktree, policy
        self.log.append("stage.inspect")
        self.inspect_calls += 1
        if self.inspect_calls == self.fail_inspect_on:
            raise OSError(BOUNDARY_ERROR_SENTINEL)
        if self.inspect_calls == self.missing_inspect_on:
            return EnvironmentStagingInspection(present=False, evidence=())
        if self.call_threads is not None:
            self.call_threads.append(("inspect", threading.get_ident()))
        return EnvironmentStagingInspection(present=True, evidence=plan.evidence)


def _result(
    spec: CommandSpec,
    *,
    caller_cancelled: bool = False,
    exit_code: int = 0,
    timed_out: bool = False,
    command_name: str | None = None,
) -> CommandTerminalResult:
    return CommandTerminalResult(
        result=CommandResult(
            command_name=command_name or spec.name,
            kind=spec.kind,
            command_digest=command_spec_digest(spec),
            policy_version=7,
            exit_code=exit_code,
            timed_out=timed_out,
            started_at=datetime.now(UTC),
            duration_ms=1,
            stdout_digest="b" * 64,
            stderr_digest="c" * 64,
            runner_mode=RunnerMode.TRUSTED_HOST,
            image_digest=None,
            network_enabled=spec.network_enabled,
            stdout_original_byte_count=0,
            stderr_original_byte_count=0,
            stdout_truncated=False,
            stderr_truncated=False,
            unsandboxed=True,
        ),
        caller_cancelled=caller_cancelled,
    )


class _Runner:
    def __init__(
        self,
        log: list[str],
        specs: dict[str, CommandSpec],
        *,
        cancel_on: str | None = None,
        fail_on: str | None = None,
        timeout_on: str | None = None,
        tamper_on: str | None = None,
        raise_on: str | None = None,
        request_sink: list[RunCommandRequest] | None = None,
    ) -> None:
        self.log = log
        self.specs = specs
        self.cancel_on = cancel_on
        self.fail_on = fail_on
        self.timeout_on = timeout_on
        self.tamper_on = tamper_on
        self.raise_on = raise_on
        self.request_sink = request_sink

    async def run_terminal(self, request: RunCommandRequest) -> CommandTerminalResult:
        self.log.append(f"run:{request.command_name}")
        if self.request_sink is not None:
            self.request_sink.append(request)
        if request.command_name == self.raise_on:
            raise OSError(RUNNER_ERROR_SENTINEL)
        return _result(
            self.specs[request.command_name],
            caller_cancelled=request.command_name == self.cancel_on,
            exit_code=2 if request.command_name == self.fail_on else 0,
            timed_out=request.command_name == self.timeout_on,
            command_name=(
                "forged-command" if request.command_name == self.tamper_on else request.command_name
            ),
        )

    async def run(self, request: RunCommandRequest) -> Any:
        return (await self.run_terminal(request)).result


class _Factory:
    def __init__(
        self,
        log: list[str],
        specs: dict[str, CommandSpec],
        *,
        cancel_on: str | None = None,
        fail_on: str | None = None,
        timeout_on: str | None = None,
        tamper_on: str | None = None,
        raise_on: str | None = None,
        request_sink: list[RunCommandRequest] | None = None,
        fail_create: bool = False,
    ) -> None:
        self.log = log
        self.specs = specs
        self.cancel_on = cancel_on
        self.fail_on = fail_on
        self.timeout_on = timeout_on
        self.tamper_on = tamper_on
        self.raise_on = raise_on
        self.request_sink = request_sink
        self.fail_create = fail_create

    def create(self, worktree: ManagedWorktree, policy: ProjectPolicy) -> _Runner:
        del worktree, policy
        self.log.append("runner.create")
        if self.fail_create:
            raise OSError(BOUNDARY_ERROR_SENTINEL)
        return _Runner(
            self.log,
            self.specs,
            cancel_on=self.cancel_on,
            fail_on=self.fail_on,
            timeout_on=self.timeout_on,
            tamper_on=self.tamper_on,
            raise_on=self.raise_on,
            request_sink=self.request_sink,
        )


def _policy(repository_path: str, *, database_enabled: bool = False) -> ProjectPolicy:
    commands = tuple(
        CommandSpec(
            kind=kind,
            name=name,
            argv=("forge-test-command", "--no-op"),
            timeout_seconds=10,
            required=required,
            environment_keys=(
                ("DATABASE_URL",) if database_enabled and name == "install-first" else ()
            ),
        )
        for kind, name, required in (
            (StepKind.TEST, "test-first", True),
            (StepKind.BOOTSTRAP, "bootstrap-first", False),
            (StepKind.INSTALL, "install-first", True),
            (StepKind.BUILD, "build-never", True),
            (StepKind.BOOTSTRAP, "bootstrap-second", True),
            (StepKind.CUSTOM, "custom-never", True),
            (StepKind.MIGRATION, "migration-first", True),
            (StepKind.SEED, "seed-first", False),
        )
    )
    return ProjectPolicy(
        id=PROJECT_ID,
        version=7,
        repository_path=repository_path,
        github_repository="forge/example",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        database=DatabaseProvisioningPolicy(
            enabled=database_enabled,
            admin_url_secret_reference=("secret://admin/postgres" if database_enabled else None),
        ),
        commands=commands,
    )


def _case(
    *,
    cancel_on: str | None = None,
    fail_on: str | None = None,
    timeout_on: str | None = None,
    tamper_on: str | None = None,
    raise_on: str | None = None,
    database_enabled: bool = False,
    database: Any | None = None,
    request_sink: list[RunCommandRequest] | None = None,
    cancel_stage: bool = False,
    staging_threads: list[tuple[str, int]] | None = None,
    fail_final_inspect: bool = False,
    missing_final_inspect: bool = False,
    fail_factory: bool = False,
) -> tuple[list[str], _UowFactory, _Operations, ProjectPolicy, Any]:
    log: list[str] = []
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=uuid4(),
        state=RunState.PREPARING_WORKTREE,
        policy_version=7,
        base_ref="refs/heads/main",
        base_sha=BASE_SHA,
        branch_name="feature/setup-order",
    )
    policy = _policy(str(Path.cwd()), database_enabled=database_enabled)
    specs = {command.name: command for command in policy.commands}
    from forge.tools.worktree import WorktreeProvisioner

    git = _Git(log)
    operations = _Operations()
    uow = _UowFactory(run)
    provisioner = WorktreeProvisioner(
        uow,
        operations=operations,
        git=git,
        database=database or _Database(),
        operation_executor=_Executor(operations),
        environment_stager=_Stager(
            log,
            cancel_on_publish=cancel_stage,
            call_threads=staging_threads,
            fail_inspect_on=2 if fail_final_inspect else None,
            missing_inspect_on=2 if missing_final_inspect else None,
        ),
        runner_factory=_Factory(
            log,
            specs,
            cancel_on=cancel_on,
            fail_on=fail_on,
            timeout_on=timeout_on,
            tamper_on=tamper_on,
            raise_on=raise_on,
            request_sink=request_sink,
            fail_create=fail_factory,
        ),
    )
    return log, uow, operations, policy, provisioner


@pytest.mark.asyncio
async def test_prepare_runs_setup_commands_in_fixed_kind_and_policy_order() -> None:
    log, _uow, _operations, policy, provisioner = _case()

    await provisioner.prepare(RUN_ID, policy)

    assert log[log.index("stage.publish") + 1 :].count("runner.create") == 1
    assert [entry for entry in log if entry.startswith("run:")] == [
        "run:bootstrap-first",
        "run:bootstrap-second",
        "run:install-first",
        "run:migration-first",
        "run:seed-first",
    ]
    assert "run:test-first" not in log
    assert "runner.create" in log


@pytest.mark.asyncio
async def test_prepare_persists_one_checkpoint_for_each_setup_effect_before_final_evidence() -> (
    None
):
    _log, uow, operations, policy, provisioner = _case()

    await provisioner.prepare(RUN_ID, policy)

    assert [intent.kind for intent in operations.intents.values()] == [
        "worktree.create",
        "worktree.environment.stage",
        "worktree.setup.command",
        "worktree.setup.command",
        "worktree.setup.command",
        "worktree.setup.command",
        "worktree.setup.command",
    ]
    assert [event.event_type for event in uow.events.values] == [
        "resource.worktree_preparing",
        "resource.worktree_created",
        "resource.environment_staged",
        "resource.setup_step_completed",
        "resource.setup_step_completed",
        "resource.setup_step_completed",
        "resource.setup_step_completed",
        "resource.setup_step_completed",
        "resource.worktree_prepared",
    ]
    assert uow.runs.current.state is RunState.PREPARING_WORKTREE
    environment = next(
        intent
        for intent in operations.intents.values()
        if intent.kind == "worktree.environment.stage"
    )
    assert environment.request_payload["files"] == (
        {
            "path_digest": "1" * 64,
            "source_digest": "2" * 64,
            "output_digest": "3" * 64,
            "byte_count": 41,
        },
    )
    assert environment.request_payload["file_count"] == 1


@pytest.mark.asyncio
async def test_repeated_prepare_adopts_exact_setup_evidence_without_duplicate_effects() -> None:
    log, uow, _operations, policy, provisioner = _case()

    await provisioner.prepare(RUN_ID, policy)
    await provisioner.prepare(RUN_ID, policy)

    assert log.count("stage.publish") == 1
    assert len([entry for entry in log if entry.startswith("run:")]) == 5
    event_types = [event.event_type for event in uow.events.values]
    assert event_types.count("resource.environment_staged") == 1
    assert event_types.count("resource.setup_step_completed") == 5
    assert event_types.count("resource.worktree_prepared") == 1


@pytest.mark.asyncio
async def test_cancelled_terminal_step_is_checkpointed_before_cancellation_propagates() -> None:
    log, uow, operations, policy, provisioner = _case(cancel_on="install-first")

    with pytest.raises(asyncio.CancelledError):
        await provisioner.prepare(RUN_ID, policy)

    assert [entry for entry in log if entry.startswith("run:")] == [
        "run:bootstrap-first",
        "run:bootstrap-second",
        "run:install-first",
    ]
    assert [event.event_type for event in uow.events.values].count(
        "resource.setup_step_completed"
    ) == 3
    assert all(event.event_type != "resource.worktree_prepared" for event in uow.events.values)
    install = next(
        intent
        for intent in operations.intents.values()
        if intent.request_payload.get("command_name") == "install-first"
    )
    assert install.status is OperationStatus.SUCCEEDED
    assert install.outcome is not None
    assert install.outcome["caller_cancelled"] is True


@pytest.mark.asyncio
async def test_nonzero_terminal_step_is_checkpointed_and_stops_later_steps() -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, operations, policy, provisioner = _case(fail_on="install-first")

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert [entry for entry in log if entry.startswith("run:")] == [
        "run:bootstrap-first",
        "run:bootstrap-second",
        "run:install-first",
    ]
    assert [event.event_type for event in uow.events.values].count(
        "resource.setup_step_completed"
    ) == 3
    assert all(event.event_type != "resource.worktree_prepared" for event in uow.events.values)
    install = next(
        intent
        for intent in operations.intents.values()
        if intent.request_payload.get("command_name") == "install-first"
    )
    assert install.status is OperationStatus.SUCCEEDED
    assert install.outcome is not None
    assert install.outcome["exit_code"] == 2


@pytest.mark.asyncio
async def test_forged_terminal_result_is_rejected_before_checkpoint_or_later_steps() -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, _operations, policy, provisioner = _case(tamper_on="bootstrap-first")

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert [entry for entry in log if entry.startswith("run:")] == ["run:bootstrap-first"]
    assert all(event.event_type != "resource.setup_step_completed" for event in uow.events.values)
    assert all(event.event_type != "resource.worktree_prepared" for event in uow.events.values)


@pytest.mark.asyncio
async def test_unresolved_command_adopts_one_exact_checkpoint_without_rerun() -> None:
    log, uow, operations, policy, provisioner = _case()

    await provisioner.prepare(RUN_ID, policy)
    run_count = len([entry for entry in log if entry.startswith("run:")])
    install_key, install = next(
        (key, intent)
        for key, intent in operations.intents.items()
        if intent.request_payload.get("command_name") == "install-first"
    )
    operations.intents[install_key] = replace(
        install,
        status=OperationStatus.PENDING,
        remote_resource_id=None,
        outcome=None,
        outcome_schema_version=None,
        completed_at=None,
        is_new=False,
    )
    uow.events.values = [
        event for event in uow.events.values if event.event_type != "resource.worktree_prepared"
    ]

    await provisioner.prepare(RUN_ID, policy)

    assert len([entry for entry in log if entry.startswith("run:")]) == run_count
    assert operations.intents[install_key].status is OperationStatus.SUCCEEDED
    assert [event.event_type for event in uow.events.values].count(
        "resource.setup_step_completed"
    ) == 5
    assert [event.event_type for event in uow.events.values].count(
        "resource.worktree_prepared"
    ) == 1


@pytest.mark.asyncio
async def test_unresolved_environment_stage_adopts_exact_publication_without_republish() -> None:
    log, uow, operations, policy, provisioner = _case()

    await provisioner.prepare(RUN_ID, policy)
    environment_key, environment = next(
        (key, intent)
        for key, intent in operations.intents.items()
        if intent.kind == "worktree.environment.stage"
    )
    operations.intents[environment_key] = replace(
        environment,
        status=OperationStatus.PENDING,
        remote_resource_id=None,
        outcome=None,
        outcome_schema_version=None,
        completed_at=None,
        is_new=False,
    )
    uow.events.values = [
        event for event in uow.events.values if event.event_type != "resource.worktree_prepared"
    ]

    await provisioner.prepare(RUN_ID, policy)

    assert log.count("stage.publish") == 1
    assert operations.intents[environment_key].status is OperationStatus.SUCCEEDED
    assert [event.event_type for event in uow.events.values].count(
        "resource.environment_staged"
    ) == 1
    assert [event.event_type for event in uow.events.values].count(
        "resource.worktree_prepared"
    ) == 1


@pytest.mark.asyncio
async def test_succeeded_command_without_checkpoint_fails_closed_without_rerun() -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, operations, policy, provisioner = _case()

    await provisioner.prepare(RUN_ID, policy)
    run_count = len([entry for entry in log if entry.startswith("run:")])
    install = next(
        intent
        for intent in operations.intents.values()
        if intent.request_payload.get("command_name") == "install-first"
    )
    uow.events.values = [
        event
        for event in uow.events.values
        if event.event_type != "resource.worktree_prepared"
        and not (
            event.event_type == "resource.setup_step_completed"
            and event.payload.get("operation_intent_id") == str(install.id)
        )
    ]

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert len([entry for entry in log if entry.startswith("run:")]) == run_count
    assert all(event.event_type != "resource.worktree_prepared" for event in uow.events.values)


@pytest.mark.asyncio
async def test_active_database_environment_is_rematerialized_and_only_opted_in() -> None:
    database = _ActiveDatabase()
    requests: list[RunCommandRequest] = []
    _log, uow, operations, policy, provisioner = _case(
        database_enabled=True,
        database=database,
        request_sink=requests,
    )

    await provisioner.prepare(RUN_ID, policy)

    assert database.rematerialize_calls == 1
    assert {request.command_name: dict(request.environment) for request in requests} == {
        "bootstrap-first": {},
        "bootstrap-second": {},
        "install-first": {"DATABASE_URL": database.environment_value},
        "migration-first": {},
        "seed-first": {},
    }
    durable = (tuple(uow.events.values), tuple(operations.intents.values()))
    assert database.environment_value not in repr(durable)


@pytest.mark.asyncio
async def test_cancelled_environment_publication_is_checkpointed_before_propagation() -> None:
    log, uow, operations, policy, provisioner = _case(cancel_stage=True)

    with pytest.raises(asyncio.CancelledError):
        await provisioner.prepare(RUN_ID, policy)

    assert log.count("stage.publish") == 1
    assert "runner.create" not in log
    assert [event.event_type for event in uow.events.values].count(
        "resource.environment_staged"
    ) == 1
    environment = next(
        intent
        for intent in operations.intents.values()
        if intent.kind == "worktree.environment.stage"
    )
    assert environment.status is OperationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_repeated_checkpoint_cancellation_persists_once_and_retry_resumes() -> None:
    log, uow, operations, policy, provisioner = _case()
    uow.runs.pause_event_type = "resource.setup_step_completed"

    cancelled_prepare = asyncio.create_task(provisioner.prepare(RUN_ID, policy))
    await uow.runs.pause_entered.wait()
    cancelled_prepare.cancel()
    cancelled_prepare.cancel()
    uow.runs.pause_release.set()

    with pytest.raises(asyncio.CancelledError):
        await cancelled_prepare

    setup_events = [
        event for event in uow.events.values if event.event_type == "resource.setup_step_completed"
    ]
    assert len(setup_events) == 1
    first_command = next(
        intent
        for intent in operations.intents.values()
        if intent.request_payload.get("command_name") == "bootstrap-first"
    )
    assert first_command.status is OperationStatus.SUCCEEDED
    assert [entry for entry in log if entry.startswith("run:")] == ["run:bootstrap-first"]

    await provisioner.prepare(RUN_ID, policy)

    assert [entry for entry in log if entry.startswith("run:")] == [
        "run:bootstrap-first",
        "run:bootstrap-second",
        "run:install-first",
        "run:migration-first",
        "run:seed-first",
    ]
    assert [event.event_type for event in uow.events.values].count(
        "resource.worktree_prepared"
    ) == 1


@pytest.mark.asyncio
async def test_staging_filesystem_calls_run_off_the_event_loop_thread() -> None:
    event_loop_thread = threading.get_ident()
    staging_threads: list[tuple[str, int]] = []
    _log, _uow, _operations, policy, provisioner = _case(staging_threads=staging_threads)

    await provisioner.prepare(RUN_ID, policy)

    assert [name for name, _thread in staging_threads] == [
        "plan",
        "publish",
        "inspect",
        "inspect",
    ]
    assert all(thread != event_loop_thread for _name, thread in staging_threads)


@pytest.mark.asyncio
async def test_runner_failure_drops_raw_exception_chain_and_stops_setup() -> None:
    from forge.tools.worktree import (
        WorktreeReconciliationRequired,
    )

    log, uow, operations, policy, provisioner = _case(raise_on="bootstrap-first")

    with pytest.raises(WorktreeReconciliationRequired) as captured:
        await provisioner.prepare(RUN_ID, policy)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert RUNNER_ERROR_SENTINEL not in repr(captured.value)
    assert [entry for entry in log if entry.startswith("run:")] == ["run:bootstrap-first"]
    assert all(event.event_type != "resource.setup_step_completed" for event in uow.events.values)
    diagnostic_state = (log, tuple(uow.events.values), tuple(operations.intents.values()))
    assert RUNNER_ERROR_SENTINEL not in repr(diagnostic_state)


@pytest.mark.asyncio
async def test_reordered_setup_checkpoints_fail_closed_without_rerun() -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, _operations, policy, provisioner = _case()
    await provisioner.prepare(RUN_ID, policy)
    run_count = len([entry for entry in log if entry.startswith("run:")])
    setup_indexes = [
        index
        for index, event in enumerate(uow.events.values)
        if event.event_type == "resource.setup_step_completed"
    ]
    first, second = setup_indexes[:2]
    uow.events.values[first], uow.events.values[second] = (
        uow.events.values[second],
        uow.events.values[first],
    )

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert len([entry for entry in log if entry.startswith("run:")]) == run_count


@pytest.mark.asyncio
async def test_timed_out_terminal_step_is_checkpointed_and_stops_later_steps() -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, operations, policy, provisioner = _case(timeout_on="install-first")

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert [entry for entry in log if entry.startswith("run:")] == [
        "run:bootstrap-first",
        "run:bootstrap-second",
        "run:install-first",
    ]
    install = next(
        intent
        for intent in operations.intents.values()
        if intent.request_payload.get("command_name") == "install-first"
    )
    assert install.status is OperationStatus.SUCCEEDED
    assert install.outcome is not None
    assert install.outcome["timed_out"] is True
    assert [event.event_type for event in uow.events.values].count(
        "resource.setup_step_completed"
    ) == 3
    assert all(event.event_type != "resource.worktree_prepared" for event in uow.events.values)


@pytest.mark.parametrize(
    "case_kwargs",
    [
        {"fail_factory": True},
        {"fail_final_inspect": True},
    ],
    ids=["runner-factory", "final-stage-inspection"],
)
@pytest.mark.asyncio
async def test_setup_boundaries_raise_stable_context_free_errors(
    case_kwargs: dict[str, bool],
) -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    _log, uow, _operations, policy, provisioner = _case(**case_kwargs)

    with pytest.raises(WorktreeReconciliationRequired) as captured:
        await provisioner.prepare(RUN_ID, policy)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert BOUNDARY_ERROR_SENTINEL not in repr(captured.value)
    assert all(event.event_type != "resource.worktree_prepared" for event in uow.events.values)


@pytest.mark.asyncio
async def test_stale_prepared_event_version_fails_closed_without_rerun() -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, _operations, policy, provisioner = _case()
    await provisioner.prepare(RUN_ID, policy)
    run_count = len([entry for entry in log if entry.startswith("run:")])
    prepared_index = next(
        index
        for index, event in enumerate(uow.events.values)
        if event.event_type == "resource.worktree_prepared"
    )
    prepared = uow.events.values[prepared_index]
    uow.events.values[prepared_index] = replace(
        prepared,
        run_version=prepared.run_version - 1,
    )

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert len([entry for entry in log if entry.startswith("run:")]) == run_count


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("ordinal", 99),
        ("kind", "seed"),
        ("command_digest", "f" * 64),
        ("policy_version", 999),
        ("evidence_digest", "e" * 64),
    ],
)
@pytest.mark.asyncio
async def test_tampered_setup_checkpoint_fails_closed_without_rerun(
    field: str,
    forged_value: object,
) -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, _operations, policy, provisioner = _case()
    await provisioner.prepare(RUN_ID, policy)
    run_count = len([entry for entry in log if entry.startswith("run:")])
    event_index = next(
        index
        for index, event in enumerate(uow.events.values)
        if event.event_type == "resource.setup_step_completed"
    )
    event = uow.events.values[event_index]
    uow.events.values[event_index] = replace(
        event,
        payload={**event.payload, field: forged_value},
    )

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert len([entry for entry in log if entry.startswith("run:")]) == run_count


@pytest.mark.asyncio
async def test_final_environment_drift_prevents_prepared_checkpoint() -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, _operations, policy, provisioner = _case(
        missing_final_inspect=True,
    )

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert len([entry for entry in log if entry.startswith("run:")]) == 5
    assert all(event.event_type != "resource.worktree_prepared" for event in uow.events.values)


@pytest.mark.asyncio
async def test_duplicate_setup_checkpoint_fails_closed_without_rerun() -> None:
    from forge.tools.worktree import WorktreeReconciliationRequired

    log, uow, _operations, policy, provisioner = _case()
    await provisioner.prepare(RUN_ID, policy)
    run_count = len([entry for entry in log if entry.startswith("run:")])
    event_index = next(
        index
        for index, event in enumerate(uow.events.values)
        if event.event_type == "resource.setup_step_completed"
    )
    uow.events.values.insert(event_index + 1, uow.events.values[event_index])

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert len([entry for entry in log if entry.startswith("run:")]) == run_count


@pytest.mark.asyncio
async def test_setup_diagnostics_exclude_scoped_values_branch_and_path() -> None:
    database = _ActiveDatabase()
    requests: list[RunCommandRequest] = []
    log, uow, operations, policy, provisioner = _case(
        database_enabled=True,
        database=database,
        request_sink=requests,
    )

    await provisioner.prepare(RUN_ID, policy)

    diagnostics = repr(
        (
            log,
            tuple(uow.events.values),
            tuple(operations.intents.values()),
            tuple(requests),
        )
    )
    for forbidden in (
        database.environment_value,
        "feature/setup-order",
        str(Path.cwd()),
    ):
        assert forbidden not in diagnostics


@pytest.mark.asyncio
async def test_repeated_prepare_accepts_later_non_resource_run_versions() -> None:
    log, uow, _operations, policy, provisioner = _case()
    await provisioner.prepare(RUN_ID, policy)
    run_count = len([entry for entry in log if entry.startswith("run:")])
    prepared_version = uow.runs.current.version
    state_engine = StateEngine()
    uow.runs.current = state_engine.resume(state_engine.pause(uow.runs.current))

    await provisioner.prepare(RUN_ID, policy)

    assert uow.runs.current.state is RunState.PREPARING_WORKTREE
    assert uow.runs.current.version == prepared_version + 2
    assert len([entry for entry in log if entry.startswith("run:")]) == run_count
    assert [event.event_type for event in uow.events.values].count(
        "resource.worktree_prepared"
    ) == 1


@pytest.mark.parametrize(
    "boundary",
    ["load-context", "setup-checkpoint", "prepared-checkpoint"],
)
@pytest.mark.asyncio
async def test_persistence_boundaries_drop_raw_exception_context(boundary: str) -> None:
    from forge.tools.worktree import WorktreeIntegrityError, WorktreeReconciliationRequired

    _log, uow, _operations, policy, provisioner = _case()
    if boundary == "load-context":
        uow.events.fail_list_on_call = 1
        expected_error = WorktreeIntegrityError
    else:
        uow.runs.fail_event_type = (
            "resource.setup_step_completed"
            if boundary == "setup-checkpoint"
            else "resource.worktree_prepared"
        )
        expected_error = WorktreeReconciliationRequired

    with pytest.raises(expected_error) as captured:
        await provisioner.prepare(RUN_ID, policy)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert PERSISTENCE_ERROR_SENTINEL not in repr(captured.value)
