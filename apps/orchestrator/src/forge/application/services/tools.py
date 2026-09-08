"""Exhaustive, deny-by-default authorization for controlled agent tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from forge.application.adapters.git_commit import (
    PREPARE_GIT_COMMIT_KIND,
    PUBLISH_GIT_COMMIT_KIND,
    PrepareGitCommitAdapter,
    PublishGitCommitAdapter,
)
from forge.application.adapters.named_check import (
    NAMED_CHECK_KIND,
    NamedCheckCancellation,
    NamedCheckOperationAdapter,
)
from forge.application.ports.artifacts import ArtifactRepository, ArtifactStore
from forge.application.ports.evidence import EvidenceInputPurpose, EvidenceReadScope
from forge.application.ports.projects import ProjectRecord
from forge.application.ports.repository import (
    MAX_REPOSITORY_WRITE_BYTES,
    FileRead,
    FileWrite,
    InstructionDocument,
    RepositoryEntry,
    RepositoryReader,
    RepositoryWriter,
    SearchMatch,
)
from forge.application.ports.runner import WorktreeRunnerFactoryPort
from forge.application.ports.tools import (
    ToolAuthorizationDenied,
    ToolAuthorizerPort,
    ToolCallRecord,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, GitOutput, ManagedWorktree
from forge.application.services.evidence_reader import EvidenceReader
from forge.application.services.recovery import OperationExecutor
from forge.domain.actor import AgentRole
from forge.domain.event import RunEvent, thaw_payload
from forge.domain.evidence import evidence_manifest_digest
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationStatus,
    canonical_digest,
    canonical_payload,
)
from forge.domain.payload import validate_durable_payload
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import (
    ToolAuthorization,
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolError,
    ToolErrorCode,
    ToolName,
    ToolRequest,
    ToolResult,
    repository_resource_identity,
)
from forge.domain.validation import command_spec_digest
from forge.observability.context import CorrelationContext, bind_context
from forge.observability.redaction import RedactionPolicy, Redactor

_REPOSITORY_READS = frozenset(
    {
        ToolName.REPOSITORY_LIST_FILES,
        ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH,
        ToolName.REPOSITORY_READ_INSTRUCTIONS,
    }
)
_CAPABILITIES = {
    AgentRole.PLANNER: _REPOSITORY_READS,
    AgentRole.DEVELOPER: _REPOSITORY_READS
    | {
        ToolName.REPOSITORY_WRITE_FILE,
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
        ToolName.GIT_COMMIT,
        ToolName.BUILD_RUN_NAMED_CHECK,
    },
    AgentRole.REVIEWER: _REPOSITORY_READS
    | {
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
        ToolName.VALIDATION_RESULTS_READ,
        ToolName.REVIEW_ARTIFACTS_READ,
    },
}

_TOOL_ARGUMENT_SCHEMAS = {
    ToolName.REPOSITORY_LIST_FILES: (frozenset(), frozenset({"path"})),
    ToolName.REPOSITORY_READ_FILE: (frozenset({"path"}), frozenset()),
    ToolName.REPOSITORY_SEARCH: (frozenset({"literal"}), frozenset({"path"})),
    ToolName.REPOSITORY_READ_INSTRUCTIONS: (frozenset(), frozenset({"target_path"})),
    ToolName.REPOSITORY_WRITE_FILE: (frozenset({"content", "path"}), frozenset()),
    ToolName.GIT_STATUS: (frozenset(), frozenset()),
    ToolName.GIT_DIFF: (frozenset(), frozenset({"scope"})),
    ToolName.GIT_COMMIT: (frozenset({"message"}), frozenset()),
    ToolName.BUILD_RUN_NAMED_CHECK: (frozenset({"command_name"}), frozenset()),
    ToolName.VALIDATION_RESULTS_READ: (frozenset(), frozenset()),
    ToolName.REVIEW_ARTIFACTS_READ: (frozenset(), frozenset()),
}


@dataclass(frozen=True, slots=True)
class CapabilityMatrix:
    """The complete decision table for every closed role and tool pair."""

    @property
    def roles(self) -> frozenset[AgentRole]:
        return frozenset(_CAPABILITIES)

    @property
    def tools(self) -> frozenset[ToolName]:
        return frozenset(ToolName)

    def capabilities_for(self, role: AgentRole) -> frozenset[ToolName]:
        """Return no capabilities for values outside the closed role type."""

        if type(role) is not AgentRole:
            return frozenset()
        return frozenset(_CAPABILITIES[role])

    def is_allowed(self, role: AgentRole, tool_name: ToolName) -> bool:
        """Return the exact decision, denying unknown or untyped values."""

        return (
            type(role) is AgentRole
            and type(tool_name) is ToolName
            and tool_name in _CAPABILITIES[role]
        )


@dataclass(frozen=True, slots=True)
class ToolAuthorizer:
    """Bind a typed request to Forge-owned authority or fail closed."""

    _matrix: CapabilityMatrix = field(default_factory=CapabilityMatrix, init=False, repr=False)

    def is_allowed(self, role: AgentRole, tool_name: ToolName) -> bool:
        return self._matrix.is_allowed(role, tool_name)

    def authorize(
        self,
        context: ToolAuthorizationContext,
        request: ToolRequest,
    ) -> ToolAuthorization:
        if type(context) is not ToolAuthorizationContext or type(request) is not ToolRequest:
            raise ToolAuthorizationDenied()
        if not self.is_allowed(context.role, request.name):
            raise ToolAuthorizationDenied()
        if not _arguments_match_schema(request.name, request.arguments):
            raise ToolAuthorizationDenied()
        return ToolAuthorization(context=context, request=request)


def _arguments_match_schema(
    tool_name: ToolName,
    arguments: Mapping[str, object],
) -> bool:
    fields = frozenset(arguments)
    required, optional = _TOOL_ARGUMENT_SCHEMAS[tool_name]
    if tool_name is ToolName.GIT_DIFF and arguments.get("scope", "working_tree") not in (
        "working_tree",
        "candidate",
    ):
        return False
    return required <= fields <= required | optional and all(
        type(value) is str for value in arguments.values()
    )


class ToolInvocationError(RuntimeError):
    """A controlled-tool invocation could not cross its durable boundary."""

    def __init__(self) -> None:
        super().__init__("controlled tool invocation failed")


_READ_TOOLS = frozenset(
    {
        ToolName.REPOSITORY_LIST_FILES,
        ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH,
        ToolName.REPOSITORY_READ_INSTRUCTIONS,
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
    }
)
_UNAVAILABLE_TOOLS = frozenset(
    {
        ToolName.BUILD_RUN_NAMED_CHECK,
    }
)
_WRITE_EXECUTION_LEASE_SECONDS = 30.0
_DUPLICATE_OBSERVER_INITIAL_DELAY_SECONDS = 0.05
_DUPLICATE_OBSERVER_MAX_DELAY_SECONDS = 1.0
_WRITE_RESULT_ARTIFACT_MAX_BYTES = 64 * 1024
_TERMINAL_TOOL_STATUSES = frozenset(
    {
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.DENIED,
        ToolCallStatus.CANCELLED,
    }
)
_BASE_SHA = re.compile(r"\A[0-9a-f]{40}\Z", re.ASCII)
_ACTIVE_RUN_STATES: Mapping[AgentRole, frozenset[RunState]] = MappingProxyType(
    {
        AgentRole.PLANNER: frozenset({RunState.PLANNING, RunState.AWAITING_PLAN_APPROVAL}),
        AgentRole.DEVELOPER: frozenset(
            {
                RunState.PREPARING_WORKTREE,
                RunState.IMPLEMENTING,
                RunState.VALIDATING,
                RunState.REMEDIATING,
            }
        ),
        AgentRole.REVIEWER: frozenset(
            {RunState.VALIDATING, RunState.REVIEWING, RunState.AWAITING_PR_APPROVAL}
        ),
    }
)
_RESULT_REDACTION_POLICY = RedactionPolicy(
    max_string_bytes=64 * 1024,
    max_collection_items=256,
    max_depth=12,
    max_nodes=10_000,
)
_GIT_RESULT_FIELDS = frozenset(
    {
        "agent_execution_id",
        "base_sha",
        "message_digest",
        "new_sha",
        "policy_version",
        "preparation_intent_id",
        "previous_sha",
        "request_digest",
        "run_id",
        "step_id",
        "tool_call_id",
        "tree_sha",
        "worktree_id",
        "publication_intent_id",
    }
)


@dataclass(frozen=True, slots=True)
class _PreparedWrite:
    path: str
    content: str | None
    content_digest: str
    byte_count: int


class ControlledToolService:
    """Validate, dispatch, and durably record one Forge-controlled tool call.

    The service accepts only typed requests and a Forge-created authorization
    context.  Every supported adapter is selected by the closed ``ToolName``
    enum; no caller can provide argv, a path outside the bound worktree, or a
    replacement run/role identity.
    """

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        *,
        authorizer: ToolAuthorizerPort | None = None,
        artifact_store: ArtifactStore | None = None,
        repository_reader: RepositoryReader | None = None,
        repository_writer: RepositoryWriter | None = None,
        controlled_git: ControlledGitPort | None = None,
        operation_executor: OperationExecutor | None = None,
        evidence_reader: EvidenceReader | None = None,
        redactor: Redactor | None = None,
        worktree: ManagedWorktree | None = None,
        runner_factory: WorktreeRunnerFactoryPort | None = None,
        command_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authorizer = authorizer or ToolAuthorizer()
        self._artifact_store = artifact_store
        self._repository_reader = repository_reader
        self._repository_writer = repository_writer
        self._git = controlled_git
        self._operation_executor = operation_executor
        self._evidence_reader = evidence_reader or (
            EvidenceReader(unit_of_work_factory, artifact_store)
            if artifact_store is not None
            else None
        )
        self._redactor = redactor or Redactor(policy=_RESULT_REDACTION_POLICY)
        self._worktree = worktree
        self._git_completions: dict[UUID, asyncio.Task[ToolResult]] = {}
        self._runner_factory = runner_factory
        self._command_environment = MappingProxyType(dict(command_environment or {}))
        self._named_completions: dict[UUID, asyncio.Task[ToolResult]] = {}
        if not callable(self._unit_of_work_factory):
            raise TypeError("controlled tool service requires a unit of work factory")

    async def invoke(
        self,
        context: ToolAuthorizationContext,
        request: ToolRequest,
    ) -> ToolResult:
        """Invoke one typed tool and commit its evidence and terminal event."""

        if type(context) is not ToolAuthorizationContext or type(request) is not ToolRequest:
            raise ToolInvocationError()
        if request.name is ToolName.REPOSITORY_WRITE_FILE:
            return await self._invoke_repository_write(context, request)
        if request.name is ToolName.GIT_COMMIT:
            return await self._invoke_git_commit(context, request)
        if request.name is ToolName.BUILD_RUN_NAMED_CHECK and context.role is AgentRole.DEVELOPER:
            return await self._invoke_named_check(context, request)
        tool_call_id = uuid4()
        started_at = datetime.now(UTC)
        started = time.monotonic()
        try:
            async with self._open_uow() as work:
                resolved_run = await self._resolve_run(work, context)
                if resolved_run is None:
                    raise ToolInvocationError()
                resolved_policy = await self._resolve_policy(work, resolved_run)
                authorization, validation_error = self._validate(
                    context,
                    request,
                    resolved_run,
                    resolved_policy,
                )
                if validation_error is None and not await self._execution_context_is_current(
                    context, work
                ):
                    validation_error = (
                        ToolErrorCode.RESOURCE_MISMATCH,
                        "tool execution identity is not current",
                    )
                if (
                    validation_error is None
                    and resolved_policy is not None
                    and not await self._budget_available(context, resolved_policy, work)
                ):
                    validation_error = (
                        ToolErrorCode.BUDGET_EXCEEDED,
                        "tool-call budget is exhausted",
                    )
                if validation_error is not None:
                    result = self._result(
                        request.name,
                        ToolCallStatus.DENIED,
                        ToolError(code=validation_error[0], message=validation_error[1]),
                    )
                    authorized = False
                else:
                    assert authorization is not None
                    if resolved_run is None or resolved_policy is None:
                        raise ToolInvocationError()
                    result = await self._dispatch(
                        authorization,
                    )
                    authorized = True
                duration_ms = max(0, int((time.monotonic() - started) * 1000))
                result = replace(
                    result,
                    tool_call_id=tool_call_id,
                    correlation_id=tool_call_id,
                    agent_execution_id=context.agent_execution_id,
                    step_id=context.step_id,
                    duration_ms=duration_ms,
                )
                completed_at = datetime.now(UTC)
                record_metadata = _record_metadata(
                    result,
                    authorized=authorized,
                    started_at=started_at,
                    completed_at=completed_at,
                    redactor=self._redactor,
                )
                record = ToolCallRecord(
                    id=tool_call_id,
                    run_id=context.run_id,
                    agent_execution_id=_required_uuid(context.agent_execution_id),
                    tool_name=request.name,
                    normalized_arguments=self._normalized_arguments(request),
                    authorized=authorized,
                    status=result.status,
                    started_at=started_at,
                    completed_at=completed_at,
                    result_metadata=record_metadata,
                    step_id=context.step_id,
                    role=context.role,
                    policy_version=context.policy_version,
                    duration_ms=result.duration_ms,
                    artifact_digests=result.artifact_digests,
                    correlation_id=tool_call_id,
                    operation_intent_id=result.operation_intent_id,
                    arguments_schema_version=1,
                    result_metadata_schema_version=1,
                )
                repository = work.tool_calls
                events = work.events
                with bind_context(
                    CorrelationContext(
                        run_id=context.run_id,
                        step_id=context.step_id,
                        agent_execution_id=context.agent_execution_id,
                        tool_call_id=tool_call_id,
                        operation_intent_id=result.operation_intent_id,
                    )
                ):
                    try:
                        await repository.record(record)
                        await events.append(
                            _tool_event(
                                result,
                                context,
                                resolved_run,
                                tool_call_id,
                                authorized=authorized,
                            )
                        )
                        await work.commit()
                    except Exception:  # noqa: BLE001 - no success may escape an audit failure
                        await work.rollback()
                        raise ToolInvocationError() from None
                return result
        except asyncio.CancelledError:
            raise
        except ToolInvocationError:
            raise
        except Exception:  # noqa: BLE001 - all untrusted boundary failures are stable
            raise ToolInvocationError() from None

    async def _invoke_repository_write(
        self,
        context: ToolAuthorizationContext,
        request: ToolRequest,
    ) -> ToolResult:
        started_at = datetime.now(UTC)
        started = time.monotonic()
        completion: asyncio.Task[ToolResult] | None = None
        writer = self._repository_writer
        executor = self._operation_executor
        artifact_store = self._artifact_store
        try:
            async with self._open_uow() as work:
                resolved_run = await self._resolve_run(work, context)
                if resolved_run is None:
                    raise ToolInvocationError()
                resolved_policy = await self._resolve_policy(work, resolved_run)
                authorization, validation_error = self._validate(
                    context,
                    request,
                    resolved_run,
                    resolved_policy,
                )
                if validation_error is None and not await self._execution_context_is_current(
                    context, work
                ):
                    validation_error = (
                        ToolErrorCode.RESOURCE_MISMATCH,
                        "tool execution identity is not current",
                    )

                prepared = self._write_request_values(request)
                normalized_arguments = self._normalized_arguments(request)
                payload: dict[str, object] | None = None
                tool_call_id: UUID | None = None
                existing: ToolCallRecord | None = None
                request_digest: str | None = None
                if (
                    validation_error is None
                    and authorization is not None
                    and resolved_policy is not None
                    and prepared is not None
                ):
                    if context.invocation_id is None:
                        validation_error = (
                            ToolErrorCode.INVALID_REQUEST,
                            "repository write requires an invocation identifier",
                        )
                    else:
                        request_digest = _write_request_digest(request)
                        tool_call_id = _write_tool_call_id(context.invocation_id)
                        existing = await work.tool_calls.find(tool_call_id)
                        if existing is not None and not _write_record_matches_request(
                            existing, context, request.name, normalized_arguments, request_digest
                        ):
                            raise ToolInvocationError()
                        payload = _write_operation_payload(
                            context, resolved_run, prepared, request_digest
                        )
                    if existing is not None and existing.status in _TERMINAL_TOOL_STATUSES:
                        if request_digest is None:
                            raise ToolInvocationError()
                        replay = await _write_replay_result(
                            existing,
                            context,
                            normalized_arguments,
                            request_digest,
                            artifacts=work.artifacts,
                            artifact_store=artifact_store,
                        )
                        await work.rollback()
                        return replay
                    if existing is None and not await self._budget_available(
                        context, resolved_policy, work
                    ):
                        validation_error = (
                            ToolErrorCode.BUDGET_EXCEEDED,
                            "tool-call budget is exhausted",
                        )

                if validation_error is not None:
                    return await self._record_write_denial(
                        work,
                        context,
                        request,
                        resolved_run,
                        validation_error,
                        started_at=started_at,
                        started=started,
                    )
                if (
                    authorization is None
                    or resolved_policy is None
                    or prepared is None
                    or payload is None
                    or tool_call_id is None
                    or request_digest is None
                ):
                    raise ToolInvocationError()
                if (
                    writer is None
                    or artifact_store is None
                    or not isinstance(executor, OperationExecutor)
                ):
                    raise ToolInvocationError()

                intent = await work.operations.begin(
                    run_id=context.run_id,
                    operation_type=ToolName.REPOSITORY_WRITE_FILE.value,
                    idempotency_key=f"tool:{tool_call_id}",
                    request_digest=canonical_digest(payload),
                    request_payload=payload,
                    execution_owner=f"forge-operation-{uuid4().hex}",
                    execution_lease_seconds=_WRITE_EXECUTION_LEASE_SECONDS,
                )
                reservation = ToolCallRecord(
                    id=tool_call_id,
                    run_id=context.run_id,
                    agent_execution_id=_required_uuid(context.agent_execution_id),
                    tool_name=request.name,
                    normalized_arguments=normalized_arguments,
                    authorized=True,
                    status=ToolCallStatus.RUNNING,
                    started_at=existing.started_at if existing is not None else started_at,
                    step_id=context.step_id,
                    role=context.role,
                    policy_version=context.policy_version,
                    correlation_id=tool_call_id,
                    operation_intent_id=intent.id,
                    arguments_schema_version=1,
                    request_digest=request_digest,
                    resource_id=context.worktree_id,
                    invocation_schema_version=1,
                )
                reserved = await work.tool_calls.reserve(reservation)
                completion = asyncio.create_task(
                    self._settle_repository_write(
                        work=work,
                        context=context,
                        request=request,
                        normalized_arguments=normalized_arguments,
                        tool_call_id=tool_call_id,
                        intent=intent,
                        reserved=reserved,
                        prepared=prepared,
                        writer=writer,
                        executor=executor,
                        artifact_store=artifact_store,
                        request_digest=request_digest,
                        started=started,
                    ),
                    name=f"forge-write-{intent.id}",
                )
                return await _await_committed_write(completion)
        except asyncio.CancelledError:
            if completion is not None:
                await _await_committed_write(completion, caller_cancelled=True)
            raise
        except ToolInvocationError:
            if completion is not None:
                try:
                    await _await_committed_write(completion)
                except asyncio.CancelledError:
                    raise
                except Exception as completion_error:  # noqa: BLE001 - original failure is preserved
                    del completion_error
            raise
        except Exception:  # noqa: BLE001 - raw content and adapter details never escape
            if completion is not None:
                try:
                    await _await_committed_write(completion)
                except asyncio.CancelledError:
                    raise
                except Exception as completion_error:  # noqa: BLE001 - original failure is preserved
                    del completion_error
            raise ToolInvocationError() from None

    async def _settle_repository_write(
        self,
        *,
        work: UnitOfWork,
        context: ToolAuthorizationContext,
        request: ToolRequest,
        normalized_arguments: dict[str, object],
        tool_call_id: UUID,
        intent: OperationIntent,
        reserved: ToolCallRecord,
        prepared: _PreparedWrite,
        writer: RepositoryWriter,
        executor: OperationExecutor,
        artifact_store: ArtifactStore,
        request_digest: str,
        started: float,
    ) -> ToolResult:
        """Settle admission before finishing its admitted repository write."""

        await work.commit()
        completion = asyncio.create_task(
            self._complete_repository_write(
                context=context,
                request=request,
                normalized_arguments=normalized_arguments,
                tool_call_id=tool_call_id,
                intent=intent,
                reserved=reserved,
                prepared=prepared,
                writer=writer,
                executor=executor,
                artifact_store=artifact_store,
                request_digest=request_digest,
                started=started,
            ),
            name=f"forge-write-completion-{intent.id}",
        )
        return await _await_committed_write(completion)

    async def _complete_repository_write(
        self,
        *,
        context: ToolAuthorizationContext,
        request: ToolRequest,
        normalized_arguments: dict[str, object],
        tool_call_id: UUID,
        intent: OperationIntent,
        reserved: ToolCallRecord,
        prepared: _PreparedWrite,
        writer: RepositoryWriter,
        executor: OperationExecutor,
        artifact_store: ArtifactStore,
        request_digest: str,
        started: float,
    ) -> ToolResult:
        """Finish a committed write even when its waiting caller is cancelled."""

        adapter = _RepositoryWriteOperationAdapter(writer=writer, prepared=prepared)
        outcome = await executor.execute_admitted(intent, adapter)
        if outcome.status is OperationStatus.SUCCEEDED:
            result_metadata = _safe_metadata(
                thaw_payload(outcome.payload),
                redactor=self._redactor,
            )
            artifact_bytes = _write_result_artifact_bytes(
                intent.id, tool_call_id, request_digest, context.worktree_id, result_metadata
            )
            descriptor = await artifact_store.put_bytes(
                artifact_bytes,
                media_type="application/json",
                max_bytes=_WRITE_RESULT_ARTIFACT_MAX_BYTES,
                bounding_policy="head_tail",
            )
            if await artifact_store.verify(descriptor.digest) is not True:
                raise ToolInvocationError()

            async with self._open_uow() as work:
                terminal_run = await work.runs.get_for_update(context.run_id)
                current = await work.tool_calls.get(tool_call_id)
                if current.status in _TERMINAL_TOOL_STATUSES:
                    replay = await _write_replay_result(
                        current,
                        context,
                        normalized_arguments,
                        request_digest,
                        artifacts=work.artifacts,
                        artifact_store=artifact_store,
                    )
                    await work.rollback()
                    return replay
                if current.status is not ToolCallStatus.RUNNING:
                    raise ToolInvocationError()

                completed_at = datetime.now(UTC)
                duration_ms = max(0, int((time.monotonic() - started) * 1000))
                result = ToolResult(
                    tool_name=request.name,
                    status=ToolCallStatus.SUCCEEDED,
                    metadata=result_metadata,
                    artifact_digests=(descriptor.digest,),
                    tool_call_id=tool_call_id,
                    operation_intent_id=intent.id,
                    correlation_id=tool_call_id,
                    agent_execution_id=context.agent_execution_id,
                    step_id=context.step_id,
                    duration_ms=duration_ms,
                )
                final_record = ToolCallRecord(
                    id=tool_call_id,
                    run_id=context.run_id,
                    agent_execution_id=_required_uuid(context.agent_execution_id),
                    tool_name=request.name,
                    normalized_arguments=normalized_arguments,
                    authorized=True,
                    status=ToolCallStatus.SUCCEEDED,
                    started_at=reserved.started_at,
                    completed_at=completed_at,
                    result_metadata=_write_record_metadata(
                        result,
                        request_digest,
                        context.worktree_id,
                        started_at=reserved.started_at,
                        completed_at=completed_at,
                        redactor=self._redactor,
                    ),
                    step_id=context.step_id,
                    role=context.role,
                    policy_version=context.policy_version,
                    duration_ms=duration_ms,
                    artifact_digests=(descriptor.digest,),
                    correlation_id=tool_call_id,
                    operation_intent_id=intent.id,
                    arguments_schema_version=1,
                    result_metadata_schema_version=1,
                    request_digest=request_digest,
                    resource_id=context.worktree_id,
                    invocation_schema_version=1,
                )
                await work.artifacts.record(
                    descriptor,
                    run_id=context.run_id,
                    producer_type="controlled_tool",
                    producer_id=tool_call_id,
                    metadata={
                        "operation_intent_id": str(intent.id),
                        "producer_id": str(tool_call_id),
                        "request_digest": request_digest,
                        "resource_id": context.worktree_id,
                        "result_schema_version": 1,
                        "tool_name": request.name.value,
                        "invocation_schema_version": 1,
                    },
                )
                await work.tool_calls.finalize(final_record)
                await work.events.append(
                    _tool_event(
                        result,
                        context,
                        terminal_run,
                        tool_call_id,
                        authorized=True,
                    )
                )
                await work.commit()
                return result
        raise ToolInvocationError()

    async def _record_write_denial(
        self,
        work: UnitOfWork,
        context: ToolAuthorizationContext,
        request: ToolRequest,
        run: RunSnapshot,
        validation_error: tuple[ToolErrorCode, str],
        *,
        started_at: datetime,
        started: float,
    ) -> ToolResult:
        tool_call_id = uuid4()
        completed_at = datetime.now(UTC)
        result = ToolResult(
            tool_name=request.name,
            status=ToolCallStatus.DENIED,
            error=ToolError(code=validation_error[0], message=validation_error[1]),
            tool_call_id=tool_call_id,
            correlation_id=tool_call_id,
            agent_execution_id=context.agent_execution_id,
            step_id=context.step_id,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
        record = ToolCallRecord(
            id=tool_call_id,
            run_id=context.run_id,
            agent_execution_id=_required_uuid(context.agent_execution_id),
            tool_name=request.name,
            normalized_arguments=self._normalized_arguments(request),
            authorized=False,
            status=ToolCallStatus.DENIED,
            started_at=started_at,
            completed_at=completed_at,
            result_metadata=_record_metadata(
                result,
                authorized=False,
                started_at=started_at,
                completed_at=completed_at,
                redactor=self._redactor,
            ),
            step_id=context.step_id,
            role=context.role,
            policy_version=context.policy_version,
            duration_ms=result.duration_ms,
            correlation_id=tool_call_id,
            arguments_schema_version=1,
            result_metadata_schema_version=1,
        )
        await work.tool_calls.record(record)
        await work.events.append(_tool_event(result, context, run, tool_call_id, authorized=False))
        await work.commit()
        return result

    async def _invoke_named_check(
        self, context: ToolAuthorizationContext, request: ToolRequest
    ) -> ToolResult:
        call_id = _write_tool_call_id(_required_uuid(context.invocation_id))
        request_digest = canonical_digest(request.arguments)
        normalized = dict(request.arguments)
        started = time.monotonic()
        try:
            async with self._open_uow() as work:
                run = await self._resolve_run(work, context)
                if run is None:
                    raise ToolInvocationError()
                policy = await self._resolve_policy(work, run)
                if policy is None:
                    raise ToolInvocationError()
                existing = await work.tool_calls.find(call_id)
                if existing is not None:
                    if not _write_record_matches_request(
                        existing, context, request.name, normalized, request_digest
                    ):
                        raise ToolInvocationError()
                    if existing.status in _TERMINAL_TOOL_STATUSES:
                        return await self._named_replay(work, existing, context, policy)
                    await work.rollback()
                    completion = self._named_completions.get(call_id)
                    if completion is not None:
                        return await _await_committed_write(completion)
                    command = next(
                        (
                            item
                            for item in policy.commands
                            if item.name == normalized.get("command_name")
                        ),
                        None,
                    )
                    if command is None:
                        raise ToolInvocationError()
                    deadline = (
                        time.monotonic() + command.timeout_seconds + _WRITE_EXECUTION_LEASE_SECONDS
                    )
                    observer_delay = _DUPLICATE_OBSERVER_INITIAL_DELAY_SECONDS
                    while time.monotonic() < deadline:
                        async with self._open_uow() as observer:
                            current = await observer.tool_calls.get(call_id)
                            if current.status in _TERMINAL_TOOL_STATUSES:
                                return await self._named_replay(observer, current, context, policy)
                        await asyncio.sleep(observer_delay)
                        observer_delay = min(
                            observer_delay * 2, _DUPLICATE_OBSERVER_MAX_DELAY_SECONDS
                        )
                    raise ToolInvocationError()
                authorization, error = self._validate(context, request, run, policy)
                if error is None and not await self._execution_context_is_current(context, work):
                    error = (
                        ToolErrorCode.RESOURCE_MISMATCH,
                        "tool execution identity is not current",
                    )
                if error is None and not await self._budget_available(context, policy, work):
                    error = (ToolErrorCode.BUDGET_EXCEEDED, "tool-call budget is exhausted")
                if error is not None:
                    return await self._record_write_denial(
                        work,
                        context,
                        request,
                        run,
                        error,
                        started_at=datetime.now(UTC),
                        started=started,
                    )
                if authorization is None or self._git is None or self._worktree is None:
                    raise ToolInvocationError()
                command = next(
                    item for item in policy.commands if item.name == normalized["command_name"]
                )
                environment = self._named_environment(command.environment_keys)
                payload = {
                    "agent_execution_id": str(_required_uuid(context.agent_execution_id)),
                    "command_digest": command_spec_digest(command),
                    "command_name": command.name,
                    "environment_keys_digest": hashlib.sha256(
                        "\n".join(sorted(environment)).encode()
                    ).hexdigest(),
                    "head_sha": self._git.head_sha(self._worktree),
                    "kind": command.kind.value,
                    "policy_version": context.policy_version,
                    "project_id": str(run.project_id),
                    "protocol_version": 1,
                    "run_id": str(context.run_id),
                    "step_id": str(_required_uuid(context.step_id)),
                    "tool_call_id": str(call_id),
                    "worktree_id": context.worktree_id,
                }
                intent = await work.operations.begin(
                    run_id=context.run_id,
                    operation_type=NAMED_CHECK_KIND,
                    idempotency_key=f"named_check:{call_id}",
                    request_digest=canonical_digest(payload),
                    request_payload=payload,
                    execution_owner=f"forge-named-{uuid4().hex}",
                    execution_lease_seconds=_WRITE_EXECUTION_LEASE_SECONDS,
                )
                if not intent.is_new:
                    raise ToolInvocationError()
                reserved = await work.tool_calls.reserve(
                    ToolCallRecord(
                        id=call_id,
                        run_id=context.run_id,
                        agent_execution_id=_required_uuid(context.agent_execution_id),
                        tool_name=request.name,
                        normalized_arguments=normalized,
                        authorized=True,
                        status=ToolCallStatus.RUNNING,
                        started_at=datetime.now(UTC),
                        step_id=context.step_id,
                        role=context.role,
                        policy_version=context.policy_version,
                        correlation_id=call_id,
                        operation_intent_id=intent.id,
                        request_digest=request_digest,
                        resource_id=context.worktree_id,
                        invocation_schema_version=1,
                    )
                )
                cancellation = NamedCheckCancellation()
                completion = asyncio.create_task(
                    self._settle_named_check(
                        work,
                        context,
                        policy,
                        environment,
                        intent,
                        reserved,
                        request_digest,
                        started,
                        cancellation,
                    ),
                    name=f"forge-named-check-{intent.id}",
                )
                self._named_completions[call_id] = completion
                completion.add_done_callback(
                    lambda _done: self._named_completions.pop(call_id, None)
                )
                return await _await_committed_write(completion, on_cancel=cancellation.request)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep command and persistence failures bounded
            raise ToolInvocationError() from None

    def _named_environment(self, keys: Sequence[str]) -> Mapping[str, str]:
        return MappingProxyType(
            {key: value for key, value in self._command_environment.items() if key in keys}
        )

    def _named_adapter(
        self,
        work: UnitOfWork,
        policy: ProjectPolicy,
        environment: Mapping[str, str],
        cancellation: NamedCheckCancellation | None = None,
    ) -> NamedCheckOperationAdapter:
        if (
            self._worktree is None
            or self._git is None
            or self._runner_factory is None
            or self._artifact_store is None
        ):
            raise ToolInvocationError()
        return NamedCheckOperationAdapter(
            worktree=self._worktree,
            policy=policy,
            controlled_git=self._git,
            runner_factory=self._runner_factory,
            environment=environment,
            artifacts=work.artifacts,
            artifact_store=self._artifact_store,
            cancellation=cancellation,
        )

    async def _settle_named_check(
        self,
        admission: UnitOfWork,
        context: ToolAuthorizationContext,
        policy: ProjectPolicy,
        environment: Mapping[str, str],
        intent: OperationIntent,
        reserved: ToolCallRecord,
        request_digest: str,
        started: float,
        cancellation: NamedCheckCancellation,
    ) -> ToolResult:
        await admission.commit()
        if self._operation_executor is None:
            raise ToolInvocationError()
        # Keep this lock to the admission check.  A bounded runner must never
        # prevent an operator from changing the run while it is executing.
        async with self._open_uow() as preflight:
            run = await preflight.runs.get_for_update(context.run_id)
            current = await preflight.tool_calls.get(reserved.id)
            if current.status is ToolCallStatus.RUNNING and (
                cancellation.requested or run.state is RunState.CANCELLED
            ):
                outcome = await self._named_adapter(
                    preflight, policy, environment, cancellation
                ).cancel_before_launch(intent)
                return await self._complete_named_check(
                    preflight,
                    context,
                    intent,
                    reserved,
                    outcome,
                    request_digest,
                    started,
                    run,
                )
            if (
                current.status is not ToolCallStatus.RUNNING
                or run.state not in _ACTIVE_RUN_STATES[context.role]
                or not await self._execution_context_is_current(context, preflight)
            ):
                raise ToolInvocationError()
            await preflight.commit()
        async with self._open_uow() as execution:
            outcome = await self._operation_executor.invoke_admitted(
                intent, self._named_adapter(execution, policy, environment, cancellation)
            )
            if outcome.status is not OperationStatus.SUCCEEDED:
                raise ToolInvocationError()
            await execution.commit()
        async with self._open_uow() as settlement:
            run = await settlement.runs.get_for_update(context.run_id)
            current = await settlement.tool_calls.get(reserved.id)
            if current.status is not ToolCallStatus.RUNNING:
                raise ToolInvocationError()
            return await self._complete_named_check(
                settlement,
                context,
                intent,
                reserved,
                outcome,
                request_digest,
                started,
                run,
            )

    async def _complete_named_check(
        self,
        work: UnitOfWork,
        context: ToolAuthorizationContext,
        intent: OperationIntent,
        reserved: ToolCallRecord,
        outcome: OperationOutcome,
        request_digest: str,
        started: float,
        run: RunSnapshot,
    ) -> ToolResult:
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise ToolInvocationError()
        result = _named_result(
            reserved, outcome.payload, max(0, int((time.monotonic() - started) * 1000))
        )
        completed_at = datetime.now(UTC)
        await work.operations.complete(intent.id, outcome, owner_id=intent.execution_owner)
        await work.tool_calls.finalize(
            replace(
                reserved,
                status=result.status,
                completed_at=completed_at,
                duration_ms=result.duration_ms,
                artifact_digests=result.artifact_digests,
                result_metadata=_write_record_metadata(
                    result,
                    request_digest,
                    context.worktree_id,
                    started_at=reserved.started_at,
                    completed_at=completed_at,
                    redactor=self._redactor,
                ),
                result_metadata_schema_version=1,
            )
        )
        await work.events.append(_tool_event(result, context, run, reserved.id, authorized=True))
        await work.commit()
        return result

    async def _named_replay(
        self,
        work: UnitOfWork,
        record: ToolCallRecord,
        context: ToolAuthorizationContext,
        policy: ProjectPolicy,
    ) -> ToolResult:
        intent = await work.operations.get(_required_uuid(record.operation_intent_id))
        command = next(
            (
                item
                for item in policy.commands
                if item.name == record.normalized_arguments.get("command_name")
            ),
            None,
        )
        if command is None or intent.idempotency_key != f"named_check:{record.id}":
            raise ToolInvocationError()
        outcome = await self._named_adapter(
            work, policy, self._named_environment(command.environment_keys)
        ).reconcile(intent)
        if (
            intent.status is not OperationStatus.SUCCEEDED
            or outcome.status is not OperationStatus.SUCCEEDED
            or intent.outcome != outcome.payload
        ):
            raise ToolInvocationError()
        result = _named_result(record, outcome.payload, record.duration_ms or 0)
        if record.status is not result.status or record.artifact_digests != result.artifact_digests:
            raise ToolInvocationError()
        await work.rollback()
        return result

    async def _invoke_git_commit(
        self, context: ToolAuthorizationContext, request: ToolRequest
    ) -> ToolResult:
        """Run the two independently admitted phases of a controlled commit.

        The first transaction deliberately contains no Git call.  Its child is
        created before committing that transaction, so cancelling a caller can
        never strand a committed authorization without an owner.
        """
        if self._git is None or self._worktree is None:
            raise ToolInvocationError()
        message = request.arguments.get("message")
        if not _valid_commit_message(message, self._redactor):
            # Validation happens before a Git read or an operation admission.
            async with self._open_uow() as work:
                run = await self._resolve_run(work, context)
                if run is None:
                    raise ToolInvocationError()
                return await self._record_write_denial(
                    work,
                    context,
                    request,
                    run,
                    (ToolErrorCode.INVALID_REQUEST, "commit message contains prohibited content"),
                    started_at=datetime.now(UTC),
                    started=time.monotonic(),
                )
        assert isinstance(message, str)
        executor = self._operation_executor
        if not isinstance(executor, OperationExecutor) or context.invocation_id is None:
            raise ToolInvocationError()
        if self._artifact_store is None:
            raise ToolInvocationError()
        request_digest = canonical_digest(request.arguments)
        call_id = _write_tool_call_id(context.invocation_id)
        normalized: dict[str, object] = {
            "message_digest": hashlib.sha256(message.encode()).hexdigest()
        }
        completion: asyncio.Task[ToolResult] | None = None
        try:
            async with self._open_uow() as work:
                run = await self._resolve_run(work, context)
                policy = await self._resolve_policy(work, run) if run is not None else None
                authorization, error = self._validate(context, request, run, policy)
                if error is None and not await self._execution_context_is_current(context, work):
                    error = (
                        ToolErrorCode.RESOURCE_MISMATCH,
                        "tool execution identity is not current",
                    )
                existing = await work.tool_calls.find(call_id)
                if existing is not None:
                    if not _git_record_matches(existing, context, request_digest, normalized):
                        raise ToolInvocationError()
                    if existing.status in _TERMINAL_TOOL_STATUSES:
                        result = await _git_terminal_result(
                            existing, context, request_digest, work.artifacts, self._artifact_store
                        )
                        await work.rollback()
                        return result
                    # A live duplicate joins its durable owner; never inspect HEAD.
                    completion = self._git_completions.get(call_id)
                    if completion is not None:
                        await work.rollback()
                        return await _await_committed_write(completion)
                    # Only the owner may turn preparation success into a
                    # publication admission.  A duplicate therefore observes
                    # the tool call; generic operation reconciliation cannot
                    # invent publication authority.
                    await work.rollback()
                    deadline = time.monotonic() + _WRITE_EXECUTION_LEASE_SECONDS
                    observer_delay = _DUPLICATE_OBSERVER_INITIAL_DELAY_SECONDS
                    while time.monotonic() < deadline:
                        await asyncio.sleep(observer_delay)
                        observer_delay = min(
                            observer_delay * 2, _DUPLICATE_OBSERVER_MAX_DELAY_SECONDS
                        )
                        async with self._open_uow() as replay_work:
                            terminal = await replay_work.tool_calls.get(call_id)
                            if terminal.status in _TERMINAL_TOOL_STATUSES:
                                result = await _git_terminal_result(
                                    terminal,
                                    context,
                                    request_digest,
                                    replay_work.artifacts,
                                    self._artifact_store,
                                )
                                await replay_work.rollback()
                                return result
                            owner = await replay_work.operations.get_by_idempotency_key(
                                f"git.commit:{call_id}:publish"
                            )
                            if owner is None:
                                owner = await replay_work.operations.get(
                                    _required_uuid(terminal.operation_intent_id)
                                )
                            if (
                                owner.run_id == context.run_id
                                and owner.execution_owner is not None
                                and owner.execution_lease_expires_at is not None
                            ):
                                remaining = (
                                    owner.execution_lease_expires_at - datetime.now(UTC)
                                ).total_seconds()
                                deadline = max(deadline, time.monotonic() + remaining)
                            await replay_work.rollback()
                    raise ToolInvocationError()
                if error is None and (
                    policy is None or not await self._budget_available(context, policy, work)
                ):
                    error = (ToolErrorCode.BUDGET_EXCEEDED, "tool-call budget is exhausted")
                if error is not None:
                    if run is None:
                        raise ToolInvocationError()
                    return await self._record_write_denial(
                        work,
                        context,
                        request,
                        run,
                        error,
                        started_at=datetime.now(UTC),
                        started=time.monotonic(),
                    )
                if authorization is None or run is None:
                    raise ToolInvocationError()
                # This is the sole pre-effect HEAD observation, retained in the request.
                previous_sha = self._git.head_sha(self._worktree)
                payload = _git_prepare_payload(context, self._worktree, message, request_digest)
                intent = await work.operations.begin(
                    run_id=context.run_id,
                    operation_type=PREPARE_GIT_COMMIT_KIND,
                    idempotency_key=f"git.commit:{call_id}:prepare",
                    request_digest=canonical_digest(payload),
                    request_payload=payload,
                    execution_owner=f"forge-git-{uuid4().hex}",
                    execution_lease_seconds=_WRITE_EXECUTION_LEASE_SECONDS,
                )
                # The adapter validates previous SHA through Git's prepared result; retain
                # the pre-admission observation only as evidence, never as authority.
                del previous_sha
                reserved = await work.tool_calls.reserve(
                    ToolCallRecord(
                        id=call_id,
                        run_id=context.run_id,
                        agent_execution_id=_required_uuid(context.agent_execution_id),
                        tool_name=ToolName.GIT_COMMIT,
                        normalized_arguments=normalized,
                        authorized=True,
                        status=ToolCallStatus.RUNNING,
                        started_at=datetime.now(UTC),
                        step_id=context.step_id,
                        role=context.role,
                        policy_version=context.policy_version,
                        correlation_id=call_id,
                        operation_intent_id=intent.id,
                        arguments_schema_version=1,
                        request_digest=request_digest,
                        resource_id=context.worktree_id,
                        invocation_schema_version=1,
                    )
                )
                completion = asyncio.create_task(
                    self._settle_git_commit(
                        work,
                        context,
                        request,
                        call_id,
                        normalized,
                        request_digest,
                        intent,
                        reserved,
                        message,
                        executor,
                    ),
                    name=f"forge-git-commit-{intent.id}",
                )
                self._git_completions[call_id] = completion
                completion.add_done_callback(lambda _done: self._git_completions.pop(call_id, None))
                return await _await_committed_write(completion)
        except asyncio.CancelledError:
            if completion is not None:
                await _await_committed_write(completion, caller_cancelled=True)
            raise
        except Exception:  # noqa: BLE001 - phase rollback errors are a stable public failure
            raise ToolInvocationError() from None

    async def _settle_git_commit(
        self,
        work: UnitOfWork,
        context: ToolAuthorizationContext,
        request: ToolRequest,
        call_id: UUID,
        normalized: dict[str, object],
        request_digest: str,
        preparation: OperationIntent,
        reserved: ToolCallRecord,
        message: str,
        executor: OperationExecutor,
    ) -> ToolResult:
        await work.commit()
        if self._git is None or self._worktree is None:
            raise ToolInvocationError()
        prepare_adapter = PrepareGitCommitAdapter(self._git, self._worktree)
        outcome = await executor.invoke_admitted(preparation, prepare_adapter)
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise ToolInvocationError()
        # Persist successful preparation and publication admission atomically.
        async with self._open_uow() as phase_work:
            run = await phase_work.runs.get_for_update(context.run_id)
            current = await phase_work.tool_calls.get(call_id)
            if current.status is ToolCallStatus.RUNNING and run.state is RunState.CANCELLED:
                return await self._finalize_git_commit(
                    context,
                    request,
                    call_id,
                    normalized,
                    request_digest,
                    preparation,
                    None,
                    reserved,
                    outcome,
                    settlement_work=phase_work,
                )
            if (
                current.status is not ToolCallStatus.RUNNING
                or run.state not in _ACTIVE_RUN_STATES[context.role]
                or not await self._execution_context_is_current(context, phase_work)
            ):
                raise ToolInvocationError()
            receipt = thaw_payload(outcome.payload)
            payload = _git_publish_payload(
                context, self._worktree, message, request_digest, preparation.id, receipt
            )
            publication = await phase_work.operations.begin(
                run_id=context.run_id,
                operation_type=PUBLISH_GIT_COMMIT_KIND,
                idempotency_key=f"git.commit:{call_id}:publish",
                request_digest=canonical_digest(payload),
                request_payload=payload,
                execution_owner=f"forge-git-{uuid4().hex}",
                execution_lease_seconds=_WRITE_EXECUTION_LEASE_SECONDS,
            )
            await phase_work.operations.complete(
                preparation.id, outcome, owner_id=preparation.execution_owner
            )
            await phase_work.commit()
        async with self._open_uow() as reader_work:
            publish_adapter = PublishGitCommitAdapter(
                self._git, self._worktree, reader_work.operations
            )
            # invoke_admitted uses the separate executor repository; receipt reads are
            # complete before this UoW exits and operation repository is session-bound.
            result_outcome = await executor.invoke_admitted(publication, publish_adapter)
            if result_outcome.status is not OperationStatus.SUCCEEDED:
                raise ToolInvocationError()
        return await self._finalize_git_commit(
            context,
            request,
            call_id,
            normalized,
            request_digest,
            preparation,
            publication,
            reserved,
            result_outcome,
        )

    async def _finalize_git_commit(
        self,
        context: ToolAuthorizationContext,
        request: ToolRequest,
        call_id: UUID,
        normalized: dict[str, object],
        request_digest: str,
        preparation: OperationIntent,
        publication: OperationIntent | None,
        reserved: ToolCallRecord,
        outcome: OperationOutcome,
        *,
        settlement_work: UnitOfWork | None = None,
    ) -> ToolResult:
        if self._artifact_store is None:
            raise ToolInvocationError()
        cancelled = publication is None
        publication_id = None if publication is None else publication.id
        status = ToolCallStatus.CANCELLED if cancelled else ToolCallStatus.SUCCEEDED
        metadata = (
            {
                "publication_disposition": "cancelled_before_admission",
                "request_digest": request_digest,
                "worktree_id": context.worktree_id,
            }
            if publication is None
            else _git_result_metadata(outcome.payload, publication.id, context, request_digest)
        )
        artifact = _git_result_artifact_bytes(
            call_id, preparation.id, publication_id, request_digest, status, metadata
        )
        descriptor = await self._artifact_store.put_bytes(
            artifact,
            media_type="application/json",
            max_bytes=_WRITE_RESULT_ARTIFACT_MAX_BYTES,
            bounding_policy="head_tail",
        )
        if await self._artifact_store.verify(descriptor.digest) is not True:
            raise ToolInvocationError()
        async with (
            self._open_uow() if settlement_work is None else nullcontext(settlement_work)
        ) as work:
            run = await work.runs.get_for_update(context.run_id)
            terminal_operation = preparation if publication is None else publication
            await work.operations.complete(
                terminal_operation.id, outcome, owner_id=terminal_operation.execution_owner
            )
            result = ToolResult(
                tool_name=ToolName.GIT_COMMIT,
                status=status,
                error=(
                    ToolError(
                        code=ToolErrorCode.CANCELLED, message="run cancelled before Git publication"
                    )
                    if cancelled
                    else None
                ),
                metadata=metadata,
                artifact_digests=(descriptor.digest,),
                tool_call_id=call_id,
                operation_intent_id=preparation.id,
                correlation_id=call_id,
                agent_execution_id=context.agent_execution_id,
                step_id=context.step_id,
                duration_ms=0,
            )
            completed = datetime.now(UTC)
            record = ToolCallRecord(
                id=call_id,
                run_id=context.run_id,
                agent_execution_id=_required_uuid(context.agent_execution_id),
                tool_name=ToolName.GIT_COMMIT,
                normalized_arguments=normalized,
                authorized=True,
                status=status,
                started_at=reserved.started_at,
                completed_at=completed,
                result_metadata=_write_record_metadata(
                    result,
                    request_digest,
                    context.worktree_id,
                    started_at=reserved.started_at,
                    completed_at=completed,
                    redactor=self._redactor,
                ),
                step_id=context.step_id,
                role=context.role,
                policy_version=context.policy_version,
                duration_ms=0,
                artifact_digests=(descriptor.digest,),
                correlation_id=call_id,
                operation_intent_id=preparation.id,
                arguments_schema_version=1,
                result_metadata_schema_version=1,
                request_digest=request_digest,
                resource_id=context.worktree_id,
                invocation_schema_version=1,
            )
            await work.artifacts.record(
                descriptor,
                run_id=context.run_id,
                producer_type="controlled_tool",
                producer_id=call_id,
                metadata={
                    "operation_intent_id": str(preparation.id),
                    "publication_intent_id": None if cancelled else str(publication_id),
                    "producer_id": str(call_id),
                    "request_digest": request_digest,
                    "resource_id": context.worktree_id,
                    "result_schema_version": 1,
                    "tool_name": ToolName.GIT_COMMIT.value,
                    "invocation_schema_version": 1,
                },
            )
            await work.tool_calls.finalize(record)
            await work.events.append(_tool_event(result, context, run, call_id, authorized=True))
            await work.commit()
            return result

    async def _resolve_run(
        self,
        work: UnitOfWork,
        context: ToolAuthorizationContext,
    ) -> RunSnapshot | None:
        try:
            candidate = await work.runs.get_for_update(context.run_id)
        except Exception:  # noqa: BLE001 - mapping failures are handled as validation denial
            return None
        return candidate if isinstance(candidate, RunSnapshot) else None

    async def _resolve_policy(
        self,
        work: UnitOfWork,
        run: RunSnapshot,
    ) -> ProjectPolicy | None:
        if run is None or run.policy_version is None:
            return None
        try:
            project = await work.projects.get(run.project_id, for_update=True)
        except Exception:  # noqa: BLE001 - mapping failures become safe denials
            return None
        if (
            project.id != run.project_id
            or project.current_policy_version != run.policy_version
            or project.policy is None
            or project.policy.project_id != run.project_id
            or project.policy.version != run.policy_version
        ):
            return None
        return _policy_from_record(project)

    async def _execution_context_is_current(
        self,
        context: ToolAuthorizationContext,
        work: UnitOfWork,
    ) -> bool:
        execution_id = context.agent_execution_id
        step_id = context.step_id
        if (
            not isinstance(execution_id, UUID)
            or execution_id.int == 0
            or not isinstance(step_id, UUID)
            or step_id.int == 0
        ):
            return False
        try:
            return await work.tool_calls.validate_execution_context(
                context.run_id,
                execution_id,
                step_id,
                context.role,
            )
        except Exception:  # noqa: BLE001 - failure to prove lineage fails closed
            return False

    def _validate(
        self,
        context: ToolAuthorizationContext,
        request: ToolRequest,
        run: RunSnapshot | None,
        policy: ProjectPolicy | None,
    ) -> tuple[ToolAuthorization | None, tuple[ToolErrorCode, str] | None]:
        if context.agent_execution_id is None or context.step_id is None:
            return None, (ToolErrorCode.INVALID_REQUEST, "tool execution identity is required")
        try:
            authorization = self._authorizer.authorize(context, request)
        except ToolAuthorizationDenied, TypeError, ValueError:
            return None, (ToolErrorCode.AUTHORIZATION_DENIED, "tool authorization denied")
        if run is None or run.id != context.run_id:
            return None, (ToolErrorCode.RESOURCE_MISMATCH, "tool run identity is not current")
        if run.policy_version is None or run.policy_version != context.policy_version:
            return None, (ToolErrorCode.POLICY_MISMATCH, "tool policy version is not current")
        if policy is None or policy.version != context.policy_version:
            return None, (ToolErrorCode.POLICY_MISMATCH, "tool policy version is not current")
        if run.state not in _ACTIVE_RUN_STATES[context.role]:
            return None, (ToolErrorCode.RUN_NOT_ACTIVE, "run is not active for this tool role")
        planner_repository_read = (
            context.role is AgentRole.PLANNER
            and request.name in _REPOSITORY_READS
            and run.worktree_path is None
            and run.state is RunState.PLANNING
        )
        if planner_repository_read:
            if not self._repository_resource_binding_matches(context, run, policy):
                return None, (
                    ToolErrorCode.RESOURCE_MISMATCH,
                    "canonical repository binding is not current",
                )
        else:
            if run.worktree_path is None:
                return None, (
                    ToolErrorCode.RESOURCE_MISMATCH,
                    "managed worktree identity is unavailable",
                )
            if not self._resource_binding_matches(context, run, policy):
                return None, (
                    ToolErrorCode.RESOURCE_MISMATCH,
                    "managed worktree path is not current",
                )
        if request.name is ToolName.REPOSITORY_WRITE_FILE:
            writer = self._repository_writer
            artifact_store = self._artifact_store
            executor = self._operation_executor
            if (
                writer is None
                or artifact_store is None
                or not isinstance(executor, OperationExecutor)
                or not callable(getattr(writer, "write_file", None))
                or not callable(getattr(writer, "inspect_file", None))
                or not callable(getattr(writer, "is_bound_to", None))
                or not callable(getattr(artifact_store, "put_bytes", None))
                or not callable(getattr(artifact_store, "verify", None))
            ):
                return None, (
                    ToolErrorCode.TOOL_UNAVAILABLE,
                    "controlled tool adapter is unavailable",
                )
            bound = self._bound_worktree(run, policy)
            controlled_git = self._git
            if bound is None or controlled_git is None or self._repository_reader is None:
                return None, (
                    ToolErrorCode.RESOURCE_MISMATCH,
                    "repository writer binding is not current",
                )
            try:
                binding_matches = writer.is_bound_to(controlled_git, bound, policy)
            except Exception:  # noqa: BLE001 - adapter authority checks fail closed
                binding_matches = False
            if binding_matches is not True or not self._reader_binding_matches(run.worktree_path):
                return None, (
                    ToolErrorCode.RESOURCE_MISMATCH,
                    "repository writer binding is not current",
                )
            if not self._path_argument_is_valid(request, allow_root=False):
                return None, (ToolErrorCode.INVALID_REQUEST, "tool path is not applicable")
            if self._write_request_values(request) is None:
                return None, (ToolErrorCode.INVALID_REQUEST, "tool content is not applicable")
            return authorization, None
        if request.name is ToolName.GIT_COMMIT:
            if (
                self._git is None
                or self._worktree is None
                or self._artifact_store is None
                or not isinstance(self._operation_executor, OperationExecutor)
                or not callable(getattr(self._git, "prepare_commit", None))
                or not callable(getattr(self._git, "commit_prepared", None))
                or not callable(getattr(self._git, "head_sha", None))
            ):
                return None, (
                    ToolErrorCode.TOOL_UNAVAILABLE,
                    "controlled tool adapter is unavailable",
                )
            return authorization, None
        if request.name in {
            ToolName.VALIDATION_RESULTS_READ,
            ToolName.REVIEW_ARTIFACTS_READ,
        }:
            if (
                self._bound_worktree(run, policy) is None
                or self._git is None
                or self._evidence_reader is None
                or not callable(getattr(self._git, "head_sha", None))
            ):
                return None, (
                    ToolErrorCode.TOOL_UNAVAILABLE,
                    "controlled tool adapter is unavailable",
                )
            return authorization, None
        if request.name is ToolName.BUILD_RUN_NAMED_CHECK:
            if (
                self._runner_factory is None
                or self._operation_executor is None
                or self._artifact_store is None
                or self._git is None
                or self._bound_worktree(run, policy) is None
            ):
                return None, (
                    ToolErrorCode.TOOL_UNAVAILABLE,
                    "controlled tool adapter is unavailable",
                )
            if not any(
                command.name == request.arguments.get("command_name") for command in policy.commands
            ):
                return None, (
                    ToolErrorCode.INVALID_REQUEST,
                    "named check is not in the project policy",
                )
            return authorization, None
        if request.name in _UNAVAILABLE_TOOLS:
            return None, (ToolErrorCode.TOOL_UNAVAILABLE, "controlled tool adapter is unavailable")
        if request.name in _READ_TOOLS - {ToolName.GIT_STATUS, ToolName.GIT_DIFF}:
            if self._repository_reader is None:
                return None, (
                    ToolErrorCode.TOOL_UNAVAILABLE,
                    "controlled tool adapter is unavailable",
                )
            expected_path = policy.repository_path if planner_repository_read else run.worktree_path
            if not self._reader_binding_matches(expected_path):
                return None, (
                    ToolErrorCode.RESOURCE_MISMATCH,
                    "repository reader binding is not current",
                )
            allow_root = request.name in {
                ToolName.REPOSITORY_LIST_FILES,
                ToolName.REPOSITORY_SEARCH,
                ToolName.REPOSITORY_READ_INSTRUCTIONS,
            }
            if not self._path_argument_is_valid(request, allow_root=allow_root):
                return None, (ToolErrorCode.INVALID_REQUEST, "tool path is not applicable")
        if request.name in {ToolName.GIT_STATUS, ToolName.GIT_DIFF}:
            if self._git is None or self._bound_worktree(run, policy) is None:
                return None, (
                    ToolErrorCode.RESOURCE_MISMATCH,
                    "controlled Git binding is not current",
                )
            method_name = request.name.value.rsplit(".", 1)[-1]
            if request.name is ToolName.GIT_DIFF and request.arguments.get("scope") == "candidate":
                method_name = "candidate_diff"
            if not callable(getattr(self._git, method_name, None)):
                return None, (
                    ToolErrorCode.TOOL_UNAVAILABLE,
                    "controlled tool adapter is unavailable",
                )
        if request.name not in _READ_TOOLS and request.name not in _UNAVAILABLE_TOOLS:
            return None, (
                ToolErrorCode.TOOL_UNAVAILABLE,
                "controlled tool adapter is unavailable",
            )
        return authorization, None

    def _resource_binding_matches(
        self,
        context: ToolAuthorizationContext,
        run: RunSnapshot,
        policy: ProjectPolicy,
    ) -> bool:
        expected = self._expected_identity(run, policy)
        if expected is None or context.worktree_id != expected.worktree_name:
            return False
        expected_path = _canonical_path(run.worktree_path)
        if expected_path is None:
            return False
        worktree = self._worktree
        if not isinstance(worktree, ManagedWorktree):
            return False
        return (
            worktree.identity == expected
            and worktree.base_sha == run.base_sha
            and _canonical_path(worktree.path) == expected_path
        )

    def _expected_identity(
        self, run: RunSnapshot, policy: ProjectPolicy
    ) -> WorktreeIdentity | None:
        if (
            run.branch_name is None
            or not isinstance(run.base_sha, str)
            or _BASE_SHA.fullmatch(run.base_sha) is None
        ):
            return None
        try:
            return WorktreeIdentity.for_run(
                run.project_id,
                run.id,
                run.branch_name,
                policy.database.enabled,
            )
        except TypeError, ValueError:
            return None

    def _repository_resource_binding_matches(
        self,
        context: ToolAuthorizationContext,
        run: RunSnapshot,
        policy: ProjectPolicy,
    ) -> bool:
        try:
            expected_identity = repository_resource_identity(run.project_id)
        except TypeError, ValueError:
            return False
        return (
            context.worktree_id == expected_identity
            and self._reader_binding_matches(policy.repository_path)
            and self._reader_exclusions_cover(policy.effective_secret_paths)
        )

    def _reader_exclusions_cover(self, paths: Sequence[str]) -> bool:
        reader = self._repository_reader
        if reader is None:
            return False
        try:
            excludes_paths = reader.excludes_paths
            return excludes_paths(paths) is True
        except AttributeError, TypeError, ValueError, RuntimeError, OSError:
            return False

    def _reader_binding_matches(self, expected_path: str | None) -> bool:
        reader = self._repository_reader
        expected = _canonical_path(expected_path)
        if reader is None or expected is None:
            return False
        try:
            root = reader.root
            root_path = root.path
        except AttributeError, TypeError, ValueError:
            return False
        return _canonical_path(root_path) == expected

    def _bound_worktree(self, run: RunSnapshot, policy: ProjectPolicy) -> ManagedWorktree | None:
        git = self._git
        expected_identity = self._expected_identity(run, policy)
        if git is None or expected_identity is None or run.worktree_path is None:
            return None
        try:
            repository_path = git.repository_path
            expected_repository = _canonical_path(policy.repository_path)
            if (
                expected_repository is None
                or _canonical_path(repository_path) != expected_repository
            ):
                return None
            expected_worktree = git.expected_worktree
            if not callable(expected_worktree):
                return None
            base_sha = run.base_sha
            if not isinstance(base_sha, str):
                return None
            candidate = expected_worktree(expected_identity, base_sha)
        except AttributeError, TypeError, ValueError, RuntimeError, OSError:
            return None
        if not isinstance(candidate, ManagedWorktree):
            return None
        selected = self._worktree if self._worktree is not None else candidate
        if not isinstance(selected, ManagedWorktree):
            return None
        expected_path = _canonical_path(run.worktree_path)
        if expected_path is None or _canonical_path(candidate.path) != expected_path:
            return None
        if candidate.identity != expected_identity or candidate.base_sha != run.base_sha:
            return None
        if (
            selected.identity != expected_identity
            or selected.base_sha != run.base_sha
            or _canonical_path(selected.path) != expected_path
        ):
            return None
        return selected

    def _path_argument_is_valid(self, request: ToolRequest, *, allow_root: bool) -> bool:
        key = "target_path" if request.name is ToolName.REPOSITORY_READ_INSTRUCTIONS else "path"
        value = request.arguments.get(key, ".")
        if not isinstance(value, str):
            return False
        reader = self._repository_reader
        if reader is None:
            return False
        root = reader.root
        try:
            if root.contains(value, allow_root=allow_root) is not True:
                return False
            normalized = root.normalize(value, allow_root=allow_root)
        except TypeError, ValueError, RuntimeError, OSError:
            return False
        return isinstance(normalized, str) and (allow_root or normalized != ".")

    def _normalized_arguments(self, request: ToolRequest) -> dict[str, object]:
        """Canonicalize path arguments before redacting tool-call evidence."""

        if request.name is ToolName.REPOSITORY_WRITE_FILE:
            return self._normalized_write_arguments(request)
        values = dict(request.arguments)
        key = "target_path" if request.name is ToolName.REPOSITORY_READ_INSTRUCTIONS else "path"
        if key not in values:
            return _safe_metadata(values, redactor=self._redactor)
        reader = self._repository_reader
        path_value = values[key]
        if reader is not None and isinstance(path_value, str):
            allow_root = request.name in {
                ToolName.REPOSITORY_LIST_FILES,
                ToolName.REPOSITORY_SEARCH,
                ToolName.REPOSITORY_READ_INSTRUCTIONS,
            }
            try:
                values[key] = reader.root.normalize(path_value, allow_root=allow_root)
            except Exception:  # noqa: BLE001 - path validation already controls dispatch
                return _safe_metadata(values, redactor=self._redactor)
        return _safe_metadata(values, redactor=self._redactor)

    def _write_request_values(self, request: ToolRequest) -> _PreparedWrite | None:
        if request.name is not ToolName.REPOSITORY_WRITE_FILE:
            return None
        path = request.arguments.get("path")
        content = request.arguments.get("content")
        reader = self._repository_reader
        if not isinstance(path, str) or not isinstance(content, str) or reader is None:
            return None
        if "\x00" in content:
            return None
        try:
            encoded = content.encode("utf-8", errors="strict")
            normalized = reader.root.normalize(path, allow_root=False)
        except AttributeError, OSError, RuntimeError, TypeError, UnicodeError, ValueError:
            return None
        if not normalized or normalized == "." or len(encoded) > MAX_REPOSITORY_WRITE_BYTES:
            return None
        return _PreparedWrite(
            path=normalized,
            content=content,
            content_digest=hashlib.sha256(encoded).hexdigest(),
            byte_count=len(encoded),
        )

    def _normalized_write_arguments(self, request: ToolRequest) -> dict[str, object]:
        prepared = self._write_request_values(request)
        if prepared is not None:
            return {
                "path": prepared.path,
                "content_digest": prepared.content_digest,
                "content_byte_count": prepared.byte_count,
            }
        evidence: dict[str, object] = {"content_valid": False}
        path = request.arguments.get("path")
        if isinstance(path, str):
            evidence["path"] = path
        content = request.arguments.get("content")
        if isinstance(content, str) and "\x00" not in content:
            try:
                encoded = content.encode("utf-8", errors="strict")
            except UnicodeError:
                pass
            else:
                evidence["content_digest"] = hashlib.sha256(encoded).hexdigest()
                evidence["content_byte_count"] = len(encoded)
        return _safe_metadata(evidence, redactor=self._redactor)

    async def _budget_available(
        self,
        context: ToolAuthorizationContext,
        policy: ProjectPolicy,
        work: UnitOfWork,
    ) -> bool:
        model = {
            AgentRole.PLANNER: policy.planner_model,
            AgentRole.DEVELOPER: policy.developer_model,
            AgentRole.REVIEWER: policy.reviewer_model,
        }[context.role]
        limit = model.max_tool_calls
        if limit < 1:
            return False
        execution_id = context.agent_execution_id
        if not isinstance(execution_id, UUID) or execution_id.int == 0:
            return False
        try:
            count = await work.tool_calls.count_for_execution(execution_id)
            return type(count) is int and count < limit
        except Exception:  # noqa: BLE001 - failure to count fails closed
            return False

    async def _dispatch(
        self,
        authorization: ToolAuthorization,
    ) -> ToolResult:
        name = authorization.tool_name
        try:
            if name is ToolName.REPOSITORY_LIST_FILES:
                reader = self._repository_reader
                if reader is None:
                    raise ToolInvocationError()
                entries = reader.list_files(_argument_text(authorization, "path", "."))
                return self._result(
                    name, ToolCallStatus.SUCCEEDED, metadata={"entries": _entries(entries)}
                )
            if name is ToolName.REPOSITORY_READ_FILE:
                reader = self._repository_reader
                if reader is None:
                    raise ToolInvocationError()
                read = reader.read_file(_argument_text(authorization, "path"))
                return self._result(name, ToolCallStatus.SUCCEEDED, metadata=_file_read(read))
            if name is ToolName.REPOSITORY_SEARCH:
                reader = self._repository_reader
                if reader is None:
                    raise ToolInvocationError()
                matches = reader.search(
                    _argument_text(authorization, "literal"),
                    _argument_text(authorization, "path", "."),
                )
                return self._result(
                    name, ToolCallStatus.SUCCEEDED, metadata={"matches": _matches(matches)}
                )
            if name is ToolName.REPOSITORY_READ_INSTRUCTIONS:
                reader = self._repository_reader
                if reader is None:
                    raise ToolInvocationError()
                documents = reader.read_instructions(
                    _argument_text(authorization, "target_path", ".")
                )
                return self._result(
                    name,
                    ToolCallStatus.SUCCEEDED,
                    metadata={"documents": _instructions(documents)},
                )
            if name is ToolName.GIT_STATUS:
                worktree = self._worktree
                if worktree is None or self._git is None:
                    raise ToolInvocationError()
                status = self._git.status(worktree)
                return self._result(name, ToolCallStatus.SUCCEEDED, metadata=_git_output(status))
            if name is ToolName.GIT_DIFF:
                worktree = self._worktree
                if worktree is None or self._git is None:
                    raise ToolInvocationError()
                if authorization.request.arguments.get("scope") == "candidate":
                    candidate = self._git.candidate_diff(worktree)
                    metadata = _git_output(candidate.diff)
                    metadata.update(
                        head_sha=candidate.head_sha,
                        diff_digest=hashlib.sha256(candidate.diff.text.encode("utf-8")).hexdigest(),
                        changed_paths=candidate.changed_paths,
                        untrusted_repository_content=True,
                    )
                    return self._result(name, ToolCallStatus.SUCCEEDED, metadata=metadata)
                diff = self._git.diff(worktree)
                return self._result(name, ToolCallStatus.SUCCEEDED, metadata=_git_output(diff))
            if name in {
                ToolName.VALIDATION_RESULTS_READ,
                ToolName.REVIEW_ARTIFACTS_READ,
            }:
                if self._evidence_reader is None or self._git is None or self._worktree is None:
                    raise ToolInvocationError()
                purpose = (
                    EvidenceInputPurpose.VALIDATION_RESULTS
                    if name is ToolName.VALIDATION_RESULTS_READ
                    else EvidenceInputPurpose.PRIOR_REVIEW
                )
                scope = EvidenceReadScope(
                    authorization.context.run_id,
                    authorization.context.policy_version,
                    _required_uuid(authorization.context.agent_execution_id),
                    _required_uuid(authorization.context.step_id),
                    self._git.head_sha(self._worktree),
                )
                manifest = await self._evidence_reader.read(purpose, scope)
                if manifest is None:
                    if purpose is EvidenceInputPurpose.VALIDATION_RESULTS:
                        return self._result(
                            name,
                            ToolCallStatus.FAILED,
                            ToolError(
                                code=ToolErrorCode.ADAPTER_ERROR,
                                message="required validation evidence is unavailable",
                            ),
                        )
                    return self._result(name, ToolCallStatus.SUCCEEDED, metadata={"evidence": None})
                return self._result(
                    name,
                    ToolCallStatus.SUCCEEDED,
                    metadata=manifest.model_dump(mode="json"),
                    artifact_digests=(evidence_manifest_digest(manifest),),
                )
            raise ToolInvocationError()
        except asyncio.CancelledError:
            raise
        except ToolInvocationError:
            raise
        except Exception:  # noqa: BLE001 - adapters cross one safe public category
            return self._result(
                name,
                ToolCallStatus.FAILED,
                ToolError(
                    code=ToolErrorCode.ADAPTER_ERROR, message="controlled tool adapter failed"
                ),
            )

    def _open_uow(self) -> UnitOfWork:
        return self._unit_of_work_factory()

    def _result(
        self,
        tool_name: ToolName,
        status: ToolCallStatus,
        error: ToolError | None = None,
        *,
        metadata: Mapping[str, object] | None = None,
        artifact_digests: tuple[str, ...] = (),
    ) -> ToolResult:
        return _new_result(
            tool_name,
            status,
            error,
            metadata=metadata,
            artifact_digests=artifact_digests,
            redactor=self._redactor,
        )


class _RepositoryWriteOperationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("repository write operation failed")


def _named_result(
    record: ToolCallRecord, payload: Mapping[str, object], duration_ms: int
) -> ToolResult:
    receipt = payload.get("receipt_digest")
    if not isinstance(receipt, str) or payload.get("tool_call_id") != str(record.id):
        raise ToolInvocationError()
    if payload.get("disposition") == "cancelled_before_launch":
        if set(payload) != {"disposition", "receipt_digest", "tool_call_id"}:
            raise ToolInvocationError()
        return ToolResult(
            tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
            status=ToolCallStatus.CANCELLED,
            error=ToolError(
                code=ToolErrorCode.CANCELLED, message="named check cancelled before launch"
            ),
            metadata=_safe_metadata(payload),
            artifact_digests=(receipt,),
            tool_call_id=record.id,
            operation_intent_id=record.operation_intent_id,
            correlation_id=record.id,
            agent_execution_id=record.agent_execution_id,
            step_id=record.step_id,
            duration_ms=duration_ms,
        )
    cancelled = payload.get("caller_cancelled") is True
    timed_out = payload.get("timed_out") is True
    failed = timed_out or payload.get("exit_code") != 0
    status = (
        ToolCallStatus.CANCELLED
        if cancelled
        else (ToolCallStatus.FAILED if failed else ToolCallStatus.SUCCEEDED)
    )
    error = None
    if cancelled:
        error = ToolError(code=ToolErrorCode.CANCELLED, message="named check cancelled")
    elif failed:
        error = ToolError(
            code=ToolErrorCode.ADAPTER_ERROR,
            message="named check timed out" if timed_out else "named check failed",
        )
    return ToolResult(
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        status=status,
        error=error,
        metadata=_safe_metadata(payload),
        artifact_digests=(receipt,),
        tool_call_id=record.id,
        operation_intent_id=record.operation_intent_id,
        correlation_id=record.id,
        agent_execution_id=record.agent_execution_id,
        step_id=record.step_id,
        duration_ms=duration_ms,
    )


def _valid_commit_message(value: object, redactor: Redactor) -> bool:
    """Validate the exact public commit schema before Git or persistence reads."""

    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 4096:
        return False
    if (
        "\r" in value
        or "\n" in value
        or any(char == "\x7f" or unicodedata.category(char) in {"Cc", "Cf"} for char in value)
    ):
        return False
    # A detector changing even one byte means the original must never cross a
    # durable boundary.  This also catches registered literal secrets.
    redacted = redactor.redact(value)
    return isinstance(redacted, str) and redacted == value


def _git_prepare_payload(
    context: ToolAuthorizationContext, worktree: ManagedWorktree, message: str, request_digest: str
) -> dict[str, object]:
    return {
        "agent_execution_id": str(_required_uuid(context.agent_execution_id)),
        "base_sha": worktree.base_sha,
        "message": message,
        "message_digest": hashlib.sha256(message.encode()).hexdigest(),
        "policy_version": context.policy_version,
        "request_digest": request_digest,
        "run_id": str(context.run_id),
        "step_id": str(_required_uuid(context.step_id)),
        "tool_call_id": str(_write_tool_call_id(_required_uuid(context.invocation_id))),
        "worktree_id": context.worktree_id,
    }


def _git_publish_payload(
    context: ToolAuthorizationContext,
    worktree: ManagedWorktree,
    message: str,
    request_digest: str,
    preparation_id: UUID,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    payload = _git_prepare_payload(context, worktree, message, request_digest)
    payload.update(
        preparation_intent_id=str(preparation_id),
        previous_sha=receipt["previous_sha"],
        tree_sha=receipt["tree_sha"],
    )
    return payload


def _git_record_matches(
    record: ToolCallRecord,
    context: ToolAuthorizationContext,
    request_digest: str,
    normalized: Mapping[str, object],
) -> bool:
    return (
        record.tool_name is ToolName.GIT_COMMIT
        and record.authorized
        and record.run_id == context.run_id
        and record.agent_execution_id == context.agent_execution_id
        and record.step_id == context.step_id
        and record.role is context.role
        and record.policy_version == context.policy_version
        and record.resource_id == context.worktree_id
        and record.request_digest == request_digest
        and record.invocation_schema_version == 1
        and record.operation_intent_id is not None
        and canonical_payload(record.normalized_arguments) == canonical_payload(normalized)
    )


async def _git_terminal_result(
    record: ToolCallRecord,
    context: ToolAuthorizationContext,
    request_digest: str,
    artifacts: ArtifactRepository,
    artifact_store: ArtifactStore | None,
) -> ToolResult:
    """Return durable terminal evidence without reading repository state."""

    if (
        record.status not in {ToolCallStatus.SUCCEEDED, ToolCallStatus.CANCELLED}
        or record.result_metadata is None
    ):
        raise ToolInvocationError()
    cancelled = record.status is ToolCallStatus.CANCELLED
    publication = record.result_metadata.get("publication_intent_id")
    if not cancelled and not isinstance(publication, str):
        raise ToolInvocationError()
    if not cancelled:
        try:
            if UUID(str(publication)).int == 0:
                raise ValueError
        except ValueError:
            raise ToolInvocationError() from None
    elif publication is not None:
        raise ToolInvocationError()
    fields = (
        {"publication_disposition", "request_digest", "worktree_id"}
        if cancelled
        else _GIT_RESULT_FIELDS
    )
    metadata = {
        key: thaw_payload(record.result_metadata[key])
        for key in fields
        if key in record.result_metadata
    }
    if set(metadata) != fields or metadata["request_digest"] != request_digest:
        raise ToolInvocationError()
    if cancelled and (
        metadata["publication_disposition"] != "cancelled_before_admission"
        or metadata["worktree_id"] != context.worktree_id
    ):
        raise ToolInvocationError()
    if artifact_store is None or len(record.artifact_digests) != 1:
        raise ToolInvocationError()
    digest = record.artifact_digests[0]
    try:
        descriptor = await artifacts.get_by_digest(digest, run_id=context.run_id)
        if (
            descriptor.media_type != "application/json"
            or descriptor.schema_version != 1
            or descriptor.truncated is not False
            or descriptor.original_byte_count != descriptor.byte_count
            or descriptor.byte_count > _WRITE_RESULT_ARTIFACT_MAX_BYTES
        ):
            raise ToolInvocationError()
        blob = await artifact_store.open_bytes(digest)
        artifact = json.loads(blob.decode("utf-8"))
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        raise ToolInvocationError() from None
    if (
        descriptor.digest != digest
        or descriptor.run_id != context.run_id
        or descriptor.producer_id != record.id
        or descriptor.producer_type != "controlled_tool"
        or descriptor.byte_count != len(blob)
        or hashlib.sha256(blob).hexdigest() != digest
        or _json_bytes(artifact) != blob
        or await artifact_store.verify(digest) is not True
    ):
        raise ToolInvocationError()
    if (
        not isinstance(artifact, Mapping)
        or artifact.get("schema_version") != 1
        or artifact.get("tool_name") != ToolName.GIT_COMMIT.value
        or artifact.get("tool_call_id") != str(record.id)
        or artifact.get("operation_intent_id") != str(record.operation_intent_id)
        or artifact.get("publication_intent_id") != publication
        or artifact.get("request_digest") != request_digest
        or artifact.get("status") != record.status.value
        or artifact.get("result") != metadata
        or blob
        != _json_bytes(
            {
                "schema_version": 1,
                "tool_name": ToolName.GIT_COMMIT.value,
                "tool_call_id": str(record.id),
                "operation_intent_id": str(record.operation_intent_id),
                "publication_intent_id": publication,
                "request_digest": request_digest,
                "status": record.status.value,
                "result": metadata,
            }
        )
    ):
        raise ToolInvocationError()
    return ToolResult(
        tool_name=record.tool_name,
        status=record.status,
        error=(
            ToolError(code=ToolErrorCode.CANCELLED, message="run cancelled before Git publication")
            if cancelled
            else None
        ),
        metadata=_safe_metadata(metadata),
        artifact_digests=record.artifact_digests,
        tool_call_id=record.id,
        operation_intent_id=record.operation_intent_id,
        correlation_id=record.correlation_id,
        agent_execution_id=record.agent_execution_id,
        step_id=record.step_id,
        duration_ms=record.duration_ms or 0,
    )


def _git_result_artifact_bytes(
    call_id: UUID,
    preparation_id: UUID,
    publication_id: UUID | None,
    request_digest: str,
    status: ToolCallStatus,
    metadata: Mapping[str, object],
) -> bytes:
    return _json_bytes(
        {
            "schema_version": 1,
            "tool_name": ToolName.GIT_COMMIT.value,
            "tool_call_id": str(call_id),
            "operation_intent_id": str(preparation_id),
            "publication_intent_id": None if publication_id is None else str(publication_id),
            "request_digest": request_digest,
            "status": status.value,
            "result": dict(metadata),
        }
    )


def _git_result_metadata(
    outcome: Mapping[str, object],
    publication_id: UUID,
    context: ToolAuthorizationContext,
    request_digest: str,
) -> dict[str, object]:
    values = {**thaw_payload(outcome), "publication_intent_id": str(publication_id)}
    if (
        set(values) != _GIT_RESULT_FIELDS
        or values.get("run_id") != str(context.run_id)
        or values.get("request_digest") != request_digest
    ):
        raise ToolInvocationError()
    return _safe_metadata(values)


async def _await_committed_write(
    completion: asyncio.Task[ToolResult],
    *,
    caller_cancelled: bool = False,
    on_cancel: Callable[[], None] | None = None,
) -> ToolResult:
    """Join a committed write without letting caller cancellation cancel its owner."""

    while True:
        try:
            result = await asyncio.shield(completion)
        except asyncio.CancelledError:
            if on_cancel is not None:
                on_cancel()
            if completion.done():
                if caller_cancelled:
                    raise asyncio.CancelledError() from None
                raise
            caller_cancelled = True
            continue
        except BaseException:
            if caller_cancelled:
                raise asyncio.CancelledError() from None
            raise
        if caller_cancelled:
            raise asyncio.CancelledError()
        return result


@dataclass(frozen=True, slots=True)
class _RepositoryWriteOperationAdapter:
    writer: RepositoryWriter
    prepared: _PreparedWrite

    @classmethod
    def for_recovery(
        cls, writer: RepositoryWriter, *, path: str, content_digest: str, byte_count: int
    ) -> _RepositoryWriteOperationAdapter:
        return cls(writer, _PreparedWrite(path, None, content_digest, byte_count))

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        if self.prepared.content is None:
            raise _RepositoryWriteOperationError()
        self._validate_intent(intent)
        result = await asyncio.to_thread(
            self.writer.write_file,
            self.prepared.path,
            self.prepared.content,
        )
        self._validate_result(result)
        return OperationOutcome(payload=_file_write(result, reconciled=False))

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        self._validate_intent(intent)
        result = await asyncio.to_thread(
            self.writer.inspect_file,
            self.prepared.path,
            self.prepared.content_digest,
        )
        if result is None:
            return OperationOutcome(
                status=OperationStatus.NEEDS_RECONCILIATION,
                error="repository write outcome requires reconciliation",
            )
        self._validate_result(result)
        return OperationOutcome(payload=_file_write(result, reconciled=True))

    def _validate_intent(self, intent: OperationIntent) -> None:
        payload = intent.request_payload
        if (
            intent.kind != ToolName.REPOSITORY_WRITE_FILE.value
            or payload.get("path") != self.prepared.path
            or payload.get("content_digest") != self.prepared.content_digest
            or payload.get("content_byte_count") != self.prepared.byte_count
        ):
            raise _RepositoryWriteOperationError()

    def _validate_result(self, result: FileWrite) -> None:
        if (
            not isinstance(result, FileWrite)
            or result.path != self.prepared.path
            or result.output_digest != self.prepared.content_digest
            or result.byte_count != self.prepared.byte_count
        ):
            raise _RepositoryWriteOperationError()


def _write_operation_payload(
    context: ToolAuthorizationContext,
    run: RunSnapshot,
    prepared: _PreparedWrite,
    request_digest: str,
) -> dict[str, object]:
    return {
        "agent_execution_id": str(_required_uuid(context.agent_execution_id)),
        "content_byte_count": prepared.byte_count,
        "content_digest": prepared.content_digest,
        "path": prepared.path,
        "policy_version": context.policy_version,
        "project_id": str(run.project_id),
        "run_id": str(context.run_id),
        "request_digest": request_digest,
        "step_id": str(_required_uuid(context.step_id)),
        "worktree_id": context.worktree_id,
    }


def _write_tool_call_id(invocation_id: UUID) -> UUID:
    """Use only the Forge-supplied invocation identity for write idempotency."""

    return _required_uuid(invocation_id)


def _write_request_digest(request: ToolRequest) -> str:
    """Digest the original bounded request without persisting its raw content."""

    return hashlib.sha256(_json_bytes(request.arguments)).hexdigest()


def _write_record_matches_request(
    record: ToolCallRecord,
    context: ToolAuthorizationContext,
    tool_name: ToolName,
    normalized_arguments: Mapping[str, object],
    request_digest: str,
) -> bool:
    """Reject cross-authority, legacy, and changed-request invocation reuse."""

    return (
        record.tool_name is tool_name
        and record.run_id == context.run_id
        and record.agent_execution_id == context.agent_execution_id
        and record.step_id == context.step_id
        and record.role is context.role
        and record.policy_version == context.policy_version
        and record.resource_id == context.worktree_id
        and record.invocation_schema_version == 1
        and record.request_digest == request_digest
        and canonical_payload(record.normalized_arguments)
        == canonical_payload(normalized_arguments)
        and record.correlation_id == record.id
        and record.operation_intent_id is not None
    )


def _file_write(value: FileWrite, *, reconciled: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "byte_count": value.byte_count,
        "output_digest": value.output_digest,
        "path": value.path,
        "reconciled": reconciled,
    }
    if not reconciled:
        result["created"] = value.created
        result["previous_digest"] = value.previous_digest
    return result


def _write_result_artifact_bytes(
    operation_intent_id: UUID,
    tool_call_id: UUID,
    request_digest: str,
    resource_id: str,
    result_metadata: Mapping[str, object],
) -> bytes:
    value = _json_bytes(
        {
            "operation_intent_id": str(operation_intent_id),
            "producer_id": str(tool_call_id),
            "request_digest": request_digest,
            "resource_id": resource_id,
            "result": dict(result_metadata),
            "schema_version": 1,
            "status": ToolCallStatus.SUCCEEDED.value,
            "tool_name": ToolName.REPOSITORY_WRITE_FILE.value,
        }
    )
    if len(value) > _WRITE_RESULT_ARTIFACT_MAX_BYTES:
        raise ToolInvocationError()
    return value


async def _write_replay_result(
    record: ToolCallRecord,
    context: ToolAuthorizationContext,
    normalized_arguments: Mapping[str, object],
    request_digest: str,
    *,
    artifacts: ArtifactRepository,
    artifact_store: ArtifactStore | None,
) -> ToolResult:
    if (
        record.status is not ToolCallStatus.SUCCEEDED
        or record.tool_name is not ToolName.REPOSITORY_WRITE_FILE
        or not record.authorized
        or record.run_id != context.run_id
        or not record.artifact_digests
        or record.result_metadata is None
        or not _write_record_matches_request(
            record,
            context,
            record.tool_name,
            normalized_arguments,
            request_digest,
        )
    ):
        raise ToolInvocationError()
    if artifact_store is None:
        raise ToolInvocationError()
    if len(record.artifact_digests) != 1:
        raise ToolInvocationError()
    metadata = {
        key: thaw_payload(record.result_metadata[key])
        for key in (
            "byte_count",
            "created",
            "output_digest",
            "path",
            "previous_digest",
            "reconciled",
        )
        if key in record.result_metadata
    }
    operation_intent_id = record.operation_intent_id
    if operation_intent_id is None:
        raise ToolInvocationError()
    digest = record.artifact_digests[0]
    try:
        descriptor = await artifacts.get_by_digest(digest, run_id=context.run_id)
        descriptor_metadata = descriptor.metadata
        if (
            descriptor.digest != digest
            or descriptor.media_type != "application/json"
            or descriptor.run_id != context.run_id
            or descriptor.producer_type != "controlled_tool"
            or descriptor.producer_id != record.id
            or descriptor.parent_digests
            or type(descriptor.schema_version) is not int
            or descriptor.schema_version != 1
            or descriptor.truncated is not False
            or descriptor.original_byte_count != descriptor.byte_count
            or descriptor.truncation_policy != "none"
            or descriptor.byte_count > _WRITE_RESULT_ARTIFACT_MAX_BYTES
            or descriptor_metadata.get("operation_intent_id") != str(operation_intent_id)
            or descriptor_metadata.get("producer_id") != str(record.id)
            or descriptor_metadata.get("request_digest") != request_digest
            or descriptor_metadata.get("resource_id") != context.worktree_id
            or descriptor_metadata.get("tool_name") != record.tool_name.value
            or type(descriptor_metadata.get("result_schema_version")) is not int
            or descriptor_metadata.get("result_schema_version") != 1
            or type(descriptor_metadata.get("invocation_schema_version")) is not int
            or descriptor_metadata.get("invocation_schema_version") != 1
        ):
            raise ToolInvocationError()
        if await artifact_store.verify(digest) is not True:
            raise ToolInvocationError()
        artifact_bytes = await artifact_store.open_bytes(digest)
        if (
            len(artifact_bytes) > _WRITE_RESULT_ARTIFACT_MAX_BYTES
            or descriptor.byte_count != len(artifact_bytes)
            or hashlib.sha256(artifact_bytes).hexdigest() != digest
        ):
            raise ToolInvocationError()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        raise ToolInvocationError() from None
    if (
        not isinstance(artifact, Mapping)
        or artifact.get("operation_intent_id") != str(operation_intent_id)
        or artifact.get("producer_id") != str(record.id)
        or artifact.get("request_digest") != request_digest
        or artifact.get("resource_id") != context.worktree_id
        or artifact.get("schema_version") != 1
        or artifact.get("status") != ToolCallStatus.SUCCEEDED.value
        or artifact.get("tool_name") != record.tool_name.value
        or artifact.get("result") != metadata
        or artifact_bytes
        != _write_result_artifact_bytes(
            operation_intent_id,
            record.id,
            request_digest,
            context.worktree_id,
            metadata,
        )
    ):
        raise ToolInvocationError()
    if (
        metadata.get("path") != normalized_arguments.get("path")
        or metadata.get("output_digest") != normalized_arguments.get("content_digest")
        or metadata.get("byte_count") != normalized_arguments.get("content_byte_count")
    ):
        raise ToolInvocationError()
    return ToolResult(
        tool_name=record.tool_name,
        status=record.status,
        metadata=_safe_metadata(metadata),
        artifact_digests=record.artifact_digests,
        tool_call_id=record.id,
        operation_intent_id=record.operation_intent_id,
        correlation_id=record.correlation_id,
        agent_execution_id=record.agent_execution_id,
        step_id=record.step_id,
        duration_ms=record.duration_ms or 0,
    )


def _write_record_metadata(
    result: ToolResult,
    request_digest: str,
    resource_id: str,
    *,
    started_at: datetime,
    completed_at: datetime,
    redactor: Redactor | None,
) -> dict[str, object]:
    metadata = _record_metadata(
        result, authorized=True, started_at=started_at, completed_at=completed_at, redactor=redactor
    )
    metadata.update(
        request_digest=request_digest,
        resource_id=resource_id,
        invocation_schema_version=1,
    )
    return metadata


def _new_result(
    tool_name: ToolName,
    status: ToolCallStatus,
    error: ToolError | None = None,
    *,
    metadata: Mapping[str, object] | None = None,
    artifact_digests: tuple[str, ...] = (),
    redactor: Redactor | None = None,
) -> ToolResult:
    """Create a result only after recursively bounding and redacting metadata."""

    return ToolResult(
        tool_name=tool_name,
        status=status,
        metadata=_safe_metadata(metadata or {}, redactor=redactor),
        artifact_digests=artifact_digests,
        error=error,
    )


def _argument_text(
    authorization: ToolAuthorization,
    key: str,
    default: str | None = None,
) -> str:
    value = authorization.arguments.get(key, default)
    if not isinstance(value, str):
        raise ToolInvocationError()
    return value


def _safe_metadata(
    value: object,
    *,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Return a detached object safe for result, intent, and event boundaries."""

    selected = redactor or Redactor(policy=_RESULT_REDACTION_POLICY)
    bounded = selected.redact(value)
    if not isinstance(bounded, Mapping):
        raise TypeError("tool metadata must be an object")
    safe, _ = _durable_safe_mapping(bounded)
    validate_durable_payload(safe)
    return safe


def _durable_safe_mapping(value: Mapping[object, object]) -> tuple[dict[str, object], bool]:
    safe: dict[str, object] = {}
    redacted_keys: list[str] = []
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        safe_item, redacted = _durable_safe_value(thaw_payload(item))
        safe[key] = safe_item
        if redacted:
            redacted_keys.append(key)
    for key in redacted_keys:
        safe[f"{key}_redacted"] = True
    return safe, bool(redacted_keys)


def _durable_safe_value(value: object) -> tuple[object, bool]:
    """Substitute unsafe text and report that evidence changed."""

    if isinstance(value, str):
        try:
            validate_durable_payload(value)
        except ValueError:
            return "[REDACTED unsafe_text]", True
        return value, False
    if isinstance(value, Mapping):
        return _durable_safe_mapping(value)
    if isinstance(value, (list, tuple)):
        safe_items: list[object] = []
        any_redacted = False
        for item in value:
            safe_item, redacted = _durable_safe_value(item)
            safe_items.append(safe_item)
            any_redacted = any_redacted or redacted
        return safe_items, any_redacted
    return value, False


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        thaw_payload(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _entries(values: Sequence[RepositoryEntry]) -> list[dict[str, object]]:
    return [
        {
            "path": item.path,
            "kind": item.kind,
            "byte_count": item.byte_count,
        }
        for item in values
    ]


def _file_read(value: FileRead) -> dict[str, object]:
    return {
        "path": value.path,
        "content": value.content,
        "original_byte_count": value.original_byte_count,
        "truncated": value.truncated,
    }


def _matches(values: Sequence[SearchMatch]) -> list[dict[str, object]]:
    return [
        {
            "path": item.path,
            "line_number": item.line_number,
            "line_text": item.line_text,
        }
        for item in values
    ]


def _instructions(values: Sequence[InstructionDocument]) -> list[dict[str, object]]:
    return [
        {
            "path": item.path,
            "content": item.content,
            "original_byte_count": item.original_byte_count,
            "truncated": item.truncated,
            "untrusted_repository_content": True,
        }
        for item in values
    ]


def _git_output(value: GitOutput) -> dict[str, object]:
    return {
        "text": value.text.replace("\x00", "\\u0000"),
        "encoding": "nul-escaped-utf8",
        "original_byte_count": value.original_byte_count,
        "truncated": value.truncated,
    }


def _record_metadata(
    result: ToolResult,
    *,
    authorized: bool,
    started_at: datetime,
    completed_at: datetime,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Build the versioned result object stored in the legacy SQL projection."""

    if (
        started_at.tzinfo is None
        or started_at.utcoffset() is None
        or completed_at.tzinfo is None
        or completed_at.utcoffset() is None
        or completed_at < started_at
    ):
        raise ValueError("tool call timestamps must be ordered and timezone-aware")
    thawed = thaw_payload(result.metadata)
    if not isinstance(thawed, Mapping):
        raise TypeError("tool result metadata is not an object")
    # Evidence describes the producing controller/reviewer step, whereas audit
    # lineage identifies this tool's consuming execution. Keep both identities.
    metadata: dict[str, object] = (
        {"evidence": dict(thawed)}
        if result.tool_name in {ToolName.VALIDATION_RESULTS_READ, ToolName.REVIEW_ARTIFACTS_READ}
        else dict(thawed)
    )
    metadata["result_status"] = result.status.value
    metadata["authorized"] = authorized
    metadata["started_at"] = started_at.isoformat()
    metadata["completed_at"] = completed_at.isoformat()
    if result.error is not None:
        metadata["error"] = {
            "code": result.error.code.value,
            "message": result.error.message,
        }
    if result.artifact_digests:
        metadata["artifact_digests"] = list(result.artifact_digests)
    return _safe_metadata(metadata, redactor=redactor)


def _tool_event(
    result: ToolResult,
    context: ToolAuthorizationContext,
    run: RunSnapshot,
    tool_call_id: UUID,
    *,
    authorized: bool,
) -> RunEvent:
    payload: dict[str, object] = {
        "tool_call_id": str(tool_call_id),
        "tool_name": result.tool_name.value,
        "status": result.status.value,
        "authorized": authorized,
        "policy_version": context.policy_version,
        "resource_id": context.worktree_id,
        "step_id": str(context.step_id) if context.step_id is not None else None,
        "agent_execution_id": (
            str(context.agent_execution_id) if context.agent_execution_id is not None else None
        ),
        "correlation_id": str(result.correlation_id) if result.correlation_id else None,
        "operation_intent_id": (
            str(result.operation_intent_id) if result.operation_intent_id else None
        ),
        "duration_ms": result.duration_ms,
        "result_digest": hashlib.sha256(_json_bytes(result.metadata)).hexdigest(),
        "artifact_digests": list(result.artifact_digests),
    }
    if result.error is not None:
        payload["error_code"] = result.error.code.value
    return RunEvent(
        run_id=context.run_id,
        run_version=run.version,
        event_type="tool_call.completed",
        actor_class="agent",
        actor_id=context.agent_execution_id,
        payload=payload,
    )


def _canonical_path(value: object) -> Path | None:
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str):
        candidate = Path(value)
    else:
        return None
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve(strict=False)
    except OSError, RuntimeError, ValueError:
        return None


def _policy_from_record(project: ProjectRecord) -> ProjectPolicy | None:
    """Rehydrate one persisted policy without trusting document identity fields."""

    value = project.policy
    if value is None:
        return None
    project_id = project.id
    record_project_id = getattr(value, "project_id", None)
    version = getattr(value, "version", None)
    document = getattr(value, "document", None)
    if (
        record_project_id != project_id
        or type(version) is not int
        or not isinstance(document, Mapping)
    ):
        return None
    try:
        values = dict(document)
        values["id"] = project_id
        values["version"] = version
        values["repository_path"] = project.canonical_path
        values["github_repository"] = project.github_repository
        values["default_branch"] = project.default_branch
        return ProjectPolicy.model_validate(values)
    except TypeError, ValueError:
        return None


def _required_uuid(value: UUID | None) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ToolInvocationError()
    return value


__all__ = [
    "CapabilityMatrix",
    "ControlledToolService",
    "ToolAuthorizer",
    "ToolInvocationError",
]
