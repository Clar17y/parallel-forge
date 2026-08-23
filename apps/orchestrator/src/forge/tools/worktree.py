"""Durable persisted-run worktree preparation boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from forge.application.ports.operations import OperationAdapter, OperationRepository
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import (
    ControlledGitPort,
    DatabaseBinding,
    DatabaseProvisionerPort,
    ManagedWorktree,
)
from forge.application.services.recovery import OperationExecutor, RecoveryService
from forge.domain.event import RunEvent
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.tools.runner import await_deferred_cancellation

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PROTOCOL_VERSION = 1
_WORKTREE_KIND = "worktree.create"
_PARTIAL_EVENT = "resource.worktree_preparing"
_CREATED_EVENT = "resource.worktree_created"
_RECONCILED_EVENT = "resource.worktree_reconciled"
_DATABASE_ACTIVE_EVENT = "resource.database_active"
_DATABASE_RETRY_EVENT = "resource.database_provisioning"
_FAILED_EVENT = "resource.worktree_failed"
_ERROR = "worktree operation failed"
_INTEGRITY_ERROR = "worktree resource identity is invalid"
_RECONCILIATION_ERROR = "worktree resource requires reconciliation"
_CHECKPOINT_KEYS = frozenset(
    {
        "operation_intent_id",
        "project_id",
        "run_id",
        "policy_version",
        "branch_digest",
        "worktree_name",
        "base_sha",
        "database_state",
    }
)
_ACTIVE_CHECKPOINT_KEYS = _CHECKPOINT_KEYS | {"database_intent_id"}


class WorktreeProvisionerError(RuntimeError):
    """A stable, redacted worktree lifecycle failure."""

    def __init__(self, message: str = _ERROR) -> None:
        super().__init__(message)


class WorktreeIntegrityError(WorktreeProvisionerError):
    """A caller, policy, persisted row, or durable intent is not exact."""

    def __init__(self) -> None:
        super().__init__(_INTEGRITY_ERROR)


class WorktreeReconciliationRequired(WorktreeProvisionerError):
    """The observed local/durable state cannot be safely advanced."""

    def __init__(self) -> None:
        super().__init__(_RECONCILIATION_ERROR)


class _UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class _Executor(Protocol):
    async def execute(
        self, request: OperationRequest, adapter: OperationAdapter
    ) -> OperationOutcome: ...


@dataclass(frozen=True, slots=True)
class _EventCheckpoint:
    event_type: str
    operation_intent_id: UUID
    database_intent_id: UUID | None
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _Context:
    run: RunSnapshot
    policy: ProjectPolicy
    identity: WorktreeIdentity
    expected: ManagedWorktree
    request: OperationRequest
    events: tuple[_EventCheckpoint, ...]
    partial: _EventCheckpoint | None
    completed: _EventCheckpoint | None
    active: _EventCheckpoint | None
    operation: OperationIntent | None

    @property
    def operation_id(self) -> UUID:
        checkpoint = self.completed or self.partial
        if checkpoint is None:
            raise WorktreeReconciliationRequired()
        return checkpoint.operation_intent_id


class WorktreeProvisioner:
    """Prepare exactly one durable run-scoped worktree and optional database."""

    def __init__(
        self,
        unit_of_work_factory: _UnitOfWorkFactory,
        *,
        operations: OperationRepository | None = None,
        git: ControlledGitPort | None = None,
        database: DatabaseProvisionerPort | None = None,
        operation_executor: _Executor | None = None,
        recovery_service: RecoveryService | None = None,
        controlled_git: ControlledGitPort | None = None,
        database_provisioner: DatabaseProvisionerPort | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        selected_git = git or controlled_git
        selected_database = database or database_provisioner
        if selected_git is None or selected_database is None:
            raise TypeError("worktree provisioner requires Git and database boundaries")
        self._git: ControlledGitPort = selected_git
        self._database: DatabaseProvisionerPort = selected_database
        if operations is None and operation_executor is not None:
            operations = getattr(operation_executor, "_operations", None)
        if operations is None:
            raise TypeError("worktree provisioner requires an operation repository")
        self._operations = operations
        self._operation_executor = operation_executor or OperationExecutor(operations)
        self._recovery = recovery_service or RecoveryService(operations)

    async def prepare(self, run_id: UUID, policy: ProjectPolicy) -> ManagedWorktree:
        """Prepare one exact managed resource through durable checkpoints."""

        _validate_public_inputs(run_id, policy)
        context = await self._load_context(run_id, policy)
        if context.run.database_state is ResourceState.ACTIVE:
            await self._verify_active_context(context)

        adapter = _WorktreeAdapter(self, policy)
        try:
            outcome = await self._operation_executor.execute(context.request, adapter)
        except asyncio.CancelledError:
            raise
        except WorktreeProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - executor failures become a stable public category
            raise WorktreeReconciliationRequired() from None
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise WorktreeReconciliationRequired()
        if not _outcome_matches(outcome, context.request, context.identity):
            raise WorktreeReconciliationRequired()

        inspected = await self._inspect_present(context.expected)
        latest = await self._load_context(run_id, policy, require_succeeded=True)
        if latest.completed is None or not _same_handle(inspected, latest.expected):
            raise WorktreeReconciliationRequired()

        if not policy.database.enabled:
            if latest.run.database_state is not ResourceState.DISABLED:
                raise WorktreeReconciliationRequired()
            return inspected

        if latest.run.database_state is ResourceState.ACTIVE:
            await self._verify_active_context(latest)
            return inspected
        if latest.run.database_state not in {ResourceState.PROVISIONING, ResourceState.FAILED}:
            raise WorktreeReconciliationRequired()
        return await self._prepare_database(latest, inspected)

    async def reconcile(self, intent_id: UUID, policy: ProjectPolicy) -> OperationIntent:
        """Reconcile one claimed worktree intent by inspection only."""

        _validate_public_inputs(intent_id, policy)
        adapter = _WorktreeAdapter(self, policy, reconcile_only=True)
        try:
            return await self._recovery.reconcile(intent_id, adapter)
        except asyncio.CancelledError:
            raise
        except WorktreeProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - recovery failures become a stable public category
            raise WorktreeReconciliationRequired() from None

    async def _load_context(
        self,
        run_id: UUID,
        policy: ProjectPolicy,
        *,
        require_succeeded: bool = False,
        expected_intent: OperationIntent | None = None,
    ) -> _Context:
        try:
            async with self._unit_of_work_factory() as work:
                run = await work.runs.get(run_id)
                raw_events = await work.events.list_after(run_id, 0)
        except WorktreeProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - persistence failures become a stable integrity category
            raise WorktreeIntegrityError() from None

        _validate_run_and_policy(run, policy, self._git.repository_path)
        identity = _identity_for_run(run, policy)
        try:
            expected = self._git.expected_worktree(identity, _require_sha(run.base_sha))
        except Exception:  # noqa: BLE001 - Git boundary failures expose no raw diagnostics
            raise WorktreeIntegrityError() from None
        _validate_current_resource(run, policy, identity, expected, self._database)
        request = _request(run, identity, policy)
        checkpoints = _parse_checkpoints(raw_events, run, request)
        operation = await self._operation_for_request(request)
        if expected_intent is not None:
            _validate_intent(expected_intent, request)
            if operation is None or operation != expected_intent:
                raise WorktreeReconciliationRequired()
        if checkpoints and operation is None:
            # A checkpoint without its authoritative intent is never enough to
            # adopt or advance a resource.
            raise WorktreeReconciliationRequired()
        if checkpoints and operation is not None:
            for checkpoint in checkpoints:
                if checkpoint.operation_intent_id != operation.id:
                    raise WorktreeReconciliationRequired()
        if require_succeeded:
            if operation is None or operation.status is not OperationStatus.SUCCEEDED:
                raise WorktreeReconciliationRequired()
            _validate_succeeded_intent(operation, request, identity)

        partial = _one_checkpoint(checkpoints, _PARTIAL_EVENT)
        completed = _one_completed(checkpoints)
        active = _one_checkpoint(checkpoints, _DATABASE_ACTIVE_EVENT)
        _validate_checkpoint_shape(run, policy, request, partial, completed, active)
        return _Context(
            run=run,
            policy=policy,
            identity=identity,
            expected=expected,
            request=request,
            events=tuple(checkpoints),
            partial=partial,
            completed=completed,
            active=active,
            operation=operation,
        )

    async def _operation_for_request(self, request: OperationRequest) -> OperationIntent | None:
        try:
            return await self._operations.get_by_idempotency_key(request.idempotency_key)
        except Exception:  # noqa: BLE001 - lookup failures expose no persistence diagnostics
            raise WorktreeIntegrityError() from None

    async def _verify_active_context(self, context: _Context) -> None:
        active = context.active
        if active is None or active.database_intent_id is None:
            raise WorktreeReconciliationRequired()
        resource = DatabaseBinding(
            state=ResourceState.ACTIVE,
            database_name=context.run.database_name,
            database_role=context.run.database_role,
            secret_id=context.run.secret_id,
        )
        try:
            self._database.validate_binding(context.identity, resource)
            verified = await self._database.verify_active(
                context.identity,
                context.policy.database,
                resource,
                policy_version=_require_policy_version(context.run),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - database verification errors are reconciliation-only
            raise WorktreeReconciliationRequired() from None
        if verified != active.database_intent_id:
            raise WorktreeReconciliationRequired()

    async def _prepare_database(
        self, context: _Context, worktree: ManagedWorktree
    ) -> ManagedWorktree:
        current = context
        if current.run.database_state is ResourceState.FAILED:
            current = await self._record_resource(
                current,
                database_state=ResourceState.PROVISIONING,
                event_type=_DATABASE_RETRY_EVENT,
                event_payload=_checkpoint_payload(
                    current.request,
                    current.operation_id,
                    target_state=ResourceState.PROVISIONING,
                ),
            )
        binding: DatabaseBinding | None = None
        validated_binding: DatabaseBinding | None = None
        try:
            binding = await self._database.provision(
                current.identity,
                current.policy.database,
                policy_version=_require_policy_version(current.run),
            )
            validated_binding = self._database.validate_binding(current.identity, binding)
            database_intent_id = await self._database.verify_active(
                current.identity,
                current.policy.database,
                validated_binding,
                policy_version=_require_policy_version(current.run),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - database failures retain only validated state
            await self._record_failure(current, validated_binding)
            raise WorktreeReconciliationRequired() from None

        active_payload = _checkpoint_payload(
            current.request,
            current.operation_id,
            target_state=ResourceState.ACTIVE,
            database_intent_id=database_intent_id,
        )
        try:
            active_context = await self._record_resource(
                current,
                database_state=ResourceState.ACTIVE,
                database_name=validated_binding.database_name,
                database_role=validated_binding.database_role,
                secret_id=validated_binding.secret_id,
                event_type=_DATABASE_ACTIVE_EVENT,
                event_payload=active_payload,
            )
        except Exception:  # noqa: BLE001 - checkpoint failures retain a safe partial state
            await self._record_failure(current, validated_binding)
            raise WorktreeReconciliationRequired() from None
        await self._verify_active_context(active_context)
        return worktree

    async def _record_failure(
        self, context: _Context, binding: DatabaseBinding | None = None
    ) -> None:
        if not context.policy.database.enabled:
            state = ResourceState.DISABLED
            name = role = secret = None
        else:
            state = ResourceState.FAILED
            name = binding.database_name if binding is not None else context.run.database_name
            role = binding.database_role if binding is not None else context.run.database_role
            secret = binding.secret_id if binding is not None else context.run.secret_id
        with suppress(Exception):
            await self._record_resource(
                context,
                database_state=state,
                database_name=name,
                database_role=role,
                secret_id=secret,
                event_type=_FAILED_EVENT,
                event_payload=_checkpoint_payload(
                    context.request,
                    context.operation_id,
                    target_state=state,
                ),
            )

    async def _record_resource(
        self,
        context: _Context,
        *,
        database_state: ResourceState,
        worktree_path: str | None | object = _ERROR,
        database_name: str | None | object = _ERROR,
        database_role: str | None | object = _ERROR,
        secret_id: str | None | object = _ERROR,
        event_type: str,
        event_payload: Mapping[str, object],
    ) -> _Context:
        path = context.run.worktree_path if worktree_path is _ERROR else worktree_path
        name = context.run.database_name if database_name is _ERROR else database_name
        role = context.run.database_role if database_role is _ERROR else database_role
        secret = context.run.secret_id if secret_id is _ERROR else secret_id
        _validate_record_identity(context, path, database_state, name, role, secret)
        _validate_resource_values(context.policy, path, database_state, name, role, secret)
        _validate_event_payload(context, event_type, event_payload)
        try:
            async with self._unit_of_work_factory() as work:
                current = await work.runs.get_for_update(context.run.id)
                if current.version != context.run.version:
                    raise WorktreeReconciliationRequired()
                await work.runs.update_resource(
                    current.id,
                    context.run.version,
                    worktree_path=cast_str_or_none(path),
                    database_state=database_state,
                    database_name=cast_str_or_none(name),
                    database_role=cast_str_or_none(role),
                    secret_id=cast_str_or_none(secret),
                    event_type=event_type,
                    event_payload=event_payload,
                )
                await work.commit()
        except WorktreeProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - transaction failures expose no persistence details
            raise WorktreeReconciliationRequired() from None
        return await self._load_context(context.run.id, context.policy)

    async def _inspect_present(self, expected: ManagedWorktree) -> ManagedWorktree:
        try:
            inspected, cancelled = await await_deferred_cancellation(
                asyncio.to_thread(
                    self._git.inspect_worktree,
                    expected.identity,
                    expected.base_sha,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - inspection failures expose no Git diagnostics
            raise WorktreeReconciliationRequired() from None
        if inspected is None or not _same_handle(inspected, expected):
            raise WorktreeReconciliationRequired()
        if cancelled:
            raise asyncio.CancelledError()
        return inspected

    async def _inspect_any(self, expected: ManagedWorktree) -> tuple[ManagedWorktree | None, bool]:
        try:
            result, cancelled = await await_deferred_cancellation(
                asyncio.to_thread(
                    self._git.inspect_worktree,
                    expected.identity,
                    expected.base_sha,
                )
            )
            if result is not None and not _same_handle(result, expected):
                raise WorktreeReconciliationRequired()
            return result, cancelled
        except WorktreeProvisionerError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - inspection failures expose no Git diagnostics
            raise WorktreeReconciliationRequired() from None

    async def _create_worktree(
        self, expected: ManagedWorktree
    ) -> tuple[ManagedWorktree | None, bool, BaseException | None]:
        try:
            result, cancelled = await await_deferred_cancellation(
                asyncio.to_thread(
                    self._git.create_worktree,
                    expected.identity,
                    expected.base_sha,
                )
            )
            if not _same_handle(result, expected):
                return None, cancelled, WorktreeReconciliationRequired()
            return result, cancelled, None
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - terminal thread failure is inspected safely
            return None, False, error


class _WorktreeAdapter:
    """Operation adapter for one exact worktree request.

    The adapter is deliberately small: all durable state validation happens by
    reloading the run and its events, while Git is the only effect boundary it
    is allowed to invoke.  ``reconcile_only`` is used by ``RecoveryService``
    and therefore never calls the Git creation method.
    """

    def __init__(
        self,
        owner: WorktreeProvisioner,
        policy: ProjectPolicy,
        *,
        reconcile_only: bool = False,
    ) -> None:
        self._owner = owner
        self._policy = policy
        self._reconcile_only = reconcile_only

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        if self._reconcile_only:
            return _needs_outcome()
        context = await self._owner._load_context(
            intent.run_id,
            self._policy,
            expected_intent=intent,
        )
        _validate_adapter_intent(intent, context.request)
        if context.operation is None or context.operation.id != intent.id:
            raise WorktreeReconciliationRequired()
        if context.completed is not None or context.partial is not None:
            # A newly-owned intent cannot safely repeat a checkpointed effect.
            raise WorktreeReconciliationRequired()

        enabled = self._policy.database.enabled
        partial_state = ResourceState.PROVISIONING if enabled else ResourceState.DISABLED
        partial = await self._owner._record_resource(
            context,
            worktree_path=str(context.expected.path),
            database_state=partial_state,
            database_name=context.identity.database_name if enabled else None,
            database_role=context.identity.database_role if enabled else None,
            secret_id=None,
            event_type=_PARTIAL_EVENT,
            event_payload=_checkpoint_payload(
                context.request,
                intent.id,
                target_state=partial_state,
            ),
        )

        present, inspection_cancelled = await self._owner._inspect_any(partial.expected)
        if present is not None:
            # The path was reserved but was already present before this owner
            # reached Git.  Adoption belongs to inspection-only recovery.
            if inspection_cancelled:
                raise asyncio.CancelledError()
            await self._owner._record_failure(partial)
            raise WorktreeReconciliationRequired()
        if inspection_cancelled:
            raise asyncio.CancelledError()

        created, create_cancelled, create_error = await self._owner._create_worktree(
            partial.expected
        )
        if create_error is not None:
            observed, observed_cancelled = await self._owner._inspect_any(partial.expected)
            if observed is not None:
                try:
                    created_context = await self._record_created(partial)
                except Exception:  # noqa: BLE001 - preserve safe partial state after Git
                    await self._owner._record_failure(partial)
                    raise WorktreeReconciliationRequired() from None
                del created_context
            else:
                await self._owner._record_failure(partial)
            if observed_cancelled:
                raise asyncio.CancelledError()
            raise WorktreeReconciliationRequired() from None

        if created is None or not _same_handle(created, partial.expected):
            await self._owner._record_failure(partial)
            raise WorktreeReconciliationRequired()

        inspected, inspect_cancelled = await self._owner._inspect_any(partial.expected)
        if inspected is None:
            await self._owner._record_failure(partial)
            raise WorktreeReconciliationRequired()
        try:
            await self._record_created(partial)
        except Exception:  # noqa: BLE001 - preserve reconciliation after checkpoint failure
            raise WorktreeReconciliationRequired() from None
        if create_cancelled or inspect_cancelled:
            raise asyncio.CancelledError()
        return _worktree_outcome(context.request, context.identity)

    async def _record_created(self, context: _Context) -> _Context:
        state = (
            ResourceState.PROVISIONING if self._policy.database.enabled else ResourceState.DISABLED
        )
        return await self._owner._record_resource(
            context,
            worktree_path=str(context.expected.path),
            database_state=state,
            database_name=context.identity.database_name if self._policy.database.enabled else None,
            database_role=context.identity.database_role if self._policy.database.enabled else None,
            secret_id=context.run.secret_id if self._policy.database.enabled else None,
            event_type=_CREATED_EVENT,
            event_payload=_checkpoint_payload(
                context.request,
                context.operation_id,
                target_state=state,
            ),
        )

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        context = await self._owner._load_context(
            intent.run_id,
            self._policy,
            expected_intent=intent,
        )
        _validate_adapter_intent(intent, context.request)
        if context.operation is None or context.operation.id != intent.id:
            raise WorktreeReconciliationRequired()
        if context.run.database_state is ResourceState.ACTIVE:
            await self._owner._verify_active_context(context)
        if context.partial is None:
            return _needs_outcome()

        inspected, inspection_cancelled = await self._owner._inspect_any(context.expected)
        if inspected is None:
            return _needs_outcome()

        if context.completed is not None:
            if inspection_cancelled:
                raise asyncio.CancelledError()
            return _worktree_outcome(context.request, context.identity)

        if inspection_cancelled:
            # Inspection has proved a handle but cancellation must be allowed
            # to propagate after its causal checkpoint is durable.
            pass
        state = (
            ResourceState.PROVISIONING if self._policy.database.enabled else ResourceState.DISABLED
        )
        reconciled = await self._owner._record_resource(
            context,
            worktree_path=str(context.expected.path),
            database_state=state,
            database_name=context.identity.database_name if self._policy.database.enabled else None,
            database_role=context.identity.database_role if self._policy.database.enabled else None,
            secret_id=context.run.secret_id if self._policy.database.enabled else None,
            event_type=_RECONCILED_EVENT,
            event_payload=_checkpoint_payload(
                context.request,
                intent.id,
                target_state=state,
            ),
        )
        del reconciled
        if inspection_cancelled:
            raise asyncio.CancelledError()
        return _worktree_outcome(context.request, context.identity)


def _validate_public_inputs(value: object, policy: object) -> None:
    if not isinstance(value, UUID) or not isinstance(policy, ProjectPolicy):
        raise WorktreeIntegrityError()


def _validate_run_and_policy(
    run: RunSnapshot,
    policy: ProjectPolicy,
    repository_path: Path,
) -> None:
    try:
        if not isinstance(run, RunSnapshot):
            raise WorktreeIntegrityError()
        if run.state is not RunState.PREPARING_WORKTREE:
            raise WorktreeIntegrityError()
        if run.project_id != policy.id or run.policy_version != policy.version:
            raise WorktreeIntegrityError()
        if run.branch_name is None or not run.branch_name.strip():
            raise WorktreeIntegrityError()
        if run.base_ref is None or not run.base_ref.strip():
            raise WorktreeIntegrityError()
        if _SHA.fullmatch(run.base_sha or "") is None:
            raise WorktreeIntegrityError()
        if not isinstance(repository_path, Path) or policy.repository_path != str(repository_path):
            raise WorktreeIntegrityError()
    except WorktreeProvisionerError:
        raise
    except OSError, TypeError, ValueError:
        raise WorktreeIntegrityError() from None


def _identity_for_run(run: RunSnapshot, policy: ProjectPolicy) -> WorktreeIdentity:
    try:
        branch = run.branch_name
        if branch is None:
            raise ValueError
        return WorktreeIdentity.for_run(run.project_id, run.id, branch, policy.database.enabled)
    except TypeError, ValueError:
        raise WorktreeIntegrityError() from None


def _require_sha(value: str | None) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise WorktreeIntegrityError()
    return value


def _require_policy_version(run: RunSnapshot) -> int:
    if type(run.policy_version) is not int or run.policy_version < 1:
        raise WorktreeIntegrityError()
    return run.policy_version


def _validate_current_resource(
    run: RunSnapshot,
    policy: ProjectPolicy,
    identity: WorktreeIdentity,
    expected: ManagedWorktree,
    database: DatabaseProvisionerPort,
) -> None:
    path = run.worktree_path
    if path is not None:
        try:
            if not isinstance(path, str) or Path(path) != expected.path:
                raise WorktreeIntegrityError()
            if not Path(path).is_absolute() or any(
                part in {".", ".."} for part in Path(path).parts[1:]
            ):
                raise WorktreeIntegrityError()
        except WorktreeProvisionerError:
            raise
        except TypeError, ValueError:
            raise WorktreeIntegrityError() from None

    if not policy.database.enabled:
        if run.database_state is not ResourceState.DISABLED or any(
            value is not None for value in (run.database_name, run.database_role, run.secret_id)
        ):
            raise WorktreeIntegrityError()
        if path is not None and path != str(expected.path):
            raise WorktreeIntegrityError()
        return

    if run.database_state is ResourceState.REMOVED:
        raise WorktreeIntegrityError()
    if run.database_state is ResourceState.DISABLED:
        if path is not None or any(
            value is not None for value in (run.database_name, run.database_role, run.secret_id)
        ):
            raise WorktreeIntegrityError()
        return
    if path is None:
        raise WorktreeIntegrityError()
    for actual, wanted in (
        (run.database_name, identity.database_name),
        (run.database_role, identity.database_role),
    ):
        if actual is not None and actual != wanted:
            raise WorktreeIntegrityError()
    expected_secret = _expected_secret_id(identity)
    if run.secret_id is not None and run.secret_id != expected_secret:
        raise WorktreeIntegrityError()
    if run.database_state is ResourceState.PROVISIONING and (
        run.database_name != identity.database_name or run.database_role != identity.database_role
    ):
        raise WorktreeIntegrityError()
    if run.database_state is ResourceState.ACTIVE and (
        run.database_name != identity.database_name
        or run.database_role != identity.database_role
        or run.secret_id != expected_secret
    ):
        raise WorktreeIntegrityError()
    if run.database_state not in {
        ResourceState.PROVISIONING,
        ResourceState.FAILED,
        ResourceState.ACTIVE,
    }:
        raise WorktreeIntegrityError()
    try:
        database.validate_binding(
            identity,
            DatabaseBinding(
                state=run.database_state,
                database_name=run.database_name,
                database_role=run.database_role,
                secret_id=run.secret_id,
            ),
        )
    except WorktreeProvisionerError:
        raise WorktreeIntegrityError() from None
    except Exception:  # noqa: BLE001 - injected validators expose no raw diagnostics
        raise WorktreeIntegrityError() from None


def _request(
    run: RunSnapshot,
    identity: WorktreeIdentity,
    policy: ProjectPolicy,
) -> OperationRequest:
    payload: dict[str, object] = {
        "project_id": str(run.project_id),
        "run_id": str(run.id),
        "policy_version": policy.version,
        "branch_digest": hashlib.sha256(identity.branch.encode("utf-8")).hexdigest(),
        "worktree_name": identity.worktree_name,
        "base_sha": _require_sha(run.base_sha),
        "database_state": (
            ResourceState.ACTIVE.value if policy.database.enabled else ResourceState.DISABLED.value
        ),
    }
    return OperationRequest(
        run_id=run.id,
        kind=_WORKTREE_KIND,
        idempotency_key=(
            f"forge-worktree-v{_PROTOCOL_VERSION}:{_WORKTREE_KIND}:"
            f"{run.project_id.hex}:{run.id.hex}:{policy.version}"
        ),
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )


def _checkpoint_payload(
    request: OperationRequest,
    operation_intent_id: UUID,
    *,
    target_state: ResourceState,
    database_intent_id: UUID | None = None,
) -> Mapping[str, object]:
    if not isinstance(operation_intent_id, UUID) or not isinstance(target_state, ResourceState):
        raise WorktreeIntegrityError()
    source = request.request_payload
    payload: dict[str, object] = {
        "operation_intent_id": str(operation_intent_id),
        "project_id": source["project_id"],
        "run_id": source["run_id"],
        "policy_version": source["policy_version"],
        "branch_digest": source["branch_digest"],
        "worktree_name": source["worktree_name"],
        "base_sha": source["base_sha"],
        "database_state": target_state.value,
    }
    if database_intent_id is not None:
        if not isinstance(database_intent_id, UUID):
            raise WorktreeIntegrityError()
        payload["database_intent_id"] = str(database_intent_id)
    return payload


def _parse_checkpoints(
    raw_events: Sequence[object],
    run: RunSnapshot,
    request: OperationRequest,
) -> tuple[_EventCheckpoint, ...]:
    relevant = {
        _PARTIAL_EVENT,
        _CREATED_EVENT,
        _RECONCILED_EVENT,
        _DATABASE_ACTIVE_EVENT,
        _DATABASE_RETRY_EVENT,
        _FAILED_EVENT,
    }
    parsed: list[_EventCheckpoint] = []
    for event in raw_events:
        if not isinstance(event, RunEvent):
            raise WorktreeIntegrityError()
        if event.event_type not in relevant:
            continue
        if event.run_id != run.id or not isinstance(event.payload, Mapping):
            raise WorktreeIntegrityError()
        payload = event.payload
        operation_id = _parse_uuid_field(payload.get("operation_intent_id"))
        if operation_id is None:
            raise WorktreeReconciliationRequired()
        expected_keys = (
            _ACTIVE_CHECKPOINT_KEYS
            if event.event_type == _DATABASE_ACTIVE_EVENT
            else _CHECKPOINT_KEYS
        )
        if set(payload) != expected_keys:
            raise WorktreeIntegrityError()
        if (
            payload.get("project_id") != str(run.project_id)
            or payload.get("run_id") != str(run.id)
            or payload.get("policy_version") != request.request_payload.get("policy_version")
            or payload.get("branch_digest") != request.request_payload.get("branch_digest")
            or payload.get("worktree_name") != request.request_payload.get("worktree_name")
            or payload.get("base_sha") != request.request_payload.get("base_sha")
        ):
            raise WorktreeReconciliationRequired()
        state = payload.get("database_state")
        if not isinstance(state, str):
            raise WorktreeIntegrityError()
        try:
            ResourceState(state)
        except ValueError:
            raise WorktreeIntegrityError() from None
        database_id = None
        if event.event_type == _DATABASE_ACTIVE_EVENT:
            database_id = _parse_uuid_field(payload.get("database_intent_id"))
            if database_id is None:
                raise WorktreeReconciliationRequired()
        parsed.append(
            _EventCheckpoint(
                event_type=event.event_type,
                operation_intent_id=operation_id,
                database_intent_id=database_id,
                payload=payload,
            )
        )
    return tuple(parsed)


def _parse_uuid_field(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def _one_checkpoint(
    checkpoints: Sequence[_EventCheckpoint], event_type: str
) -> _EventCheckpoint | None:
    matches = [checkpoint for checkpoint in checkpoints if checkpoint.event_type == event_type]
    if len(matches) > 1:
        raise WorktreeReconciliationRequired()
    return matches[0] if matches else None


def _one_completed(checkpoints: Sequence[_EventCheckpoint]) -> _EventCheckpoint | None:
    matches = [
        checkpoint
        for checkpoint in checkpoints
        if checkpoint.event_type in {_CREATED_EVENT, _RECONCILED_EVENT}
    ]
    if len(matches) > 1:
        raise WorktreeReconciliationRequired()
    return matches[0] if matches else None


def _checkpoint_database_state(checkpoint: _EventCheckpoint) -> ResourceState:
    value = checkpoint.payload.get("database_state")
    if not isinstance(value, str):
        raise WorktreeIntegrityError()
    try:
        return ResourceState(value)
    except ValueError:
        raise WorktreeIntegrityError() from None


def _validate_checkpoint_shape(
    run: RunSnapshot,
    policy: ProjectPolicy,
    request: OperationRequest,
    partial: _EventCheckpoint | None,
    completed: _EventCheckpoint | None,
    active: _EventCheckpoint | None,
) -> None:
    del request
    partial_state = (
        ResourceState.PROVISIONING if policy.database.enabled else ResourceState.DISABLED
    )
    if partial is not None and _checkpoint_database_state(partial) is not partial_state:
        raise WorktreeReconciliationRequired()
    if completed is not None and _checkpoint_database_state(completed) is not partial_state:
        raise WorktreeReconciliationRequired()
    if active is not None:
        if not policy.database.enabled or completed is None:
            raise WorktreeReconciliationRequired()
        if _checkpoint_database_state(active) is not ResourceState.ACTIVE:
            raise WorktreeReconciliationRequired()
    if run.worktree_path is None and any(
        checkpoint is not None for checkpoint in (partial, completed, active)
    ):
        raise WorktreeReconciliationRequired()
    if run.worktree_path is not None and partial is None:
        raise WorktreeReconciliationRequired()
    if completed is not None and partial is None:
        raise WorktreeReconciliationRequired()
    if active is not None and run.database_state is not ResourceState.ACTIVE:
        raise WorktreeReconciliationRequired()
    if run.database_state is ResourceState.ACTIVE and active is None:
        raise WorktreeReconciliationRequired()
    if not policy.database.enabled and active is not None:
        raise WorktreeReconciliationRequired()


def _validate_intent(intent: OperationIntent, request: OperationRequest) -> None:
    if not isinstance(intent, OperationIntent):
        raise WorktreeIntegrityError()
    if (
        intent.run_id != request.run_id
        or intent.kind != request.kind
        or intent.idempotency_key != request.idempotency_key
        or intent.request_digest != request.request_digest
        or intent.request_schema_version != request.request_schema_version
        or canonical_digest(intent.request_payload) != canonical_digest(request.request_payload)
    ):
        raise WorktreeReconciliationRequired()


def _validate_adapter_intent(intent: OperationIntent, request: OperationRequest) -> None:
    _validate_intent(intent, request)
    if intent.status not in {
        OperationStatus.PENDING,
        OperationStatus.NEEDS_RECONCILIATION,
    }:
        raise WorktreeReconciliationRequired()


def _validate_succeeded_intent(
    intent: OperationIntent,
    request: OperationRequest,
    identity: WorktreeIdentity,
) -> None:
    if intent.status is not OperationStatus.SUCCEEDED:
        raise WorktreeReconciliationRequired()
    _validate_intent(intent, request)
    if intent.outcome is None:
        raise WorktreeReconciliationRequired()
    outcome = intent.to_outcome()
    if not _outcome_matches(outcome, request, identity):
        raise WorktreeReconciliationRequired()


def _worktree_outcome(request: OperationRequest, identity: WorktreeIdentity) -> OperationOutcome:
    target_state = request.request_payload.get("database_state")
    if not isinstance(target_state, str):
        raise WorktreeIntegrityError()
    return OperationOutcome(
        status=OperationStatus.SUCCEEDED,
        remote_resource_id=identity.worktree_name,
        payload={
            "worktree_name": identity.worktree_name,
            "base_sha": request.request_payload.get("base_sha"),
            "database_state": target_state,
        },
    )


def _needs_outcome() -> OperationOutcome:
    return OperationOutcome(
        status=OperationStatus.NEEDS_RECONCILIATION,
        error=_RECONCILIATION_ERROR,
    )


def _outcome_matches(
    outcome: OperationOutcome,
    request: OperationRequest,
    identity: WorktreeIdentity,
) -> bool:
    if outcome.status is not OperationStatus.SUCCEEDED:
        return False
    expected = _worktree_outcome(request, identity)
    return (
        outcome.remote_resource_id == expected.remote_resource_id
        and outcome.outcome_schema_version == expected.outcome_schema_version
        and canonical_digest(outcome.payload) == canonical_digest(expected.payload)
    )


def _same_handle(left: ManagedWorktree | None, right: ManagedWorktree | None) -> bool:
    return (
        isinstance(left, ManagedWorktree)
        and isinstance(right, ManagedWorktree)
        and left.identity == right.identity
        and left.path == right.path
        and left.base_sha == right.base_sha
    )


def _validate_resource_values(
    policy: ProjectPolicy,
    worktree_path: str | None | object,
    database_state: ResourceState,
    database_name: str | None | object,
    database_role: str | None | object,
    secret_id: str | None | object,
) -> None:
    if not isinstance(database_state, ResourceState):
        raise WorktreeIntegrityError()
    for value in (worktree_path, database_name, database_role, secret_id):
        if value is not None and value is not _ERROR and not isinstance(value, str):
            raise WorktreeIntegrityError()
    if not policy.database.enabled:
        if database_state is not ResourceState.DISABLED or any(
            value is not None for value in (database_name, database_role, secret_id)
        ):
            raise WorktreeIntegrityError()
        return
    if database_state in {ResourceState.DISABLED, ResourceState.REMOVED} and any(
        value is not None for value in (database_name, database_role, secret_id)
    ):
        raise WorktreeIntegrityError()
    if database_state is ResourceState.ACTIVE and any(
        value is None for value in (database_name, database_role, secret_id)
    ):
        raise WorktreeIntegrityError()
    if database_state not in {
        ResourceState.DISABLED,
        ResourceState.PROVISIONING,
        ResourceState.ACTIVE,
        ResourceState.FAILED,
    }:
        raise WorktreeIntegrityError()


def _validate_record_identity(
    context: _Context,
    worktree_path: str | None | object,
    database_state: ResourceState,
    database_name: str | None | object,
    database_role: str | None | object,
    secret_id: str | None | object,
) -> None:
    if worktree_path is not None and (
        not isinstance(worktree_path, str) or Path(worktree_path) != context.expected.path
    ):
        raise WorktreeIntegrityError()
    if not context.policy.database.enabled:
        if database_state is not ResourceState.DISABLED or any(
            value is not None for value in (database_name, database_role, secret_id)
        ):
            raise WorktreeIntegrityError()
        return
    expected_name = context.identity.database_name
    expected_role = context.identity.database_role
    expected_secret = _expected_secret_id(context.identity)
    for actual, expected in (
        (database_name, expected_name),
        (database_role, expected_role),
        (secret_id, expected_secret),
    ):
        if actual is not None and actual is not _ERROR and actual != expected:
            raise WorktreeIntegrityError()
    if database_state is ResourceState.PROVISIONING and (
        database_name != expected_name or database_role != expected_role
    ):
        raise WorktreeIntegrityError()
    if database_state is ResourceState.ACTIVE and (
        database_name != expected_name
        or database_role != expected_role
        or secret_id != expected_secret
    ):
        raise WorktreeIntegrityError()


def _validate_event_payload(
    context: _Context,
    event_type: str,
    event_payload: Mapping[str, object],
) -> None:
    if event_type not in {
        _PARTIAL_EVENT,
        _CREATED_EVENT,
        _RECONCILED_EVENT,
        _DATABASE_ACTIVE_EVENT,
        _DATABASE_RETRY_EVENT,
        _FAILED_EVENT,
    }:
        raise WorktreeIntegrityError()
    _parse_checkpoints(
        (
            RunEvent(
                run_id=context.run.id,
                run_version=context.run.version + 1,
                event_type=event_type,
                payload=event_payload,
            ),
        ),
        context.run,
        context.request,
    )


def _expected_secret_id(identity: WorktreeIdentity) -> str:
    if identity.run_id is None:
        raise WorktreeIntegrityError()
    return f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}"


def cast_str_or_none(value: str | None | object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value is _ERROR:
        raise WorktreeIntegrityError()
    return value


__all__ = [
    "WorktreeIntegrityError",
    "WorktreeProvisioner",
    "WorktreeProvisionerError",
    "WorktreeReconciliationRequired",
]
