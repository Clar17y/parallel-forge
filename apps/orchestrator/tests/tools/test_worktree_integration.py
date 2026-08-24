"""PostgreSQL-backed integration coverage for durable worktree preparation."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self
from uuid import UUID

import pytest
from forge.application.ports.worktrees import DatabaseBinding, ManagedWorktree
from forge.application.services.recovery import OperationExecutor
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import DatabaseProvisioningPolicy, ProjectPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import OperationIntent as OperationIntentRecord
from forge.persistence.models import Run
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.worktree import WorktreeProvisioner, WorktreeReconciliationRequired
from sqlalchemy import select, update

BASE_SHA = "a" * 40


class _GitEffect:
    repository_path = Path.cwd()

    def __init__(self, *, block_create: bool = False) -> None:
        self.block_create = block_create
        self.create_started = threading.Event()
        self.create_release = threading.Event()
        self.create_calls = 0
        self.inspect_calls = 0
        self.handle: ManagedWorktree | None = None

    def expected_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        return ManagedWorktree(
            identity=identity,
            path=self.repository_path / ".worktrees" / identity.worktree_name,
            base_sha=base_sha,
        )

    def inspect_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree | None:
        del identity, base_sha
        self.inspect_calls += 1
        return self.handle

    def create_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        self.create_calls += 1
        self.create_started.set()
        if self.block_create and not self.create_release.wait(timeout=5):
            raise RuntimeError("test Git release was not signalled")
        self.handle = self.expected_worktree(identity, base_sha)
        return self.handle


class _DisabledDatabase:
    def validate_binding(
        self, identity: WorktreeIdentity, binding: DatabaseBinding
    ) -> DatabaseBinding:
        del identity, binding
        raise AssertionError("disabled preparation must not validate a database")

    async def verify_active(self, *args: Any, **kwargs: Any) -> UUID:
        raise AssertionError("disabled preparation must not verify a database")

    async def provision(self, *args: Any, **kwargs: Any) -> DatabaseBinding:
        raise AssertionError("disabled preparation must not provision a database")


class _DatabaseEffectAdapter:
    def __init__(
        self,
        owner: _PersistedDatabase,
        outcome: OperationOutcome,
    ) -> None:
        self._owner = owner
        self._outcome = outcome

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        del intent
        self._owner.effect_calls += 1
        return self._outcome

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        del intent
        return self._outcome


class _PersistedDatabase:
    def __init__(self, operations: Any, *, expected_arrivals: int = 1) -> None:
        self._operations = operations
        self._executor = OperationExecutor(operations, execution_lease_seconds=1)
        self._expected_arrivals = expected_arrivals
        self._all_arrived = asyncio.Event()
        self.arrivals = 0
        self.effect_calls = 0

    def _binding(self, identity: WorktreeIdentity) -> DatabaseBinding:
        if identity.run_id is None:
            raise AssertionError("integration database requires a persisted run")
        return DatabaseBinding(
            state=ResourceState.ACTIVE,
            database_name=identity.database_name,
            database_role=identity.database_role,
            secret_id=f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}",
        )

    def _request(self, identity: WorktreeIdentity, policy_version: int) -> OperationRequest:
        if identity.run_id is None:
            raise AssertionError("integration database requires a persisted run")
        binding = self._binding(identity)
        payload: dict[str, object] = {
            "project_id": str(identity.project_id),
            "run_id": str(identity.run_id),
            "policy_version": policy_version,
            "database_name": binding.database_name,
            "database_role": binding.database_role,
            "secret_id": binding.secret_id,
        }
        return OperationRequest(
            run_id=identity.run_id,
            kind="database.provision",
            idempotency_key=(
                f"integration-database-v1:{identity.project_id.hex}:"
                f"{identity.run_id.hex}:{policy_version}"
            ),
            request_digest=canonical_digest(payload),
            request_payload=payload,
        )

    def _outcome(self, identity: WorktreeIdentity) -> OperationOutcome:
        binding = self._binding(identity)
        return OperationOutcome(
            remote_resource_id=binding.database_name,
            payload={
                "database_state": ResourceState.ACTIVE.value,
                "database_name": binding.database_name,
                "database_role": binding.database_role,
                "secret_id": binding.secret_id,
            },
        )

    def validate_binding(
        self, identity: WorktreeIdentity, binding: DatabaseBinding
    ) -> DatabaseBinding:
        expected = self._binding(identity)
        if (
            binding.state
            not in {ResourceState.PROVISIONING, ResourceState.FAILED, ResourceState.ACTIVE}
            or binding.database_name != expected.database_name
            or binding.database_role != expected.database_role
            or (binding.secret_id is not None and binding.secret_id != expected.secret_id)
            or (binding.state is ResourceState.ACTIVE and binding.secret_id != expected.secret_id)
        ):
            raise AssertionError("database binding was not exact")
        return binding

    async def provision(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        del policy
        self.arrivals += 1
        if self.arrivals == self._expected_arrivals:
            self._all_arrived.set()
        await self._all_arrived.wait()
        request = self._request(identity, policy_version)
        outcome = self._outcome(identity)
        observed = await self._executor.execute(
            request,
            _DatabaseEffectAdapter(self, outcome),
        )
        if observed != outcome:
            raise AssertionError("database outcome was not exact")
        return self._binding(identity)

    async def verify_active(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int,
    ) -> UUID:
        del policy
        self.validate_binding(identity, resource)
        request = self._request(identity, policy_version)
        intent = await self._operations.get_by_idempotency_key(request.idempotency_key)
        if (
            intent is None
            or intent.status is not OperationStatus.SUCCEEDED
            or intent.to_outcome() != self._outcome(identity)
        ):
            raise AssertionError("database intent was not durably successful")
        return intent.id


class _FailingEventUnitOfWork(PostgresUnitOfWork):
    async def __aenter__(self) -> Self:
        await super().__aenter__()
        append = self.events.append

        async def append_then_fail(event: Any) -> Any:
            stored = await append(event)
            if event.event_type == "resource.worktree_preparing":
                raise RuntimeError("injected resource event failure")
            return stored

        self.events.append = append_then_fail  # type: ignore[method-assign]
        return self


async def _seed_preparing_run(
    session_factory: Any,
    persisted_run: RunSnapshot,
    *,
    branch: str,
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Run)
            .where(Run.id == persisted_run.id)
            .values(
                state=RunState.PREPARING_WORKTREE.value,
                branch_name=branch,
                base_ref="refs/heads/main",
                base_sha=BASE_SHA,
            )
        )


def _policy(run: RunSnapshot, git: _GitEffect, *, enabled: bool) -> ProjectPolicy:
    return ProjectPolicy(
        id=run.project_id,
        version=run.policy_version,
        repository_path=str(git.repository_path),
        github_repository=f"Clar17y/forge-{run.project_id}",
        default_branch="main",
        database=DatabaseProvisioningPolicy(
            enabled=enabled,
            admin_url_secret_reference="secret://admin/postgres" if enabled else None,
        ),
    )


async def _stored_state(session_factory: Any, run_id: UUID) -> tuple[Any, list[Any], list[Any]]:
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get(run_id)
        events = await work.events.list_after(run_id, 0)
    async with session_factory() as session:
        intents = list(
            (
                await session.execute(
                    select(OperationIntentRecord)
                    .where(OperationIntentRecord.run_id == run_id)
                    .order_by(OperationIntentRecord.created_at, OperationIntentRecord.id)
                )
            )
            .scalars()
            .all()
        )
    return run, events, intents


@pytest.mark.integration
async def test_postgres_commits_intent_and_partial_checkpoint_before_git(
    session_factory: Any,
    operation_repository: Any,
    persisted_run: RunSnapshot,
) -> None:
    branch = "feature/e1-postgres-ordering"
    await _seed_preparing_run(session_factory, persisted_run, branch=branch)
    git = _GitEffect(block_create=True)
    policy = _policy(persisted_run, git, enabled=False)
    provisioner = WorktreeProvisioner(
        lambda: PostgresUnitOfWork(session_factory),
        operations=operation_repository,
        git=git,
        database=_DisabledDatabase(),
    )

    task = asyncio.create_task(provisioner.prepare(persisted_run.id, policy))
    assert await asyncio.to_thread(git.create_started.wait, 2)
    during_run, during_events, during_intents = await _stored_state(
        session_factory, persisted_run.id
    )
    git.create_release.set()
    result = await task

    identity = WorktreeIdentity.for_run(
        persisted_run.project_id,
        persisted_run.id,
        branch,
        False,
    )
    expected = git.expected_worktree(identity, BASE_SHA)
    assert during_run.worktree_path == str(expected.path)
    assert during_run.database_state is ResourceState.DISABLED
    assert during_run.database_name is None
    assert during_run.database_role is None
    assert during_run.secret_id is None
    assert [event.event_type for event in during_events] == [
        "operation.intent_created",
        "resource.worktree_preparing",
    ]
    assert [(intent.operation_kind, intent.status) for intent in during_intents] == [
        ("worktree.create", "PENDING")
    ]
    assert result == expected

    final_run, final_events, final_intents = await _stored_state(session_factory, persisted_run.id)
    assert final_run.worktree_path == str(expected.path)
    assert [event.event_type for event in final_events] == [
        "operation.intent_created",
        "resource.worktree_preparing",
        "resource.worktree_created",
    ]
    assert [(intent.operation_kind, intent.status) for intent in final_intents] == [
        ("worktree.create", "SUCCEEDED")
    ]
    assert final_intents[0].remote_resource_id == identity.worktree_name
    assert branch not in repr(final_intents[0].request_payload)
    assert str(expected.path) not in repr(final_intents[0].request_payload)


@pytest.mark.integration
async def test_postgres_partial_event_failure_rolls_back_before_git(
    session_factory: Any,
    operation_repository: Any,
    persisted_run: RunSnapshot,
) -> None:
    branch = "feature/e1-postgres-rollback"
    await _seed_preparing_run(session_factory, persisted_run, branch=branch)
    git = _GitEffect()
    policy = _policy(persisted_run, git, enabled=False)
    factory: Callable[[], PostgresUnitOfWork] = lambda: _FailingEventUnitOfWork(session_factory)
    provisioner = WorktreeProvisioner(
        factory,
        operations=operation_repository,
        git=git,
        database=_DisabledDatabase(),
    )

    with pytest.raises(WorktreeReconciliationRequired):
        await provisioner.prepare(persisted_run.id, policy)

    stored_run, events, intents = await _stored_state(session_factory, persisted_run.id)
    assert stored_run.worktree_path is None
    assert stored_run.database_state is ResourceState.DISABLED
    assert stored_run.database_name is None
    assert stored_run.database_role is None
    assert stored_run.secret_id is None
    assert [event.event_type for event in events] == ["operation.intent_created"]
    assert [(intent.operation_kind, intent.status) for intent in intents] == [
        ("worktree.create", "NEEDS_RECONCILIATION")
    ]
    assert git.create_calls == 0


@pytest.mark.integration
async def test_postgres_concurrent_enabled_prepares_share_effects_and_converge(
    session_factory: Any,
    operation_repository: Any,
    persisted_run: RunSnapshot,
) -> None:
    branch = "feature/e1-postgres-concurrent"
    await _seed_preparing_run(session_factory, persisted_run, branch=branch)
    git = _GitEffect()
    database = _PersistedDatabase(operation_repository, expected_arrivals=2)
    policy = _policy(persisted_run, git, enabled=True)
    provisioner = WorktreeProvisioner(
        lambda: PostgresUnitOfWork(session_factory),
        operations=operation_repository,
        git=git,
        database=database,
    )

    first, second = await asyncio.gather(
        provisioner.prepare(persisted_run.id, policy),
        provisioner.prepare(persisted_run.id, policy),
    )

    identity = WorktreeIdentity.for_run(
        persisted_run.project_id,
        persisted_run.id,
        branch,
        True,
    )
    expected = git.expected_worktree(identity, BASE_SHA)
    assert first == second == expected
    assert git.create_calls == 1
    assert database.arrivals == 2
    assert database.effect_calls == 1

    stored_run, events, intents = await _stored_state(session_factory, persisted_run.id)
    assert stored_run.worktree_path == str(expected.path)
    assert stored_run.database_state is ResourceState.ACTIVE
    assert stored_run.database_name == identity.database_name
    assert stored_run.database_role == identity.database_role
    assert stored_run.secret_id == (f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}")
    assert [event.event_type for event in events] == [
        "operation.intent_created",
        "resource.worktree_preparing",
        "resource.worktree_created",
        "operation.intent_created",
        "resource.database_active",
    ]
    by_kind = {intent.operation_kind: intent for intent in intents}
    assert set(by_kind) == {"worktree.create", "database.provision"}
    assert all(intent.status == "SUCCEEDED" for intent in intents)
    active_events = [event for event in events if event.event_type == "resource.database_active"]
    assert len(active_events) == 1
    assert active_events[0].payload["operation_intent_id"] == str(by_kind["worktree.create"].id)
    assert active_events[0].payload["database_intent_id"] == str(by_kind["database.provision"].id)
    resource_events = [
        event
        for event in events
        if event.event_type.startswith("resource.worktree_")
        or event.event_type == "resource.database_active"
    ]
    assert [event.run_version for event in resource_events] == [1, 2, 3]
