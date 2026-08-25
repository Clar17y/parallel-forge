"""Durable persisted-run worktree preparation boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from forge.application.ports.operations import OperationAdapter, OperationRepository
from forge.application.ports.runner import (
    CommandResult,
    RunCommandRequest,
    WorktreeRunnerFactoryPort,
    WorktreeRunnerPort,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import (
    ControlledGitPort,
    DatabaseBinding,
    DatabaseProvisionerPort,
    EnvironmentFileEvidence,
    EnvironmentStagingInspection,
    EnvironmentStagingPlan,
    EnvironmentStagingPort,
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
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.tools.runner import await_deferred_cancellation, command_spec_digest

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PROTOCOL_VERSION = 1
_WORKTREE_KIND = "worktree.create"
_WORKTREE_TEARDOWN_KIND = "worktree.teardown"
_WORKTREE_REMOVED_EVENT = "resource.worktree_removed"
_DATABASE_REMOVED_EVENT = "resource.database_removed"
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
_SETUP_KINDS = (
    StepKind.BOOTSTRAP,
    StepKind.INSTALL,
    StepKind.MIGRATION,
    StepKind.SEED,
)
_ENVIRONMENT_KIND = "worktree.environment.stage"
_SETUP_COMMAND_KIND = "worktree.setup.command"
_ENVIRONMENT_STAGED_EVENT = "resource.environment_staged"
_SETUP_STEP_EVENT = "resource.setup_step_completed"
_PREPARED_EVENT = "resource.worktree_prepared"


async def _run_redacted_thread[Result](
    operation: Callable[[], Result],
    *,
    already_cancelled: bool = False,
) -> tuple[Result, bool]:
    try:
        result = await await_deferred_cancellation(
            asyncio.to_thread(operation),
            already_cancelled=already_cancelled,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - boundary diagnostics must not escape
        failure = WorktreeReconciliationRequired()
    else:
        return result
    raise failure


async def _run_redacted_async[Result](operation: Awaitable[Result]) -> Result:
    try:
        result = await operation
    except asyncio.CancelledError:
        raise
    except WorktreeProvisionerError:
        raise
    except Exception:  # noqa: BLE001 - boundary diagnostics must not escape
        failure = WorktreeReconciliationRequired()
    else:
        return result
    raise failure


def _run_redacted[Result](operation: Callable[[], Result]) -> Result:
    try:
        result = operation()
    except Exception:  # noqa: BLE001 - boundary diagnostics must not escape
        failure = WorktreeReconciliationRequired()
    else:
        return result
    raise failure


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


class _ResourceConflict(WorktreeReconciliationRequired):
    """A locked run advanced beyond the caller's validated snapshot."""


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


@dataclass(frozen=True, slots=True)
class _TeardownContext:
    run: RunSnapshot
    policy: ProjectPolicy
    identity: WorktreeIdentity
    expected: ManagedWorktree
    request: OperationRequest
    removed: _EventCheckpoint | None


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
        environment_stager: EnvironmentStagingPort | None = None,
        runner_factory: WorktreeRunnerFactoryPort | None = None,
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
        if (environment_stager is None) != (runner_factory is None):
            raise TypeError("worktree setup requires staging and runner boundaries")
        self._environment_stager = environment_stager
        self._runner_factory = runner_factory

    async def teardown(self, run_id: UUID, policy: ProjectPolicy) -> RunSnapshot:
        """Remove one exact persisted worktree before any database cleanup."""

        _validate_public_inputs(run_id, policy)
        context = await self._load_teardown_context(run_id, policy)
        if (
            context.run.worktree_path is None
            and context.run.database_state is ResourceState.REMOVED
        ):
            return context.run
        operation = await self._operation_for_request(context.request)
        if context.run.worktree_path is not None or operation is not None:
            adapter = _WorktreeTeardownAdapter(self, policy)
            try:
                outcome = await self._operation_executor.execute(context.request, adapter)
            except asyncio.CancelledError:
                raise
            except WorktreeProvisionerError:
                raise
            except Exception:  # noqa: BLE001 - executor failures are redacted
                raise WorktreeReconciliationRequired() from None
            if outcome.status is not OperationStatus.SUCCEEDED:
                raise WorktreeReconciliationRequired()
            if not _outcome_matches(outcome, context.request, context.identity):
                raise WorktreeReconciliationRequired()
        elif policy.database.enabled and context.run.database_state not in {
            ResourceState.DISABLED,
            ResourceState.REMOVED,
        }:
            raise WorktreeReconciliationRequired()

        latest = await self._load_teardown_context(run_id, policy)
        if latest.run.worktree_path is not None:
            raise WorktreeReconciliationRequired()
        if not policy.database.enabled:
            return latest.run
        if latest.run.database_state is ResourceState.REMOVED:
            return latest.run
        if latest.run.database_state is ResourceState.DISABLED:
            return latest.run

        resource = DatabaseBinding(
            state=latest.run.database_state,
            database_name=latest.run.database_name,
            database_role=latest.run.database_role,
            secret_id=latest.run.secret_id,
        )
        try:
            self._database.validate_binding(latest.identity, resource)
            removed = await self._database.teardown(
                latest.identity,
                latest.policy.database,
                resource,
                policy_version=_require_policy_version(latest.run),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - database failures retain exact state
            raise WorktreeReconciliationRequired() from None
        if removed != DatabaseBinding(state=ResourceState.REMOVED):
            raise WorktreeReconciliationRequired()
        operation = await self._operation_for_request(latest.request)
        if operation is None or operation.status is not OperationStatus.SUCCEEDED:
            raise WorktreeReconciliationRequired()
        completed = await self._record_database_removed(latest, operation.id)
        return completed.run

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
            return await self._run_setup(latest, inspected)

        if latest.run.database_state is ResourceState.ACTIVE:
            await self._verify_active_context(latest)
            return await self._run_setup(latest, inspected)
        if latest.run.database_state not in {ResourceState.PROVISIONING, ResourceState.FAILED}:
            raise WorktreeReconciliationRequired()
        prepared = await self._prepare_database(latest, inspected)
        active = await self._load_context(run_id, policy, require_succeeded=True)
        return await self._run_setup(active, prepared)

    async def _run_setup(
        self,
        context: _Context,
        worktree: ManagedWorktree,
    ) -> ManagedWorktree:
        stager = self._environment_stager
        runner_factory = self._runner_factory
        if stager is None or runner_factory is None:
            return worktree
        binding = await self._setup_binding(context)
        plan, plan_cancelled = await _run_redacted_thread(
            lambda: stager.build_plan(
                worktree,
                context.policy,
                binding,
                policy_version=_require_policy_version(context.run),
            )
        )
        if plan_cancelled:
            raise asyncio.CancelledError()

        environment_request = _environment_request(context, plan)
        environment_adapter = _EnvironmentStageAdapter(
            self,
            stager,
            context.policy,
            worktree,
            plan,
            environment_request,
        )
        environment_outcome = await self._execute_setup_effect(
            environment_request,
            environment_adapter,
        )
        environment_intent = await self._require_completed_effect(
            environment_request,
            environment_outcome,
            context.policy,
        )
        if environment_adapter.caller_cancelled:
            raise asyncio.CancelledError()

        runner = _run_redacted(lambda: runner_factory.create(worktree, context.policy))
        setup_evidence: list[Mapping[str, object]] = []
        ordinal = 0
        for kind in _SETUP_KINDS:
            for command in context.policy.commands_for(kind):
                selected = {
                    key: binding.environment[key]
                    for key in command.environment_keys
                    if key in binding.environment
                }
                command_request = _setup_command_request(
                    context,
                    command,
                    ordinal=ordinal,
                    environment_keys=tuple(selected),
                )
                command_adapter = _SetupCommandAdapter(
                    self,
                    context.policy,
                    worktree,
                    runner,
                    RunCommandRequest(
                        command_name=command.name,
                        kind=kind,
                        environment=selected,
                    ),
                    command_request,
                )
                command_outcome = await self._execute_setup_effect(
                    command_request,
                    command_adapter,
                )
                command_intent = await self._require_completed_effect(
                    command_request,
                    command_outcome,
                    context.policy,
                )
                result = command_outcome.payload
                setup_evidence.append(
                    {
                        "operation_intent_id": str(command_intent.id),
                        "evidence_digest": result["evidence_digest"],
                    }
                )
                if command_adapter.caller_cancelled:
                    raise asyncio.CancelledError()
                if result.get("exit_code") != 0 or result.get("timed_out") is not False:
                    raise WorktreeReconciliationRequired()
                ordinal += 1

        latest = await self._load_context(
            context.run.id,
            context.policy,
            require_succeeded=True,
        )
        await self._inspect_present(latest.expected)
        inspection, inspection_cancelled = await _run_redacted_thread(
            lambda: stager.inspect(worktree, context.policy, plan)
        )
        if not inspection.present or inspection.evidence != plan.evidence:
            raise WorktreeReconciliationRequired()
        if inspection_cancelled:
            raise asyncio.CancelledError()
        if latest.run.database_state is ResourceState.ACTIVE:
            await self._verify_active_context(latest)
        prepared_payload: Mapping[str, object] = {
            "worktree_operation_intent_id": str(latest.operation_id),
            "environment_operation_intent_id": str(environment_intent.id),
            "environment_evidence_digest": environment_outcome.payload["evidence_digest"],
            "setup_operations": setup_evidence,
            "setup_count": len(setup_evidence),
            "policy_version": latest.policy.version,
            "worktree_name": latest.identity.worktree_name,
            "base_sha": _require_sha(latest.run.base_sha),
            "database_state": latest.run.database_state.value,
        }
        await self._record_prepared_once(latest, prepared_payload)
        return worktree

    async def _setup_binding(self, context: _Context) -> DatabaseBinding:
        if context.run.database_state is ResourceState.DISABLED:
            return DatabaseBinding(state=ResourceState.DISABLED)
        if context.run.database_state is not ResourceState.ACTIVE:
            raise WorktreeReconciliationRequired()
        binding = DatabaseBinding(
            state=ResourceState.ACTIVE,
            database_name=context.run.database_name,
            database_role=context.run.database_role,
            secret_id=context.run.secret_id,
        )
        return await _run_redacted_async(
            self._database.rematerialize_active(
                context.identity,
                context.policy.database,
                binding,
                policy_version=_require_policy_version(context.run),
            )
        )

    async def _execute_setup_effect(
        self,
        request: OperationRequest,
        adapter: OperationAdapter,
    ) -> OperationOutcome:
        outcome = await _run_redacted_async(self._operation_executor.execute(request, adapter))
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise WorktreeReconciliationRequired()
        return outcome

    async def _require_completed_effect(
        self,
        request: OperationRequest,
        outcome: OperationOutcome,
        policy: ProjectPolicy,
    ) -> OperationIntent:
        intent = await self._operation_for_request(request)
        if intent is None or intent.status is not OperationStatus.SUCCEEDED:
            raise WorktreeReconciliationRequired()
        _validate_intent(intent, request)
        if intent.outcome is None or canonical_digest(intent.outcome) != canonical_digest(
            outcome.payload
        ):
            raise WorktreeReconciliationRequired()
        event_type = (
            _ENVIRONMENT_STAGED_EVENT if request.kind == _ENVIRONMENT_KIND else _SETUP_STEP_EVENT
        )
        checkpoint = await self._one_setup_checkpoint(
            request.run_id,
            event_type,
            intent.id,
        )
        if checkpoint is None:
            raise WorktreeReconciliationRequired()
        if request.kind == _ENVIRONMENT_KIND:
            expected = {
                "operation_intent_id": str(intent.id),
                **dict(request.request_payload),
            }
            if canonical_digest(checkpoint) != canonical_digest(expected):
                raise WorktreeReconciliationRequired()
        elif request.kind == _SETUP_COMMAND_KIND:
            recovered = _command_checkpoint_outcome(request, checkpoint, policy)
            if canonical_digest(recovered.payload) != canonical_digest(outcome.payload):
                raise WorktreeReconciliationRequired()
        else:
            raise WorktreeReconciliationRequired()
        return intent

    async def _one_setup_checkpoint(
        self,
        run_id: UUID,
        event_type: str,
        intent_id: UUID,
    ) -> Mapping[str, object] | None:
        try:
            async with self._unit_of_work_factory() as work:
                raw_events = await work.events.list_after(run_id, 0)
        except WorktreeProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - persistence failures remain redacted
            failure: WorktreeReconciliationRequired | None = WorktreeReconciliationRequired()
        else:
            failure = None
        if failure is not None:
            raise failure
        matches: list[Mapping[str, object]] = []
        expected_ordinal = 0
        seen_intents: set[str] = set()
        for event in raw_events:
            if not isinstance(event, RunEvent) or event.event_type != event_type:
                continue
            if event.run_id != run_id or not isinstance(event.payload, Mapping):
                raise WorktreeReconciliationRequired()
            operation_intent_id = event.payload.get("operation_intent_id")
            if event_type == _SETUP_STEP_EVENT:
                if (
                    event.payload.get("ordinal") != expected_ordinal
                    or not isinstance(operation_intent_id, str)
                    or operation_intent_id in seen_intents
                ):
                    raise WorktreeReconciliationRequired()
                seen_intents.add(operation_intent_id)
                expected_ordinal += 1
            if operation_intent_id == str(intent_id):
                matches.append(event.payload)
        if len(matches) > 1:
            raise WorktreeReconciliationRequired()
        return matches[0] if matches else None

    async def _record_setup_event(
        self,
        context: _Context,
        event_type: str,
        payload: Mapping[str, object],
    ) -> _Context:
        return await self._record_resource(
            context,
            database_state=context.run.database_state,
            event_type=event_type,
            event_payload=payload,
        )

    async def _record_prepared_once(
        self,
        context: _Context,
        payload: Mapping[str, object],
    ) -> _Context:
        _validate_event_payload(context, _PREPARED_EVENT, payload)
        try:
            async with self._unit_of_work_factory() as work:
                current = await work.runs.get_for_update(context.run.id)
                raw_events = await work.events.list_after(context.run.id, 0)
                matches = [
                    event
                    for event in raw_events
                    if isinstance(event, RunEvent) and event.event_type == _PREPARED_EVENT
                ]
                if len(matches) > 1:
                    raise WorktreeReconciliationRequired()
                if matches:
                    prepared = matches[0]
                    resource_events = [
                        event
                        for event in raw_events
                        if isinstance(event, RunEvent) and event.event_type.startswith("resource.")
                    ]
                    if (
                        prepared.run_id != context.run.id
                        or prepared.run_version > current.version
                        or len(resource_events) < 2
                        or resource_events[-1] is not prepared
                        or prepared.run_version != resource_events[-2].run_version + 1
                        or canonical_digest(prepared.payload) != canonical_digest(payload)
                    ):
                        raise WorktreeReconciliationRequired()
                else:
                    if current.version != context.run.version:
                        raise _ResourceConflict()
                    await work.runs.update_resource(
                        current.id,
                        context.run.version,
                        worktree_path=context.run.worktree_path,
                        database_state=context.run.database_state,
                        database_name=context.run.database_name,
                        database_role=context.run.database_role,
                        secret_id=context.run.secret_id,
                        event_type=_PREPARED_EVENT,
                        event_payload=payload,
                    )
                    await work.commit()
        except WorktreeProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - transaction failures expose no persistence details
            failure: WorktreeReconciliationRequired | None = WorktreeReconciliationRequired()
        else:
            failure = None
        if failure is not None:
            raise failure
        return await self._load_context(
            context.run.id,
            context.policy,
            require_succeeded=True,
        )

    async def reconcile(self, intent_id: UUID, policy: ProjectPolicy) -> OperationIntent:
        """Reconcile one claimed worktree intent by inspection only."""

        _validate_public_inputs(intent_id, policy)
        try:
            intent = await self._operations.get(intent_id)
        except Exception:  # noqa: BLE001 - persistence diagnostics are redacted
            raise WorktreeIntegrityError() from None
        if intent.kind == _WORKTREE_KIND:
            adapter: OperationAdapter = _WorktreeAdapter(
                self,
                policy,
                reconcile_only=True,
            )
        elif intent.kind == _WORKTREE_TEARDOWN_KIND:
            adapter = _WorktreeTeardownAdapter(self, policy)
        else:
            raise WorktreeIntegrityError()
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
            failure: WorktreeIntegrityError | None = WorktreeIntegrityError()
        else:
            failure = None
        if failure is not None:
            raise failure

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
            partial=partial,
            completed=completed,
            active=active,
            operation=operation,
        )

    async def _load_teardown_context(
        self,
        run_id: UUID,
        policy: ProjectPolicy,
    ) -> _TeardownContext:
        try:
            async with self._unit_of_work_factory() as work:
                run = await work.runs.get(run_id)
                raw_events = await work.events.list_after(run_id, 0)
        except Exception:  # noqa: BLE001 - persistence diagnostics are redacted
            raise WorktreeIntegrityError() from None

        _validate_teardown_run_and_policy(run, policy, self._git.repository_path)
        identity = _identity_for_run(run, policy)
        try:
            expected = self._git.expected_worktree(identity, _require_sha(run.base_sha))
        except Exception:  # noqa: BLE001 - Git diagnostics are redacted
            raise WorktreeIntegrityError() from None
        _validate_teardown_resource(run, policy, identity, expected, self._database)
        request = _teardown_request(run, identity, policy)
        removed = _parse_teardown_checkpoint(raw_events, run, request)
        _validate_teardown_checkpoint_shape(run, policy, removed)
        return _TeardownContext(
            run=run,
            policy=policy,
            identity=identity,
            expected=expected,
            request=request,
            removed=removed,
        )

    async def _record_worktree_removed(
        self,
        context: _TeardownContext,
        intent_id: UUID,
    ) -> _TeardownContext:
        if context.run.worktree_path != str(context.expected.path):
            raise WorktreeReconciliationRequired()
        payload = _teardown_checkpoint_payload(
            context.request,
            intent_id,
            target_state=context.run.database_state,
        )
        try:
            async with self._unit_of_work_factory() as work:
                current = await work.runs.get_for_update(context.run.id)
                if current.version != context.run.version:
                    raise _ResourceConflict()
                await work.runs.update_resource(
                    current.id,
                    context.run.version,
                    worktree_path=None,
                    database_state=context.run.database_state,
                    database_name=context.run.database_name,
                    database_role=context.run.database_role,
                    secret_id=context.run.secret_id,
                    event_type=_WORKTREE_REMOVED_EVENT,
                    event_payload=payload,
                )
                await work.commit()
        except WorktreeProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - transaction failures are redacted
            raise WorktreeReconciliationRequired() from None
        return await self._load_teardown_context(context.run.id, context.policy)

    async def _record_database_removed(
        self,
        context: _TeardownContext,
        intent_id: UUID,
    ) -> _TeardownContext:
        if context.run.worktree_path is not None:
            raise WorktreeReconciliationRequired()
        payload = _teardown_checkpoint_payload(
            context.request,
            intent_id,
            target_state=ResourceState.REMOVED,
        )
        try:
            async with self._unit_of_work_factory() as work:
                current = await work.runs.get_for_update(context.run.id)
                if current.version != context.run.version:
                    raise _ResourceConflict()
                await work.runs.update_resource(
                    current.id,
                    context.run.version,
                    worktree_path=None,
                    database_state=ResourceState.REMOVED,
                    database_name=None,
                    database_role=None,
                    secret_id=None,
                    event_type=_DATABASE_REMOVED_EVENT,
                    event_payload=payload,
                )
                await work.commit()
        except WorktreeProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - transaction failures are redacted
            raise WorktreeReconciliationRequired() from None
        return await self._load_teardown_context(context.run.id, context.policy)

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
        except _ResourceConflict:
            return await self._converge_active(
                current,
                worktree,
                database_intent_id,
            )
        except Exception:  # noqa: BLE001 - checkpoint failures retain a safe partial state
            await self._record_failure(current, validated_binding)
            raise WorktreeReconciliationRequired() from None
        await self._verify_active_context(active_context)
        return worktree

    async def _converge_active(
        self,
        context: _Context,
        worktree: ManagedWorktree,
        database_intent_id: UUID,
    ) -> ManagedWorktree:
        latest = await self._load_context(
            context.run.id,
            context.policy,
            require_succeeded=True,
        )
        if (
            latest.run.database_state is not ResourceState.ACTIVE
            or latest.completed is None
            or latest.active is None
            or latest.active.database_intent_id != database_intent_id
            or not _same_handle(worktree, latest.expected)
        ):
            raise WorktreeReconciliationRequired()
        await self._verify_active_context(latest)
        return await self._inspect_present(latest.expected)

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
                    raise _ResourceConflict()
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
            failure: WorktreeReconciliationRequired | None = WorktreeReconciliationRequired()
        else:
            failure = None
        if failure is not None:
            raise failure
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

    async def _verify_absent(self, expected: ManagedWorktree) -> bool:
        _, cancelled = await _run_redacted_thread(
            lambda: self._git.verify_worktree_absent(expected)
        )
        return cancelled

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


class _WorktreeTeardownAdapter:
    """Remove one exact registered worktree and reconcile only by inspection."""

    def __init__(self, owner: WorktreeProvisioner, policy: ProjectPolicy) -> None:
        self._owner = owner
        self._policy = policy

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        context = await self._owner._load_teardown_context(intent.run_id, self._policy)
        _validate_adapter_intent(intent, context.request)
        if context.run.worktree_path is None:
            raise WorktreeReconciliationRequired()

        present, inspect_cancelled = await self._owner._inspect_any(context.expected)
        if present is None:
            if inspect_cancelled:
                raise asyncio.CancelledError()
            raise WorktreeReconciliationRequired()
        if inspect_cancelled:
            raise asyncio.CancelledError()

        remove_cancelled = False
        try:
            _, remove_cancelled = await _run_redacted_thread(
                lambda: self._owner._git.remove_worktree(context.expected)
            )
        except WorktreeProvisionerError:
            pass
        verify_cancelled = await self._owner._verify_absent(context.expected)
        _, prune_cancelled = await _run_redacted_thread(
            self._owner._git.prune,
            already_cancelled=remove_cancelled or verify_cancelled,
        )
        context = await self._owner._record_worktree_removed(context, intent.id)
        _require_teardown_checkpoint(context, intent.id)
        if remove_cancelled or verify_cancelled or prune_cancelled:
            raise asyncio.CancelledError()
        return _worktree_outcome(context.request, context.identity)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        context = await self._owner._load_teardown_context(intent.run_id, self._policy)
        _validate_adapter_intent(intent, context.request)
        if context.run.worktree_path is None:
            _require_teardown_checkpoint(context, intent.id)
        try:
            verify_cancelled = await self._owner._verify_absent(context.expected)
        except WorktreeReconciliationRequired:
            present, inspect_cancelled = await self._owner._inspect_any(context.expected)
            if present is None:
                raise WorktreeReconciliationRequired()
            if inspect_cancelled:
                raise asyncio.CancelledError()
            return _needs_outcome()
        _, prune_cancelled = await _run_redacted_thread(
            self._owner._git.prune,
            already_cancelled=verify_cancelled,
        )
        if context.run.worktree_path is not None:
            context = await self._owner._record_worktree_removed(context, intent.id)
        if context.run.worktree_path is not None:
            raise WorktreeReconciliationRequired()
        _require_teardown_checkpoint(context, intent.id)
        if verify_cancelled or prune_cancelled:
            raise asyncio.CancelledError()
        return _worktree_outcome(context.request, context.identity)


class _EnvironmentStageAdapter:
    def __init__(
        self,
        owner: WorktreeProvisioner,
        stager: EnvironmentStagingPort,
        policy: ProjectPolicy,
        worktree: ManagedWorktree,
        plan: EnvironmentStagingPlan,
        request: OperationRequest,
    ) -> None:
        self._owner = owner
        self._stager = stager
        self._policy = policy
        self._worktree = worktree
        self._plan = plan
        self._request = request
        self._caller_cancelled = False

    @property
    def caller_cancelled(self) -> bool:
        return self._caller_cancelled

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_adapter_intent(intent, self._request)
        context = await self._owner._load_context(
            intent.run_id,
            self._policy,
            require_succeeded=True,
        )
        inspected = await self._owner._inspect_present(context.expected)
        if not _same_handle(inspected, self._worktree):
            raise WorktreeReconciliationRequired()
        try:
            (published, inspection), staging_cancelled = await _run_redacted_thread(
                self._publish_and_inspect
            )
        except asyncio.CancelledError:
            self._caller_cancelled = True
            published = self._plan.evidence
            inspection, inspection_cancelled = await _run_redacted_thread(
                lambda: self._stager.inspect(
                    self._worktree,
                    self._policy,
                    self._plan,
                ),
                already_cancelled=True,
            )
            self._caller_cancelled = self._caller_cancelled or inspection_cancelled
        else:
            self._caller_cancelled = self._caller_cancelled or staging_cancelled
        if (
            published != self._plan.evidence
            or not inspection.present
            or inspection.evidence != self._plan.evidence
        ):
            raise WorktreeReconciliationRequired()
        outcome = _environment_outcome(self._request)
        _, checkpoint_cancelled = await await_deferred_cancellation(
            self._owner._record_setup_event(
                context,
                _ENVIRONMENT_STAGED_EVENT,
                {
                    "operation_intent_id": str(intent.id),
                    **dict(self._request.request_payload),
                },
            ),
            already_cancelled=self._caller_cancelled,
        )
        self._caller_cancelled = self._caller_cancelled or checkpoint_cancelled
        return outcome

    def _publish_and_inspect(
        self,
    ) -> tuple[
        tuple[EnvironmentFileEvidence, ...],
        EnvironmentStagingInspection,
    ]:
        published = self._stager.publish(
            self._worktree,
            self._policy,
            self._plan,
        )
        inspection = self._stager.inspect(
            self._worktree,
            self._policy,
            self._plan,
        )
        return published, inspection

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        _validate_adapter_intent(intent, self._request)
        context = await self._owner._load_context(
            intent.run_id,
            self._policy,
            require_succeeded=True,
        )
        inspected = await self._owner._inspect_present(context.expected)
        if not _same_handle(inspected, self._worktree):
            raise WorktreeReconciliationRequired()
        inspection, inspection_cancelled = await _run_redacted_thread(
            lambda: self._stager.inspect(
                self._worktree,
                self._policy,
                self._plan,
            ),
            already_cancelled=self._caller_cancelled,
        )
        self._caller_cancelled = self._caller_cancelled or inspection_cancelled
        if not inspection.present or inspection.evidence != self._plan.evidence:
            raise WorktreeReconciliationRequired()
        payload = await self._owner._one_setup_checkpoint(
            intent.run_id,
            _ENVIRONMENT_STAGED_EVENT,
            intent.id,
        )
        expected_payload = {
            "operation_intent_id": str(intent.id),
            **dict(self._request.request_payload),
        }
        if payload is None:
            _, checkpoint_cancelled = await await_deferred_cancellation(
                self._owner._record_setup_event(
                    context,
                    _ENVIRONMENT_STAGED_EVENT,
                    expected_payload,
                ),
                already_cancelled=self._caller_cancelled,
            )
            self._caller_cancelled = self._caller_cancelled or checkpoint_cancelled
        elif canonical_digest(payload) != canonical_digest(expected_payload):
            raise WorktreeReconciliationRequired()
        return _environment_outcome(self._request)


class _SetupCommandAdapter:
    def __init__(
        self,
        owner: WorktreeProvisioner,
        policy: ProjectPolicy,
        worktree: ManagedWorktree,
        runner: WorktreeRunnerPort,
        run_request: RunCommandRequest,
        request: OperationRequest,
    ) -> None:
        self._owner = owner
        self._policy = policy
        self._worktree = worktree
        self._runner = runner
        self._run_request = run_request
        self._request = request
        self._caller_cancelled = False

    @property
    def caller_cancelled(self) -> bool:
        return self._caller_cancelled

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_adapter_intent(intent, self._request)
        context = await self._owner._load_context(
            intent.run_id,
            self._policy,
            require_succeeded=True,
        )
        inspected = await self._owner._inspect_present(context.expected)
        if not _same_handle(inspected, self._worktree):
            raise WorktreeReconciliationRequired()
        terminal = await _run_redacted_async(self._runner.run_terminal(self._run_request))
        _validate_command_result(self._request, terminal.result, self._policy)
        outcome = _command_outcome(self._request, terminal.result, terminal.caller_cancelled)
        _, checkpoint_cancelled = await await_deferred_cancellation(
            self._owner._record_setup_event(
                context,
                _SETUP_STEP_EVENT,
                {
                    "operation_intent_id": str(intent.id),
                    **dict(self._request.request_payload),
                    **dict(outcome.payload),
                },
            ),
            already_cancelled=terminal.caller_cancelled,
        )
        self._caller_cancelled = terminal.caller_cancelled or checkpoint_cancelled
        return outcome

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        _validate_adapter_intent(intent, self._request)
        context = await self._owner._load_context(
            intent.run_id,
            self._policy,
            require_succeeded=True,
        )
        inspected = await self._owner._inspect_present(context.expected)
        if not _same_handle(inspected, self._worktree):
            raise WorktreeReconciliationRequired()
        payload = await self._owner._one_setup_checkpoint(
            intent.run_id,
            _SETUP_STEP_EVENT,
            intent.id,
        )
        if payload is None:
            raise WorktreeReconciliationRequired()
        return _command_checkpoint_outcome(self._request, payload, self._policy)


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
                    await self._record_created(partial)
                except Exception:  # noqa: BLE001 - preserve safe partial state after Git
                    await self._owner._record_failure(partial)
                    raise WorktreeReconciliationRequired() from None
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
        await self._owner._record_resource(
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
        if inspection_cancelled:
            raise asyncio.CancelledError()
        return _worktree_outcome(context.request, context.identity)


def _environment_evidence(plan: EnvironmentStagingPlan) -> tuple[Mapping[str, object], ...]:
    return tuple(
        {
            "path_digest": item.path_digest,
            "source_digest": item.source_digest,
            "output_digest": item.output_digest,
            "byte_count": item.byte_count,
        }
        for item in plan.evidence
    )


def _environment_request(context: _Context, plan: EnvironmentStagingPlan) -> OperationRequest:
    files = _environment_evidence(plan)
    evidence_digest = canonical_digest({"files": files})
    payload: Mapping[str, object] = {
        "project_id": str(context.run.project_id),
        "run_id": str(context.run.id),
        "policy_version": context.policy.version,
        "worktree_name": context.identity.worktree_name,
        "database_state": context.run.database_state.value,
        "files": files,
        "evidence_digest": evidence_digest,
        "file_count": len(files),
        "protocol_version": _PROTOCOL_VERSION,
    }
    return OperationRequest(
        run_id=context.run.id,
        kind=_ENVIRONMENT_KIND,
        idempotency_key=(
            f"forge-worktree-v{_PROTOCOL_VERSION}:{_ENVIRONMENT_KIND}:"
            f"{context.run.project_id.hex}:{context.run.id.hex}:{context.policy.version}"
        ),
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )


def _environment_outcome(request: OperationRequest) -> OperationOutcome:
    return OperationOutcome(
        status=OperationStatus.SUCCEEDED,
        remote_resource_id=str(request.request_payload["worktree_name"]),
        payload={
            "worktree_name": request.request_payload["worktree_name"],
            "evidence_digest": request.request_payload["evidence_digest"],
            "file_count": request.request_payload["file_count"],
        },
    )


def _setup_command_request(
    context: _Context,
    command: CommandSpec,
    *,
    ordinal: int,
    environment_keys: tuple[str, ...],
) -> OperationRequest:
    environment_keys_digest = canonical_digest({"keys": environment_keys})
    payload: Mapping[str, object] = {
        "project_id": str(context.run.project_id),
        "run_id": str(context.run.id),
        "policy_version": context.policy.version,
        "worktree_name": context.identity.worktree_name,
        "base_sha": _require_sha(context.run.base_sha),
        "ordinal": ordinal,
        "kind": command.kind.value,
        "command_name": command.name,
        "command_digest": command_spec_digest(command),
        "environment_keys_digest": environment_keys_digest,
        "protocol_version": _PROTOCOL_VERSION,
    }
    return OperationRequest(
        run_id=context.run.id,
        kind=_SETUP_COMMAND_KIND,
        idempotency_key=(
            f"forge-worktree-v{_PROTOCOL_VERSION}:{_SETUP_COMMAND_KIND}:"
            f"{context.run.project_id.hex}:{context.run.id.hex}:"
            f"{context.policy.version}:{ordinal}"
        ),
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )


_COMMAND_OUTCOME_KEYS = frozenset(
    {
        "ordinal",
        "kind",
        "command_name",
        "command_digest",
        "evidence_digest",
        "policy_version",
        "exit_code",
        "timed_out",
        "started_at",
        "duration_ms",
        "stdout_digest",
        "stderr_digest",
        "runner_mode",
        "image_digest",
        "network_enabled",
        "stdout_original_byte_count",
        "stderr_original_byte_count",
        "stdout_truncated",
        "stderr_truncated",
        "unsandboxed",
        "caller_cancelled",
    }
)


def _command_checkpoint_outcome(
    request: OperationRequest,
    payload: Mapping[str, object],
    policy: ProjectPolicy,
) -> OperationOutcome:
    expected_keys = (
        frozenset(request.request_payload) | _COMMAND_OUTCOME_KEYS | {"operation_intent_id"}
    )
    if frozenset(payload) != expected_keys or any(
        payload.get(key) != value for key, value in request.request_payload.items()
    ):
        raise WorktreeReconciliationRequired()
    try:
        result = CommandResult(
            command_name=cast(str, payload["command_name"]),
            kind=StepKind(cast(str, payload["kind"])),
            command_digest=cast(str, payload["command_digest"]),
            policy_version=cast(int, payload["policy_version"]),
            exit_code=cast(int | None, payload["exit_code"]),
            timed_out=cast(bool, payload["timed_out"]),
            started_at=datetime.fromisoformat(cast(str, payload["started_at"])),
            duration_ms=cast(int, payload["duration_ms"]),
            stdout_digest=cast(str, payload["stdout_digest"]),
            stderr_digest=cast(str, payload["stderr_digest"]),
            runner_mode=RunnerMode(cast(str, payload["runner_mode"])),
            image_digest=cast(str | None, payload["image_digest"]),
            network_enabled=cast(bool, payload["network_enabled"]),
            stdout_original_byte_count=cast(
                int,
                payload["stdout_original_byte_count"],
            ),
            stderr_original_byte_count=cast(
                int,
                payload["stderr_original_byte_count"],
            ),
            stdout_truncated=cast(bool, payload["stdout_truncated"]),
            stderr_truncated=cast(bool, payload["stderr_truncated"]),
            unsandboxed=cast(bool, payload["unsandboxed"]),
        )
        caller_cancelled = cast(bool, payload["caller_cancelled"])
        if type(caller_cancelled) is not bool:
            raise TypeError
    except KeyError, TypeError, ValueError:
        failure: WorktreeReconciliationRequired | None = WorktreeReconciliationRequired()
    else:
        failure = None
    if failure is not None:
        raise failure
    _validate_command_result(request, result, policy)
    outcome = _command_outcome(request, result, caller_cancelled)
    if canonical_digest(outcome.payload) != canonical_digest(
        {key: payload[key] for key in _COMMAND_OUTCOME_KEYS}
    ):
        raise WorktreeReconciliationRequired()
    return outcome


def _validate_command_result(
    request: OperationRequest,
    result: CommandResult,
    policy: ProjectPolicy,
) -> None:
    payload = request.request_payload
    if (
        result.command_name != payload.get("command_name")
        or result.kind.value != payload.get("kind")
        or result.command_digest != payload.get("command_digest")
        or result.policy_version != payload.get("policy_version")
        or result.runner_mode is not policy.runner_mode
        or result.unsandboxed != (policy.runner_mode is RunnerMode.TRUSTED_HOST)
        or (policy.runner_mode.value == "trusted_host" and result.image_digest is not None)
    ):
        raise WorktreeReconciliationRequired()


def _command_outcome(
    request: OperationRequest,
    result: CommandResult,
    caller_cancelled: bool,
) -> OperationOutcome:
    payload: Mapping[str, object] = {
        "ordinal": request.request_payload["ordinal"],
        "kind": result.kind.value,
        "command_name": result.command_name,
        "command_digest": result.command_digest,
        "evidence_digest": result.evidence_digest,
        "policy_version": result.policy_version,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "started_at": result.started_at.isoformat(),
        "duration_ms": result.duration_ms,
        "stdout_digest": result.stdout_digest,
        "stderr_digest": result.stderr_digest,
        "runner_mode": result.runner_mode.value,
        "image_digest": result.image_digest,
        "network_enabled": result.network_enabled,
        "stdout_original_byte_count": result.stdout_original_byte_count,
        "stderr_original_byte_count": result.stderr_original_byte_count,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "unsandboxed": result.unsandboxed,
        "caller_cancelled": caller_cancelled,
    }
    return OperationOutcome(
        status=OperationStatus.SUCCEEDED,
        remote_resource_id=(
            f"{request.request_payload['worktree_name']}:{request.request_payload['ordinal']}"
        ),
        payload=payload,
    )


def _validate_teardown_run_and_policy(
    run: RunSnapshot,
    policy: ProjectPolicy,
    repository_path: Path,
) -> None:
    try:
        if not isinstance(run, RunSnapshot):
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


def _validate_teardown_resource(
    run: RunSnapshot,
    policy: ProjectPolicy,
    identity: WorktreeIdentity,
    expected: ManagedWorktree,
    database: DatabaseProvisionerPort,
) -> None:
    path = run.worktree_path
    if path is not None:
        try:
            candidate = Path(path)
            if candidate != expected.path or not candidate.is_absolute():
                raise WorktreeIntegrityError()
            if any(part in {".", ".."} for part in candidate.parts[1:]):
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
        return

    if run.database_state is ResourceState.DISABLED:
        if path is not None or any(
            value is not None for value in (run.database_name, run.database_role, run.secret_id)
        ):
            raise WorktreeIntegrityError()
        return
    if run.database_state is ResourceState.REMOVED:
        if path is not None or any(
            value is not None for value in (run.database_name, run.database_role, run.secret_id)
        ):
            raise WorktreeIntegrityError()
        return
    if run.database_state not in {
        ResourceState.PROVISIONING,
        ResourceState.FAILED,
        ResourceState.ACTIVE,
    }:
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
    except Exception:  # noqa: BLE001 - injected validators are redacted
        raise WorktreeIntegrityError() from None


def _teardown_request(
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
    }
    return OperationRequest(
        run_id=run.id,
        kind=_WORKTREE_TEARDOWN_KIND,
        idempotency_key=(
            f"forge-worktree-v{_PROTOCOL_VERSION}:{_WORKTREE_TEARDOWN_KIND}:"
            f"{run.project_id.hex}:{run.id.hex}:{policy.version}"
        ),
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )


def _teardown_checkpoint_payload(
    request: OperationRequest,
    operation_intent_id: UUID,
    *,
    target_state: ResourceState,
) -> Mapping[str, object]:
    if not isinstance(operation_intent_id, UUID) or not isinstance(target_state, ResourceState):
        raise WorktreeIntegrityError()
    source = request.request_payload
    return {
        "operation_intent_id": str(operation_intent_id),
        "project_id": source["project_id"],
        "run_id": source["run_id"],
        "policy_version": source["policy_version"],
        "branch_digest": source["branch_digest"],
        "worktree_name": source["worktree_name"],
        "base_sha": source["base_sha"],
        "database_state": target_state.value,
    }


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


def _parse_teardown_checkpoint(
    raw_events: Sequence[object],
    run: RunSnapshot,
    request: OperationRequest,
) -> _EventCheckpoint | None:
    matches: list[_EventCheckpoint] = []
    for event in raw_events:
        if not isinstance(event, RunEvent):
            raise WorktreeIntegrityError()
        if event.event_type != _WORKTREE_REMOVED_EVENT:
            continue
        if event.run_id != run.id or not isinstance(event.payload, Mapping):
            raise WorktreeReconciliationRequired()
        payload = event.payload
        if set(payload) != _CHECKPOINT_KEYS:
            raise WorktreeIntegrityError()
        operation_id = _parse_uuid_field(payload.get("operation_intent_id"))
        if operation_id is None:
            raise WorktreeReconciliationRequired()
        if (
            payload.get("project_id") != request.request_payload.get("project_id")
            or payload.get("run_id") != request.request_payload.get("run_id")
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
        matches.append(
            _EventCheckpoint(
                event_type=event.event_type,
                operation_intent_id=operation_id,
                database_intent_id=None,
                payload=payload,
            )
        )
    if len(matches) > 1:
        raise WorktreeReconciliationRequired()
    return matches[0] if matches else None


def _validate_teardown_checkpoint_shape(
    run: RunSnapshot,
    policy: ProjectPolicy,
    removed: _EventCheckpoint | None,
) -> None:
    if removed is not None and run.worktree_path is not None:
        raise WorktreeReconciliationRequired()
    if (
        policy.database.enabled
        and run.worktree_path is None
        and run.database_state
        in {ResourceState.PROVISIONING, ResourceState.FAILED, ResourceState.ACTIVE}
        and removed is None
    ):
        raise WorktreeReconciliationRequired()
    if removed is None:
        return
    state = _checkpoint_database_state(removed)
    expected_states = (
        {ResourceState.PROVISIONING, ResourceState.FAILED, ResourceState.ACTIVE}
        if policy.database.enabled
        else {ResourceState.DISABLED}
    )
    if state not in expected_states:
        raise WorktreeReconciliationRequired()


def _require_teardown_checkpoint(context: _TeardownContext, intent_id: UUID) -> None:
    if (
        context.removed is None
        or not isinstance(intent_id, UUID)
        or context.removed.operation_intent_id != intent_id
    ):
        raise WorktreeReconciliationRequired()


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
    return OperationOutcome(
        status=OperationStatus.SUCCEEDED,
        remote_resource_id=identity.worktree_name,
        payload={
            "worktree_name": identity.worktree_name,
            "base_sha": request.request_payload.get("base_sha"),
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
    worktree_events = {
        _PARTIAL_EVENT,
        _CREATED_EVENT,
        _RECONCILED_EVENT,
        _DATABASE_ACTIVE_EVENT,
        _DATABASE_RETRY_EVENT,
        _FAILED_EVENT,
    }
    if event_type in worktree_events:
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
        return
    if event_type not in {
        _ENVIRONMENT_STAGED_EVENT,
        _SETUP_STEP_EVENT,
        _PREPARED_EVENT,
    }:
        raise WorktreeIntegrityError()
    if not isinstance(event_payload, Mapping):
        raise WorktreeIntegrityError()


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
