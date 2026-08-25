"""Durable persisted-run worktree preparation contracts."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from forge.application.ports.worktrees import DatabaseBinding, ManagedWorktree
from forge.domain.event import RunEvent
from forge.domain.operation import (
    OperationExecutionClaim,
    OperationIntent,
    OperationOutcome,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import DatabaseProvisioningPolicy, ProjectPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
BASE_SHA = "a" * 40


def _policy(*, enabled: bool = False, repository_path: str | None = None) -> ProjectPolicy:
    return ProjectPolicy(
        id=PROJECT_ID,
        version=7,
        repository_path=repository_path or str(Path.cwd()),
        github_repository="forge/example",
        default_branch="main",
        database=DatabaseProvisioningPolicy(
            enabled=enabled,
            admin_url_secret_reference="secret://admin/postgres" if enabled else None,
        ),
    )


@dataclass
class _Events:
    values: list[RunEvent] = field(default_factory=list)
    fail_next_append: bool = False

    async def append(self, event: RunEvent) -> RunEvent:
        assert isinstance(event, RunEvent)
        if self.fail_next_append:
            self.fail_next_append = False
            raise RuntimeError("event persistence sentinel")
        self.values.append(event)
        return event

    async def list_after(self, run_id: UUID, sequence: int) -> list[Any]:
        del run_id, sequence
        return list(self.values)


@dataclass
class _Runs:
    current: RunSnapshot
    events: _Events
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

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
        self.calls.append((values["event_type"], values))
        previous = self.current
        changed = self.current.with_resource(
            worktree_path=values["worktree_path"],
            database_state=values["database_state"],
            database_name=values["database_name"],
            database_role=values["database_role"],
            secret_id=values["secret_id"],
        )
        self.current = changed
        try:
            await self.events.append(
                RunEvent(
                    run_id=changed.id,
                    run_version=changed.version,
                    event_type=values["event_type"],
                    payload=values["event_payload"],
                )
            )
        except BaseException:
            self.current = previous
            raise
        return changed


@dataclass
class _Uow:
    runs: _Runs
    events: _Events
    log: list[str]
    committed: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        self.committed = True
        self.log.append("resource.commit")


class _UowFactory:
    def __init__(self, run: RunSnapshot, log: list[str]) -> None:
        self.events = _Events()
        self.runs = _Runs(run, self.events)
        self.log = log

    def __call__(self) -> _Uow:
        return _Uow(self.runs, self.events, self.log)


class _Operations:
    def __init__(self, run_id: UUID, log: list[str]) -> None:
        self.run_id = run_id
        self.log = log
        self.intent: OperationIntent | None = None
        self.outcome: OperationOutcome | None = None
        self.by_key: dict[str, OperationIntent] = {}

    async def begin(self, **values: Any) -> OperationIntent:
        existing = self.by_key.get(values["idempotency_key"])
        if existing is not None:
            if (
                existing.run_id != values["run_id"]
                or existing.kind != values["operation_type"]
                or existing.request_digest != values["request_digest"]
                or canonical_digest(existing.request_payload)
                != canonical_digest(values["request_payload"])
            ):
                raise ValueError("operation idempotency key has a different request")
            return replace(existing, is_new=False)
        self.log.append("intent.commit")
        now = datetime.now(UTC)
        self.intent = OperationIntent(
            id=uuid4(),
            run_id=self.run_id,
            kind=values["operation_type"],
            idempotency_key=values["idempotency_key"],
            request_digest=values["request_digest"],
            request_payload=values["request_payload"],
            status=OperationStatus.PENDING,
            created_at=now,
            updated_at=now,
            is_new=True,
        )
        self.by_key[self.intent.idempotency_key] = self.intent
        return self.intent

    async def complete(
        self, intent_id: UUID, outcome: OperationOutcome, **_: Any
    ) -> OperationIntent:
        assert self.intent is not None and intent_id == self.intent.id
        self.log.append("operation.complete")
        self.outcome = outcome
        now = self.intent.created_at
        self.intent = replace(
            self.intent,
            status=OperationStatus.SUCCEEDED,
            remote_resource_id=outcome.remote_resource_id,
            updated_at=now,
            completed_at=now,
            outcome=outcome.payload,
            outcome_schema_version=outcome.outcome_schema_version,
            is_new=False,
        )
        self.by_key[self.intent.idempotency_key] = self.intent
        return self.intent

    async def get_by_idempotency_key(self, idempotency_key: str) -> OperationIntent | None:
        return self.by_key.get(idempotency_key)

    async def get(self, intent_id: UUID) -> OperationIntent:
        for intent in self.by_key.values():
            if intent.id == intent_id:
                return intent
        raise AssertionError("unknown operation intent")

    async def claim_for_recovery(
        self, intent_id: UUID, *, owner_id: str, lease_seconds: float
    ) -> OperationExecutionClaim:
        del owner_id, lease_seconds
        intent = await self.get(intent_id)
        return OperationExecutionClaim(intent=intent, acquired=True)

    async def fail(
        self,
        intent_id: UUID,
        *,
        error: str,
        needs_reconciliation: bool = False,
        owner_id: str | None = None,
    ) -> OperationIntent:
        del owner_id
        current = await self.get(intent_id)
        now = current.updated_at or current.created_at
        assert now is not None
        failed = replace(
            current,
            status=(
                OperationStatus.NEEDS_RECONCILIATION
                if needs_reconciliation
                else OperationStatus.FAILED
            ),
            updated_at=now,
            completed_at=None if needs_reconciliation else now,
            outcome=None,
            outcome_schema_version=None,
            remote_resource_id=None,
            error=error,
            is_new=False,
        )
        self.by_key[current.idempotency_key] = failed
        self.intent = failed
        return failed


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
        if not intent.is_new:
            outcome = await adapter.reconcile(intent)
            if outcome.status is OperationStatus.SUCCEEDED:
                await self.operations.complete(intent.id, outcome)
            else:
                await self.operations.fail(
                    intent.id,
                    error=outcome.error or "operation could not be reconciled",
                    needs_reconciliation=True,
                )
            return outcome
        outcome = await adapter.invoke(intent)
        await self.operations.complete(intent.id, outcome)
        return outcome


class _ForgingExecutor(_Executor):
    async def execute(self, request: Any, adapter: Any) -> OperationOutcome:
        authoritative = await self.operations.begin(
            run_id=request.run_id,
            operation_type=request.kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
        )
        forged = replace(authoritative, id=uuid4())
        return await adapter.invoke(forged)


class _Git:
    repository_path = Path.cwd()

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.handle: ManagedWorktree | None = None
        self.branch_present = True

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

    def remove_worktree(self, worktree: ManagedWorktree) -> None:
        assert self.handle == worktree
        self.log.append("git.remove")
        self.handle = None

    def verify_worktree_absent(self, worktree: ManagedWorktree) -> None:
        self.log.append("git.verify_absent")
        assert self.handle is None
        assert self.branch_present is True
        assert worktree == self.expected_worktree(worktree.identity, worktree.base_sha)

    def prune(self) -> None:
        self.log.append("git.prune")


class _DelayedGit(_Git):
    def __init__(self, log: list[str]) -> None:
        super().__init__(log)
        self.started = threading.Event()
        self.release = threading.Event()

    def create_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        self.started.set()
        self.release.wait(timeout=5)
        return super().create_worktree(identity, base_sha)


class _DelayedRemoveGit(_Git):
    def __init__(self, log: list[str]) -> None:
        super().__init__(log)
        self.remove_started = threading.Event()
        self.remove_release = threading.Event()

    def remove_worktree(self, worktree: ManagedWorktree) -> None:
        self.remove_started.set()
        self.remove_release.wait(timeout=5)
        super().remove_worktree(worktree)


class _RemoveThenRaiseGit(_Git):
    def remove_worktree(self, worktree: ManagedWorktree) -> None:
        super().remove_worktree(worktree)
        raise RuntimeError("raw git removal sentinel")


class _BranchRetainingGit(_Git):
    def inspect_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree | None:
        inspected = super().inspect_worktree(identity, base_sha)
        if inspected is None and self.branch_present:
            raise RuntimeError("creation inspection rejects a retained branch")
        return inspected


class _Database:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.calls = 0

    def validate_binding(
        self, identity: WorktreeIdentity, binding: DatabaseBinding
    ) -> DatabaseBinding:
        del identity
        return binding

    async def verify_active(self, *args: Any, **kwargs: Any) -> UUID:
        raise AssertionError("disabled preparation must not verify a database")

    async def provision(self, *args: Any, **kwargs: Any) -> DatabaseBinding:
        self.calls += 1
        raise AssertionError("disabled preparation must not provision a database")

    async def teardown(self, *args: Any, **kwargs: Any) -> DatabaseBinding:
        self.calls += 1
        raise AssertionError("disabled teardown must not call the database boundary")


class _EnabledDatabase:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.provision_calls = 0
        self.verify_calls = 0
        self.binding: DatabaseBinding | None = None
        self.intent_id = uuid4()

    def validate_binding(
        self, identity: WorktreeIdentity, binding: DatabaseBinding
    ) -> DatabaseBinding:
        assert binding.database_name == identity.database_name
        assert binding.database_role == identity.database_role
        if binding.state is ResourceState.ACTIVE or binding.secret_id is not None:
            assert binding.secret_id == f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}"
        return binding

    async def provision(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        del policy, policy_version
        self.log.append("database.provision")
        self.provision_calls += 1
        self.binding = DatabaseBinding(
            state=ResourceState.ACTIVE,
            database_name=identity.database_name,
            database_role=identity.database_role,
            secret_id=f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}",
            environment={"DATABASE_URL": "postgresql://transient-secret"},
        )
        return self.binding

    async def verify_active(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int,
    ) -> UUID:
        del identity, policy, resource, policy_version
        self.log.append("database.verify")
        self.verify_calls += 1
        return self.intent_id

    async def teardown(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        del identity, policy, resource, policy_version
        self.log.append("database.teardown")
        return DatabaseBinding(state=ResourceState.REMOVED)


class _FailOnceTeardownDatabase(_EnabledDatabase):
    def __init__(self, log: list[str]) -> None:
        super().__init__(log)
        self.teardown_attempts = 0

    async def teardown(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        self.teardown_attempts += 1
        if self.teardown_attempts == 1:
            self.log.append("database.teardown.failed")
            raise RuntimeError("raw database teardown sentinel")
        return await super().teardown(
            identity,
            policy,
            resource,
            policy_version=policy_version,
        )


class _ForeignDatabase(_EnabledDatabase):
    async def provision(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        del identity, policy, policy_version
        self.log.append("database.provision")
        self.provision_calls += 1
        self.binding = DatabaseBinding(
            state=ResourceState.ACTIVE,
            database_name="foreign_database",
            database_role="foreign_role",
            secret_id="foreign_secret",
        )
        return self.binding


class _ConcurrentDatabase(_EnabledDatabase):
    def __init__(self, log: list[str]) -> None:
        super().__init__(log)
        self.arrivals = 0
        self.all_arrived = asyncio.Event()
        self.effect_calls = 0

    async def provision(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        del policy, policy_version
        self.log.append("database.provision")
        self.provision_calls += 1
        self.arrivals += 1
        if self.arrivals == 2:
            self.all_arrived.set()
        await self.all_arrived.wait()
        if self.binding is None:
            self.effect_calls += 1
            self.binding = DatabaseBinding(
                state=ResourceState.ACTIVE,
                database_name=identity.database_name,
                database_role=identity.database_role,
                secret_id=f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}",
            )
        return self.binding


@pytest.mark.asyncio
async def test_disabled_prepare_commits_safe_checkpoints_before_git_and_skips_database() -> None:
    log: list[str] = []
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=uuid4(),
        state=RunState.PREPARING_WORKTREE,
        policy_version=7,
        base_ref="refs/heads/main",
        base_sha=BASE_SHA,
        branch_name="feature/e1",
    )
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _Git(log)
    database = _Database(log)

    from forge.tools.worktree import WorktreeProvisioner

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )

    result = await provisioner.prepare(RUN_ID, _policy(repository_path=str(git.repository_path)))

    assert result == git.handle
    assert database.calls == 0
    assert log.index("intent.commit") < log.index("resource.commit") < log.index("git.create")
    assert log.index("git.create") < log.index("operation.complete")
    assert uow_factory.runs.current.database_state is ResourceState.DISABLED
    assert uow_factory.runs.current.worktree_path == str(result.path)


@pytest.mark.asyncio
async def test_enabled_prepare_persists_exact_binding_after_git_and_redacts_environment() -> None:
    log: list[str] = []
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=uuid4(),
        state=RunState.PREPARING_WORKTREE,
        policy_version=7,
        base_ref="refs/heads/main",
        base_sha=BASE_SHA,
        branch_name="feature/e1-enabled",
    )
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _Git(log)
    database = _EnabledDatabase(log)

    from forge.tools.worktree import WorktreeProvisioner

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )

    result = await provisioner.prepare(
        RUN_ID,
        _policy(enabled=True, repository_path=str(git.repository_path)),
    )

    assert result == git.handle
    assert database.provision_calls == 1
    assert database.verify_calls >= 1
    assert (
        log.index("git.create") < log.index("operation.complete") < log.index("database.provision")
    )
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, "feature/e1-enabled", True)
    assert uow_factory.runs.current.database_state is ResourceState.ACTIVE
    assert uow_factory.runs.current.worktree_path == str(result.path)
    assert uow_factory.runs.current.database_name == identity.database_name
    assert uow_factory.runs.current.database_role == identity.database_role
    assert uow_factory.runs.current.secret_id == (f"forge_db_{PROJECT_ID.hex}_{RUN_ID.hex}")
    resource_fields = {
        "worktree_path",
        "database_state",
        "database_name",
        "database_role",
        "secret_id",
    }
    assert all(resource_fields <= values.keys() for _event, values in uow_factory.runs.calls)
    assert [event for event, _values in uow_factory.runs.calls] == [
        "resource.worktree_preparing",
        "resource.worktree_created",
        "resource.database_active",
    ]
    assert uow_factory.runs.calls[1][1]["database_name"] == identity.database_name
    assert uow_factory.runs.calls[1][1]["database_role"] == identity.database_role
    assert uow_factory.runs.calls[1][1]["secret_id"] is None
    active_event = next(
        event
        for event in uow_factory.events.values
        if event.event_type == "resource.database_active"
    )
    assert active_event.payload["database_intent_id"] == str(database.intent_id)
    assert "transient-secret" not in repr(database.binding)
    assert all("transient-secret" not in repr(event) for event in uow_factory.events.values)
    assert all("transient-secret" not in repr(value) for value in operations.by_key.values())
    safe_records = (
        tuple(uow_factory.events.values),
        tuple(operations.by_key.values()),
        operations.outcome,
    )
    assert "feature/e1-enabled" not in repr(safe_records)
    assert str(result.path) not in repr(safe_records)


@pytest.mark.asyncio
async def test_foreign_database_binding_records_only_truthful_expected_failure_state() -> None:
    log: list[str] = []
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=uuid4(),
        state=RunState.PREPARING_WORKTREE,
        policy_version=7,
        base_ref="refs/heads/main",
        base_sha=BASE_SHA,
        branch_name="feature/e1-foreign-database",
    )
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _Git(log)
    database = _ForeignDatabase(log)

    from forge.tools.worktree import WorktreeProvisioner, WorktreeReconciliationRequired

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(
            RUN_ID,
            _policy(enabled=True, repository_path=str(git.repository_path)),
        )

    identity = WorktreeIdentity.for_run(
        PROJECT_ID,
        RUN_ID,
        "feature/e1-foreign-database",
        True,
    )
    assert uow_factory.runs.current.database_state is ResourceState.FAILED
    assert uow_factory.runs.current.database_name == identity.database_name
    assert uow_factory.runs.current.database_role == identity.database_role
    assert uow_factory.runs.current.secret_id is None
    assert all(
        event.event_type != "resource.database_active" for event in uow_factory.events.values
    )


@pytest.mark.asyncio
async def test_concurrent_enabled_prepares_converge_on_one_exact_active_checkpoint() -> None:
    branch = "feature/e1-concurrent-active"
    run = _run(branch=branch)
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _Git(log)
    database = _ConcurrentDatabase(log)
    policy = _policy(enabled=True, repository_path=str(git.repository_path))

    from forge.tools.worktree import (
        WorktreeProvisioner,
        _checkpoint_payload,
        _request,
        _worktree_outcome,
    )

    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch, True)
    request = _request(run, identity, policy)
    intent = await operations.begin(
        run_id=RUN_ID,
        operation_type=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
    )
    expected = git.expected_worktree(identity, BASE_SHA)
    git.handle = expected
    partial = await uow_factory.runs.update_resource(
        RUN_ID,
        run.version,
        worktree_path=str(expected.path),
        database_state=ResourceState.PROVISIONING,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=None,
        event_type="resource.worktree_preparing",
        event_payload=_checkpoint_payload(
            request,
            intent.id,
            target_state=ResourceState.PROVISIONING,
        ),
    )
    await uow_factory.runs.update_resource(
        RUN_ID,
        partial.version,
        worktree_path=str(expected.path),
        database_state=ResourceState.PROVISIONING,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=None,
        event_type="resource.worktree_created",
        event_payload=_checkpoint_payload(
            request,
            intent.id,
            target_state=ResourceState.PROVISIONING,
        ),
    )
    await operations.complete(intent.id, _worktree_outcome(request, identity))
    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )

    results = await asyncio.gather(
        provisioner.prepare(RUN_ID, policy),
        provisioner.prepare(RUN_ID, policy),
        return_exceptions=True,
    )

    assert results == [expected, expected]
    assert database.effect_calls == 1
    assert (
        sum(event.event_type == "resource.database_active" for event in uow_factory.events.values)
        == 1
    )
    assert uow_factory.runs.current.database_state is ResourceState.ACTIVE


def _run(*, enabled: bool = False, branch: str = "feature/e1") -> RunSnapshot:
    return RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=uuid4(),
        state=RunState.PREPARING_WORKTREE,
        policy_version=7,
        base_ref="refs/heads/main",
        base_sha=BASE_SHA,
        branch_name=branch,
        database_state=ResourceState.DISABLED,
    )


def _provisioner(
    run: RunSnapshot,
    *,
    enabled: bool = False,
) -> tuple[Any, _UowFactory, _Operations, _Git, list[str]]:
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _Git(log)
    database = _EnabledDatabase(log) if enabled else _Database(log)
    from forge.tools.worktree import WorktreeProvisioner

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )
    return provisioner, uow_factory, operations, git, log


@pytest.mark.asyncio
async def test_teardown_request_identity_ignores_mutable_database_state() -> None:
    run = _run(enabled=True, branch="feature/teardown-stable-request")
    policy = _policy(enabled=True)
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", True)
    from forge.tools.worktree import _teardown_request

    provisioning = _teardown_request(
        replace(run, database_state=ResourceState.PROVISIONING), identity, policy
    )
    failed = _teardown_request(replace(run, database_state=ResourceState.FAILED), identity, policy)

    assert set(provisioning.request_payload) == {
        "project_id",
        "run_id",
        "policy_version",
        "branch_digest",
        "worktree_name",
        "base_sha",
    }
    assert provisioning.idempotency_key == failed.idempotency_key
    assert provisioning.request_digest == failed.request_digest
    assert provisioning.request_payload == failed.request_payload

    operations = _Operations(RUN_ID, [])
    original = await operations.begin(
        run_id=RUN_ID,
        operation_type=provisioning.kind,
        idempotency_key=provisioning.idempotency_key,
        request_digest=provisioning.request_digest,
        request_payload=provisioning.request_payload,
    )
    adopted = await operations.begin(
        run_id=RUN_ID,
        operation_type=failed.kind,
        idempotency_key=failed.idempotency_key,
        request_digest=failed.request_digest,
        request_payload=failed.request_payload,
    )

    assert adopted.id == original.id
    assert adopted.is_new is False


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_kind", ("missing", "foreign", "mismatched"))
async def test_teardown_reconcile_rejects_invalid_removal_checkpoint(
    checkpoint_kind: str,
) -> None:
    run = _run(enabled=True, branch="feature/teardown-missing-checkpoint")
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _BranchRetainingGit(log)
    database = _EnabledDatabase(log)
    from forge.tools.worktree import (
        WorktreeProvisioner,
        _teardown_checkpoint_payload,
        _teardown_request,
    )

    policy = _policy(enabled=True, repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", True)
    run = replace(
        run,
        worktree_path=None,
        database_state=ResourceState.ACTIVE,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=f"forge_db_{PROJECT_ID.hex}_{RUN_ID.hex}",
    )
    uow_factory.runs.current = run
    request = _teardown_request(run, identity, policy)
    intent = await operations.begin(
        run_id=RUN_ID,
        operation_type=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
    )
    if checkpoint_kind != "missing":
        checkpoint_intent_id = intent.id if checkpoint_kind == "mismatched" else uuid4()
        payload = dict(
            _teardown_checkpoint_payload(
                request,
                checkpoint_intent_id,
                target_state=ResourceState.ACTIVE,
            )
        )
        if checkpoint_kind == "mismatched":
            payload["base_sha"] = "b" * 40
        await uow_factory.events.append(
            RunEvent(
                run_id=RUN_ID,
                run_version=run.version,
                event_type="resource.worktree_removed",
                payload=payload,
            )
        )
    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )

    reconciled = await provisioner.reconcile(intent.id, policy)

    assert reconciled.status is OperationStatus.NEEDS_RECONCILIATION
    assert "database.teardown" not in log


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ("missing", "foreign", "mismatched", "live"))
async def test_succeeded_teardown_revalidates_checkpoint_and_absence_before_database(
    evidence: str,
) -> None:
    run = _run(enabled=True, branch="feature/teardown-succeeded-retry")
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _BranchRetainingGit(log)
    database = _EnabledDatabase(log)
    from forge.tools.worktree import (
        WorktreeProvisioner,
        WorktreeProvisionerError,
        _teardown_checkpoint_payload,
        _teardown_request,
        _worktree_outcome,
    )

    policy = _policy(enabled=True, repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", True)
    run = replace(
        run,
        worktree_path=None,
        database_state=ResourceState.ACTIVE,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=f"forge_db_{PROJECT_ID.hex}_{RUN_ID.hex}",
    )
    uow_factory.runs.current = run
    request = _teardown_request(run, identity, policy)
    intent = await operations.begin(
        run_id=RUN_ID,
        operation_type=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
    )
    await operations.complete(intent.id, _worktree_outcome(request, identity))
    if evidence != "missing":
        checkpoint_intent_id = uuid4() if evidence == "foreign" else intent.id
        payload = dict(
            _teardown_checkpoint_payload(
                request,
                checkpoint_intent_id,
                target_state=ResourceState.ACTIVE,
            )
        )
        if evidence == "mismatched":
            payload["base_sha"] = "b" * 40
        await uow_factory.events.append(
            RunEvent(
                run_id=RUN_ID,
                run_version=run.version,
                event_type="resource.worktree_removed",
                payload=payload,
            )
        )
    if evidence == "live":
        git.handle = git.expected_worktree(identity, BASE_SHA)
    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )

    with pytest.raises(WorktreeProvisionerError):
        await provisioner.teardown(RUN_ID, policy)

    assert "database.teardown" not in log
    assert uow_factory.runs.current.database_state is ResourceState.ACTIVE
    assert operations.intent is not None
    assert operations.intent.status is OperationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_disabled_teardown_removes_exact_worktree_and_keeps_branch() -> None:
    run = _run(branch="feature/teardown-disabled")
    provisioner, uow_factory, operations, git, log = _provisioner(run)
    policy = _policy(repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", False)
    expected = git.expected_worktree(identity, BASE_SHA)
    git.handle = expected
    uow_factory.runs.current = replace(run, worktree_path=str(expected.path))

    removed = await provisioner.teardown(RUN_ID, policy)

    assert removed == uow_factory.runs.current
    assert removed.worktree_path is None
    assert removed.database_state is ResourceState.DISABLED
    assert git.handle is None
    assert git.branch_present is True
    assert operations.intent is not None
    assert operations.intent.kind == "worktree.teardown"
    assert [
        entry
        for entry in log
        if entry
        in {
            "intent.commit",
            "git.inspect",
            "git.remove",
            "git.verify_absent",
            "git.prune",
            "resource.commit",
            "operation.complete",
        }
    ] == [
        "intent.commit",
        "git.inspect",
        "git.remove",
        "git.verify_absent",
        "git.prune",
        "resource.commit",
        "operation.complete",
    ]
    assert [event.event_type for event in uow_factory.events.values] == [
        "resource.worktree_removed"
    ]


@pytest.mark.asyncio
async def test_teardown_rejects_wrong_persisted_path_before_any_effect() -> None:
    run = _run(branch="feature/teardown-wrong-path")
    provisioner, uow_factory, operations, git, log = _provisioner(run)
    policy = _policy(repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", False)
    git.handle = git.expected_worktree(identity, BASE_SHA)
    uow_factory.runs.current = replace(
        run,
        worktree_path=str(git.repository_path / ".worktrees" / "foreign"),
    )
    from forge.tools.worktree import WorktreeIntegrityError

    with pytest.raises(WorktreeIntegrityError) as captured:
        await provisioner.teardown(RUN_ID, policy)

    assert captured.value.__cause__ is None
    assert operations.by_key == {}
    assert "git.remove" not in log
    assert "git.prune" not in log
    assert uow_factory.events.values == []


@pytest.mark.asyncio
async def test_active_database_teardown_runs_only_after_worktree_checkpoint() -> None:
    run = _run(branch="feature/teardown-active")
    provisioner, uow_factory, _operations, git, log = _provisioner(run, enabled=True)
    policy = _policy(enabled=True, repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", True)
    expected = git.expected_worktree(identity, BASE_SHA)
    git.handle = expected
    uow_factory.runs.current = replace(
        run,
        worktree_path=str(expected.path),
        database_state=ResourceState.ACTIVE,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=f"forge_db_{PROJECT_ID.hex}_{RUN_ID.hex}",
    )

    removed = await provisioner.teardown(RUN_ID, policy)

    assert removed.worktree_path is None
    assert removed.database_state is ResourceState.REMOVED
    assert removed.database_name is None
    assert removed.database_role is None
    assert removed.secret_id is None
    assert log.index("git.remove") < log.index("database.teardown")
    assert [event.event_type for event in uow_factory.events.values] == [
        "resource.worktree_removed",
        "resource.database_removed",
    ]
    assert git.branch_present is True
    repeated = await provisioner.teardown(RUN_ID, policy)

    assert repeated == removed
    assert log.count("git.remove") == 1
    assert log.count("database.teardown") == 1


@pytest.mark.asyncio
async def test_teardown_uses_removal_inspection_when_branch_is_retained() -> None:
    run = _run(enabled=True, branch="feature/teardown-retained-branch")
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _BranchRetainingGit(log)
    database = _EnabledDatabase(log)
    from forge.tools.worktree import WorktreeProvisioner

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )
    policy = _policy(enabled=True, repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", True)
    expected = git.expected_worktree(identity, BASE_SHA)
    git.handle = expected
    uow_factory.runs.current = replace(
        run,
        worktree_path=str(expected.path),
        database_state=ResourceState.ACTIVE,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=f"forge_db_{PROJECT_ID.hex}_{RUN_ID.hex}",
    )

    removed = await provisioner.teardown(RUN_ID, policy)

    assert removed.worktree_path is None
    assert removed.database_state is ResourceState.REMOVED
    assert log.index("git.verify_absent") < log.index("database.teardown")


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_database_teardown_failure_preserves_exact_state_for_retry() -> None:
    run = _run(branch="feature/teardown-database-retry")
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _Git(log)
    database = _FailOnceTeardownDatabase(log)
    from forge.tools.worktree import WorktreeProvisioner, WorktreeReconciliationRequired

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )
    policy = _policy(enabled=True, repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", True)
    expected = git.expected_worktree(identity, BASE_SHA)
    git.handle = expected
    uow_factory.runs.current = replace(
        run,
        worktree_path=str(expected.path),
        database_state=ResourceState.ACTIVE,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=f"forge_db_{PROJECT_ID.hex}_{RUN_ID.hex}",
    )

    with pytest.raises(WorktreeReconciliationRequired) as captured:
        await provisioner.teardown(RUN_ID, policy)

    assert captured.value.__cause__ is None
    assert uow_factory.runs.current.worktree_path is None
    assert uow_factory.runs.current.database_state is ResourceState.ACTIVE
    assert uow_factory.runs.current.database_name == identity.database_name
    assert uow_factory.runs.current.database_role == identity.database_role

    removed = await provisioner.teardown(RUN_ID, policy)

    assert removed.database_state is ResourceState.REMOVED
    assert database.teardown_attempts == 2
    assert log.count("git.remove") == 1


async def test_teardown_cancellation_reconciles_original_intent_on_retry() -> None:
    run = _run(branch="feature/teardown-cancel")
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _DelayedRemoveGit(log)
    database = _Database(log)
    from forge.tools.worktree import WorktreeProvisioner

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )
    policy = _policy(repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", False)
    expected = git.expected_worktree(identity, BASE_SHA)
    git.handle = expected
    uow_factory.runs.current = replace(run, worktree_path=str(expected.path))

    task = asyncio.create_task(provisioner.teardown(RUN_ID, policy))
    assert await asyncio.to_thread(git.remove_started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    git.remove_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert uow_factory.runs.current.worktree_path is None
    assert [event.event_type for event in uow_factory.events.values] == [
        "resource.worktree_removed"
    ]
    assert operations.intent is not None
    assert operations.intent.status is OperationStatus.PENDING

    reconciled = await provisioner.reconcile(operations.intent.id, policy)
    assert reconciled.status is OperationStatus.SUCCEEDED
    assert log.count("git.remove") == 1
    assert log.count("git.verify_absent") == 2

    removed = await provisioner.teardown(RUN_ID, policy)

    assert removed.worktree_path is None
    assert operations.intent is not None
    assert operations.intent.status is OperationStatus.SUCCEEDED
    assert log.count("git.remove") == 1


@pytest.mark.asyncio
async def test_teardown_adopts_exact_absence_after_git_reports_failure() -> None:
    run = _run(branch="feature/teardown-terminal-error")
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _RemoveThenRaiseGit(log)
    database = _Database(log)
    from forge.tools.worktree import WorktreeProvisioner

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )
    policy = _policy(repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", False)
    expected = git.expected_worktree(identity, BASE_SHA)
    git.handle = expected
    uow_factory.runs.current = replace(run, worktree_path=str(expected.path))

    removed = await provisioner.teardown(RUN_ID, policy)

    assert removed.worktree_path is None
    assert log.count("git.remove") == 1
    assert [event.event_type for event in uow_factory.events.values] == [
        "resource.worktree_removed"
    ]

    assert operations.intent is not None
    assert operations.intent.status is OperationStatus.SUCCEEDED
    assert log.count("git.remove") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "run_update", "policy_update"),
    (
        ("wrong state", {"state": RunState.IMPLEMENTING}, {}),
        ("foreign project", {"project_id": UUID("33333333-3333-3333-3333-333333333333")}, {}),
        ("wrong policy version", {}, {"version": 8}),
        ("foreign repository", {}, {"repository_path": str(Path.cwd() / "foreign")}),
        (
            "non-canonical repository alias",
            {},
            {"repository_path": str(Path.cwd() / "nested" / "..")},
        ),
        ("missing branch", {"branch_name": None}, {}),
        ("missing base", {"base_sha": None}, {}),
        ("removed resource", {"database_state": ResourceState.REMOVED}, {}),
    ),
)
async def test_prepare_rejects_authoritative_validation_failures_before_effects(
    label: str,
    run_update: dict[str, Any],
    policy_update: dict[str, Any],
) -> None:
    del label
    run = replace(_run(), **run_update)
    provisioner, uow_factory, operations, git, log = _provisioner(run)
    policy = _policy(repository_path=str(git.repository_path)).model_copy(update=policy_update)

    from forge.tools.worktree import WorktreeProvisionerError

    with pytest.raises(WorktreeProvisionerError):
        await provisioner.prepare(RUN_ID, policy)

    assert operations.by_key == {}
    assert git.handle is None
    assert uow_factory.runs.calls == []
    assert "git.create" not in log


@pytest.mark.asyncio
async def test_forged_disabled_resource_path_is_rejected_before_operation_intent() -> None:
    run = replace(_run(), worktree_path=str(Path.cwd() / ".worktrees" / "foreign"))
    provisioner, uow_factory, operations, git, log = _provisioner(run)

    from forge.tools.worktree import WorktreeProvisionerError

    with pytest.raises(WorktreeProvisionerError):
        await provisioner.prepare(RUN_ID, _policy(repository_path=str(git.repository_path)))

    assert operations.by_key == {}
    assert uow_factory.runs.calls == []
    assert "git.create" not in log


@pytest.mark.asyncio
async def test_partial_event_failure_rolls_back_before_git_with_all_resource_fields() -> None:
    run = _run()
    provisioner, uow_factory, operations, git, log = _provisioner(run)
    uow_factory.events.fail_next_append = True

    from forge.tools.worktree import WorktreeReconciliationRequired

    with pytest.raises(WorktreeReconciliationRequired) as captured:
        await provisioner.prepare(
            RUN_ID,
            _policy(repository_path=str(git.repository_path)),
        )

    assert captured.value.__cause__ is None
    assert uow_factory.runs.current == run
    assert uow_factory.events.values == []
    assert len(uow_factory.runs.calls) == 1
    assert {
        "worktree_path",
        "database_state",
        "database_name",
        "database_role",
        "secret_id",
    } <= uow_factory.runs.calls[0][1].keys()
    assert len(operations.by_key) == 1
    assert "git.create" not in log


@pytest.mark.asyncio
async def test_reconcile_adopts_present_worktree_without_git_creation_or_duplicate_checkpoint() -> (
    None
):
    run = _run()
    provisioner, uow_factory, operations, git, log = _provisioner(run)
    policy = _policy(repository_path=str(git.repository_path))

    from forge.tools.worktree import _checkpoint_payload, _request

    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", False)
    request = _request(run, identity, policy)
    intent = await operations.begin(
        run_id=RUN_ID,
        operation_type=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
    )
    expected = git.expected_worktree(identity, BASE_SHA)
    git.handle = expected
    await uow_factory.runs.update_resource(
        RUN_ID,
        run.version,
        worktree_path=str(expected.path),
        database_state=ResourceState.DISABLED,
        database_name=None,
        database_role=None,
        secret_id=None,
        event_type="resource.worktree_preparing",
        event_payload=_checkpoint_payload(
            request,
            intent.id,
            target_state=ResourceState.DISABLED,
        ),
    )

    result = await provisioner.reconcile(intent.id, policy)

    assert result.status is OperationStatus.SUCCEEDED
    assert "git.create" not in log
    assert (
        sum(
            event.event_type == "resource.worktree_reconciled"
            for event in uow_factory.events.values
        )
        == 1
    )
    assert uow_factory.runs.current.worktree_path == str(expected.path)


@pytest.mark.asyncio
async def test_reconcile_fails_closed_when_checkpoint_has_no_authoritative_operation() -> None:
    run = _run()
    provisioner, uow_factory, operations, git, log = _provisioner(run)
    policy = _policy(repository_path=str(git.repository_path))
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", False)
    expected = git.expected_worktree(identity, BASE_SHA)
    uow_factory.runs.current = replace(uow_factory.runs.current, worktree_path=str(expected.path))
    await uow_factory.events.append(
        RunEvent(
            run_id=RUN_ID,
            run_version=uow_factory.runs.current.version,
            event_type="resource.worktree_preparing",
            payload={
                "operation_intent_id": str(uuid4()),
                "project_id": str(PROJECT_ID),
                "run_id": str(RUN_ID),
                "policy_version": 7,
                "branch_digest": hashlib.sha256(identity.branch.encode()).hexdigest(),
                "worktree_name": identity.worktree_name,
                "base_sha": BASE_SHA,
                "database_state": ResourceState.DISABLED.value,
            },
        )
    )

    from forge.tools.worktree import WorktreeReconciliationRequired

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(RUN_ID, policy)

    assert operations.by_key == {}
    assert "git.create" not in log


@pytest.mark.asyncio
async def test_prepare_rejects_forged_adapter_intent_before_checkpoint_or_git() -> None:
    run = _run()
    provisioner, uow_factory, operations, git, log = _provisioner(run)
    provisioner._operation_executor = _ForgingExecutor(operations)

    from forge.tools.worktree import WorktreeReconciliationRequired

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(
            RUN_ID,
            _policy(repository_path=str(git.repository_path)),
        )

    assert len(operations.by_key) == 1
    assert uow_factory.runs.calls == []
    assert "git.create" not in log


@pytest.mark.asyncio
async def test_cancellation_waits_for_git_terminal_result_and_records_exact_path() -> None:
    run = _run()
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _DelayedGit(log)
    database = _Database(log)
    from forge.tools.worktree import WorktreeProvisioner

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )
    task = asyncio.create_task(
        provisioner.prepare(RUN_ID, _policy(repository_path=str(git.repository_path)))
    )
    assert await asyncio.to_thread(git.started.wait, 2)
    task.cancel()
    git.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    expected = git.expected_worktree(
        WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, run.branch_name or "", False),
        BASE_SHA,
    )
    assert git.handle == expected
    assert uow_factory.runs.current.worktree_path == str(expected.path)
    assert any(
        event.event_type == "resource.worktree_created" for event in uow_factory.events.values
    )
    assert database.calls == 0
    assert "operation.complete" not in log


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_enabled_git_and_never_starts_database() -> None:
    run = _run(branch="feature/e1-cancel-enabled")
    log: list[str] = []
    uow_factory = _UowFactory(run, log)
    operations = _Operations(RUN_ID, log)
    git = _DelayedGit(log)
    database = _EnabledDatabase(log)
    from forge.tools.worktree import WorktreeProvisioner

    provisioner = WorktreeProvisioner(
        uow_factory,
        operations=operations,
        git=git,
        database=database,
        operation_executor=_Executor(operations),
    )
    task = asyncio.create_task(
        provisioner.prepare(
            RUN_ID,
            _policy(enabled=True, repository_path=str(git.repository_path)),
        )
    )
    assert await asyncio.to_thread(git.started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    git.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    identity = WorktreeIdentity.for_run(
        PROJECT_ID,
        RUN_ID,
        "feature/e1-cancel-enabled",
        True,
    )
    expected = git.expected_worktree(identity, BASE_SHA)
    assert git.handle == expected
    assert uow_factory.runs.current.worktree_path == str(expected.path)
    assert uow_factory.runs.current.database_state is ResourceState.PROVISIONING
    assert uow_factory.runs.current.database_name == identity.database_name
    assert uow_factory.runs.current.database_role == identity.database_role
    assert uow_factory.runs.current.secret_id is None
    assert database.provision_calls == 0
    assert "operation.complete" not in log


def test_worktree_provisioner_api_is_available() -> None:
    from forge.application.ports.worktrees import WorktreeProvisionerPort
    from forge.tools.worktree import WorktreeProvisioner

    assert WorktreeProvisioner is not None
    assert WorktreeProvisionerPort is not None
