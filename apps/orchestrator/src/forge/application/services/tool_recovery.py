"""Read-only discovery and durable settlement for completed controlled effects."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast
from uuid import UUID

from forge.application.adapters.git_commit import (
    GitCommitOperationError,
    _prepared_from_intent,
    _primary_paths,
    _publish_outcome,
    _publish_request,
)
from forge.application.adapters.named_check import (
    NAMED_CHECK_KIND,
    NamedCheckOperationAdapter,
    NamedCheckOperationError,
)
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.repository import MAX_REPOSITORY_WRITE_BYTES, FileWrite
from forge.application.ports.tool_recovery import (
    VerifiedTerminalEffect,
    terminal_call_digest,
    terminal_intent_digest,
)
from forge.application.ports.tools import ToolCallRecord
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ManagedWorktree, PublishedGitCommit
from forge.application.services.recovery import RecoveryError
from forge.application.services.tools import (
    ToolInvocationError,
    _git_prepare_payload,
    _git_result_artifact_bytes,
    _git_result_metadata,
    _git_terminal_result,
    _named_result,
    _repository_mutation_result_artifact_bytes,
    _repository_mutation_schema_version,
    _safe_metadata,
    _tool_event,
    _write_record_metadata,
)
from forge.domain.actor import AgentRole
from forge.domain.artifact import ArtifactDescriptor, validate_artifact_digest
from forge.domain.event import thaw_payload
from forge.domain.operation import OperationIntent, OperationStatus, canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot
from forge.domain.subscription import SpecialistPurpose
from forge.domain.tool import (
    SubscriptionToolAuthorizationContext,
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolError,
    ToolErrorCode,
    ToolName,
    ToolResult,
)
from forge.observability.redaction import Redactor


class ToolRecoveryDisposition(StrEnum):
    SETTLED = "settled"
    TERMINAL = "terminal"
    UNSUPPORTED = "unsupported"
    UNRESOLVED = "unresolved"
    INTERVENTION = "intervention"


@dataclass(frozen=True, slots=True)
class ToolRecoveryResult:
    call_id: UUID
    disposition: ToolRecoveryDisposition


@dataclass(frozen=True, slots=True)
class _VerifiedArtifactBytes:
    """Per-verification immutable bytes, never a persistence fallback."""

    blobs: Mapping[str, bytes]

    async def verify(self, digest: str) -> bool:
        return digest in self.blobs

    async def open_bytes(self, digest: str, *, max_bytes: int | None = None) -> bytes:
        data = self.blobs.get(digest)
        if data is None or (max_bytes is not None and len(data) > max_bytes):
            raise ValueError("terminal artifact was not loaded")
        return data

    async def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        max_bytes: int | None = None,
        bounding_policy: str = "none",
    ) -> ArtifactDescriptor:
        raise RuntimeError("terminal verification cannot write artifacts")


def _recovery_context(
    call: ToolCallRecord,
) -> ToolAuthorizationContext | SubscriptionToolAuthorizationContext:
    if call.resource_id is None or call.policy_version is None:
        raise ValueError("recovery authority is incomplete")
    if call.subscription_task_id is not None and call.subscription_attempt_id is not None:
        if (
            call.agent_execution_id is not None
            or call.step_id is not None
            or call.role is not None
            or call.subscription_purpose is None
        ):
            raise ValueError("recovery authority is ambiguous")
        return SubscriptionToolAuthorizationContext(
            run_id=call.run_id,
            task_id=call.subscription_task_id,
            attempt_id=call.subscription_attempt_id,
            purpose=SpecialistPurpose(call.subscription_purpose),
            worktree_id=call.resource_id,
            policy_version=call.policy_version,
            permitted_tools=frozenset({call.tool_name}),
            invocation_id=call.id,
            operation_intent_id=call.operation_intent_id,
        )
    if call.role is None or call.agent_execution_id is None or call.step_id is None:
        raise ValueError("recovery legacy authority is incomplete")
    return ToolAuthorizationContext(
        role=call.role,
        run_id=call.run_id,
        worktree_id=call.resource_id,
        policy_version=call.policy_version,
        agent_execution_id=call.agent_execution_id,
        step_id=call.step_id,
        invocation_id=call.id,
    )


def _request_authority(call: ToolCallRecord) -> dict[str, object] | None:
    try:
        context = _recovery_context(call)
    except TypeError, ValueError:
        return None
    if isinstance(context, SubscriptionToolAuthorizationContext):
        return {
            "authority_schema_version": 2,
            "subscription_task_id": str(context.task_id),
            "subscription_attempt_id": str(context.attempt_id),
            "subscription_purpose": context.purpose.value,
        }
    if context.role is not AgentRole.DEVELOPER:
        return None
    return {"agent_execution_id": str(context.agent_execution_id), "step_id": str(context.step_id)}


class ToolRecoveryService:
    """Recovery boundary; it never invokes a writer, Git, or runner."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        artifact_store: ArtifactStore,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._artifact_store = artifact_store
        self._redactor = redactor

    async def verify_terminal_effect(self, effect_id: UUID) -> VerifiedTerminalEffect | None:
        """Attest retained terminal evidence without invoking or finalizing an effect."""
        try:
            artifacts = await self._load_terminal_artifact_bytes(effect_id)
            if artifacts is None:
                return None
            return await self._verify_loaded_terminal_effect(effect_id, artifacts)
        except RuntimeError, TypeError, ValueError, OSError:
            return None

    async def _load_terminal_artifact_bytes(
        self,
        effect_id: UUID,
    ) -> _VerifiedArtifactBytes | None:
        # Only descriptor reads occur in this UoW. All external storage calls
        # happen after it closes; validators later re-read current DB bindings.
        async with self._uow_factory() as work:
            call = await work.tool_calls.find(effect_id)
            if (
                call is None
                or call.subscription_task_id is None
                or call.tool_name
                not in {
                    ToolName.BUILD_RUN_NAMED_CHECK,
                    ToolName.GIT_COMMIT,
                }
            ):
                return None
            pending = list(call.artifact_digests)
            if call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
                pending.extend(
                    item.digest
                    for item in await work.artifacts.get_by_producer(
                        run_id=call.run_id,
                        producer_type="named_check",
                        producer_id=call.id,
                    )
                )
            descriptors: dict[str, ArtifactDescriptor] = {}
            while pending:
                digest = pending.pop()
                if digest in descriptors:
                    continue
                descriptor = await work.artifacts.get_by_digest(digest, run_id=call.run_id)
                if (
                    descriptor.digest != digest
                    or len(descriptors) >= 16
                    or descriptor.byte_count > 8 * 1024 * 1024
                ):
                    return None
                descriptors[digest] = descriptor
                pending.extend(descriptor.parent_digests)
            if (
                not descriptors
                or sum(item.byte_count for item in descriptors.values()) > 32 * 1024 * 1024
            ):
                return None
        blobs: dict[str, bytes] = {}
        for digest, descriptor in descriptors.items():
            if await self._artifact_store.verify(digest) is not True:
                return None
            data = await self._artifact_store.open_bytes(digest, max_bytes=8 * 1024 * 1024)
            if (
                type(data) is not bytes
                or len(data) != descriptor.byte_count
                or hashlib.sha256(data).hexdigest() != digest
            ):
                return None
            blobs[digest] = data
        return _VerifiedArtifactBytes(MappingProxyType(blobs))

    async def _verify_loaded_terminal_effect(
        self,
        effect_id: UUID,
        artifacts: _VerifiedArtifactBytes,
    ) -> VerifiedTerminalEffect | None:
        try:
            async with self._uow_factory() as work:
                call = await work.tool_calls.find(effect_id)
                if call is None:
                    return None
                run = await work.runs.get_for_update(call.run_id)
                call = await work.tool_calls.get(effect_id)
                if (
                    call.subscription_task_id is None
                    or call.operation_intent_id != effect_id
                    or call.status
                    not in {
                        ToolCallStatus.SUCCEEDED,
                        ToolCallStatus.FAILED,
                        ToolCallStatus.CANCELLED,
                    }
                    or call.completed_at is None
                ):
                    return None
                if call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
                    result = await self._recover_named(
                        work,
                        call,
                        run,
                        verify_only=True,
                        verification_store=artifacts,
                    )
                elif call.tool_name is ToolName.GIT_COMMIT:
                    result = await self._recover_git(
                        work,
                        call,
                        run,
                        verify_only=True,
                        verification_store=artifacts,
                    )
                else:
                    return None
                if result.disposition is not ToolRecoveryDisposition.TERMINAL:
                    return None
                intents = [await work.operations.get(effect_id)]
                if call.tool_name is ToolName.GIT_COMMIT:
                    publication = await work.operations.get_by_idempotency_key(
                        f"git.commit:{effect_id}:publish"
                    )
                    if publication is None:
                        return None
                    intents.append(publication)
                if any(
                    intent.status is not OperationStatus.SUCCEEDED or intent.completed_at is None
                    for intent in intents
                ):
                    return None
                return VerifiedTerminalEffect(
                    effect_id=effect_id,
                    call_digest=terminal_call_digest(call),
                    intent_digests=tuple(
                        (intent.id, terminal_intent_digest(intent)) for intent in intents
                    ),
                )
        except RuntimeError, TypeError, ValueError, OSError:
            # Missing or malformed historical evidence never removes a fence.
            return None

    def _matches_terminal(self, call: ToolCallRecord, result: ToolResult) -> bool:
        if call.completed_at is None or call.request_digest is None or call.resource_id is None:
            return False
        expected = _write_record_metadata(
            replace(result, duration_ms=call.duration_ms or 0),
            call.request_digest,
            call.resource_id,
            started_at=call.started_at,
            completed_at=call.completed_at,
            redactor=self._redactor,
        )
        # Persistence adds audit lineage to the operation result metadata.
        for key, value in {
            "operation_intent_id": str(call.operation_intent_id),
            "correlation_id": str(call.correlation_id),
            "policy_version": call.policy_version,
            "duration_ms": call.duration_ms,
        }.items():
            expected.setdefault(key, value)
        return (
            call.status is result.status
            and call.artifact_digests == result.artifact_digests
            and call.result_metadata == expected
        )

    async def recover_one(self, call_id: UUID) -> ToolRecoveryResult:
        async with self._uow_factory() as work:
            call = await work.tool_calls.get(call_id)
            run = await work.runs.get_for_update(call.run_id)
            call = await work.tool_calls.get(call_id)
            if call.status is not ToolCallStatus.RUNNING:
                await work.rollback()
                return ToolRecoveryResult(call_id, ToolRecoveryDisposition.TERMINAL)
            if (
                call.tool_name is ToolName.GIT_DIFF
                and call.normalized_arguments == {"scope": "snapshot"}
                and call.subscription_task_id is not None
                and call.operation_intent_id is None
                and call.authorized
                and call.request_digest == canonical_digest({"scope": "snapshot"})
                and call.resource_id is not None
                and call.policy_version is not None
            ):
                # A read-only snapshot has no mutation to replay. Any still-owned
                # reader will observe this terminal row before publishing evidence.
                return await self._finalize(
                    work,
                    call,
                    run,
                    ToolResult(
                        tool_name=call.tool_name,
                        status=ToolCallStatus.CANCELLED,
                        error=ToolError(
                            code=ToolErrorCode.CANCELLED, message="snapshot was interrupted"
                        ),
                        metadata={"recovery_disposition": "interrupted_snapshot"},
                        tool_call_id=call.id,
                        correlation_id=call.id,
                        duration_ms=0,
                    ),
                )
            if call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
                return await self._recover_named(work, call, run)
            if call.tool_name is ToolName.GIT_COMMIT:
                return await self._recover_git(work, call, run)
            if call.tool_name not in {
                ToolName.REPOSITORY_WRITE_FILE,
                ToolName.REPOSITORY_DELETE_FILE,
                ToolName.REPOSITORY_RENAME_FILE,
            }:
                await work.rollback()
                return ToolRecoveryResult(call_id, ToolRecoveryDisposition.UNSUPPORTED)
            if call.operation_intent_id is None:
                await work.rollback()
                return ToolRecoveryResult(call_id, ToolRecoveryDisposition.INTERVENTION)
            operation = await work.operations.get(call.operation_intent_id)
            if operation.status is not OperationStatus.SUCCEEDED:
                await work.rollback()
                return ToolRecoveryResult(call_id, ToolRecoveryDisposition.UNRESOLVED)
            payload = thaw_payload(operation.outcome or {})
            if not self._valid_repository_mutation(call, operation, run, payload):
                await work.rollback()
                return ToolRecoveryResult(call_id, ToolRecoveryDisposition.INTERVENTION)
            assert call.request_digest is not None and call.resource_id is not None
            assert call.policy_version is not None
            payload = _safe_metadata(payload, redactor=self._redactor)
            result = ToolResult(
                tool_name=call.tool_name,
                status=ToolCallStatus.SUCCEEDED,
                metadata=payload,
                tool_call_id=call.id,
                operation_intent_id=operation.id,
                correlation_id=call.id,
                agent_execution_id=call.agent_execution_id,
                step_id=call.step_id,
                duration_ms=0,
            )
            data = _repository_mutation_result_artifact_bytes(
                call.tool_name,
                operation.id,
                call.id,
                call.request_digest,
                call.resource_id,
                payload,
            )
            return await self._settle(work, call, run, result, data)

    async def _settle(
        self,
        work: UnitOfWork,
        call: ToolCallRecord,
        run: RunSnapshot,
        result: ToolResult,
        data: bytes,
    ) -> ToolRecoveryResult:
        assert call.request_digest is not None and call.resource_id is not None
        assert call.policy_version is not None
        descriptor = await self._artifact_store.put_bytes(
            data, media_type="application/json", max_bytes=65536, bounding_policy="head_tail"
        )
        if (
            descriptor.digest != hashlib.sha256(data).hexdigest()
            or descriptor.byte_count != len(data)
            or descriptor.media_type != "application/json"
            or descriptor.truncated
            or await self._artifact_store.verify(descriptor.digest) is not True
            or await self._artifact_store.open_bytes(descriptor.digest) != data
        ):
            raise RuntimeError("artifact verification failed")
        result = replace(result, artifact_digests=(descriptor.digest,))
        await work.artifacts.record(
            descriptor,
            run_id=call.run_id,
            producer_type="controlled_tool",
            producer_id=call.id,
            metadata={
                **(
                    {"publication_intent_id": result.metadata["publication_intent_id"]}
                    if call.tool_name is ToolName.GIT_COMMIT
                    else {}
                ),
                "operation_intent_id": str(result.operation_intent_id),
                "producer_id": str(call.id),
                "request_digest": call.request_digest,
                "resource_id": call.resource_id,
                "result_schema_version": _repository_mutation_schema_version(call.tool_name),
                "tool_name": call.tool_name.value,
                "invocation_schema_version": 1,
            },
        )
        return await self._finalize(work, call, run, result)

    async def _finalize(
        self,
        work: UnitOfWork,
        call: ToolCallRecord,
        run: RunSnapshot,
        result: ToolResult,
    ) -> ToolRecoveryResult:
        assert call.request_digest is not None and call.resource_id is not None
        assert call.policy_version is not None
        completed = datetime.now(UTC)
        final = replace(
            call,
            status=result.status,
            completed_at=completed,
            result_metadata=_write_record_metadata(
                result,
                call.request_digest,
                call.resource_id,
                started_at=call.started_at,
                completed_at=completed,
                redactor=self._redactor,
            ),
            duration_ms=0,
            artifact_digests=result.artifact_digests,
            result_metadata_schema_version=1,
        )
        await work.tool_calls.finalize(final)
        context = _recovery_context(call)
        await work.events.append(_tool_event(result, context, run, call.id, authorized=True))
        await work.commit()
        return ToolRecoveryResult(call.id, ToolRecoveryDisposition.SETTLED)

    async def _recover_named(
        self,
        work: UnitOfWork,
        call: ToolCallRecord,
        run: RunSnapshot,
        *,
        verify_only: bool = False,
        verification_store: ArtifactStore | None = None,
    ) -> ToolRecoveryResult:
        subscription = (
            call.subscription_task_id is not None or call.subscription_attempt_id is not None
        )
        if (
            not call.authorized
            or (not subscription and (call.role is not AgentRole.DEVELOPER or call.step_id is None))
            or (subscription and (call.role is not None or call.step_id is not None))
            or call.policy_version != run.policy_version
            or call.invocation_schema_version != 1
            or call.arguments_schema_version != 1
            or call.operation_intent_id is None
            or call.request_digest is None
            or call.resource_id is None
            or call.correlation_id != call.id
            or run.branch_name is None
            or run.base_sha is None
            or run.worktree_path is None
        ):
            return ToolRecoveryResult(call.id, ToolRecoveryDisposition.INTERVENTION)
        intent = await work.operations.get(call.operation_intent_id)
        if intent.status is not OperationStatus.SUCCEEDED:
            return ToolRecoveryResult(call.id, ToolRecoveryDisposition.UNRESOLVED)
        request = intent.request_payload
        authority = _request_authority(call)
        if (
            intent.kind != NAMED_CHECK_KIND
            or intent.run_id != call.run_id
            or intent.idempotency_key != f"named_check:{call.id}"
            or intent.outcome_schema_version != 1
            or request.get("tool_call_id") != str(call.id)
            or authority is None
            or any(request.get(key) != value for key, value in authority.items())
            or request.get("worktree_id") != call.resource_id
            or call.normalized_arguments != {"command_name": request.get("command_name")}
            or canonical_digest(call.normalized_arguments) != call.request_digest
        ):
            return ToolRecoveryResult(call.id, ToolRecoveryDisposition.INTERVENTION)
        try:
            policy_record = await work.projects.get_policy(
                run.project_id, cast(int, run.policy_version)
            )
            policy = ProjectPolicy.model_validate(policy_record.document)
            if policy.id != run.project_id or policy.version != call.policy_version:
                raise ValueError()
            worktree = ManagedWorktree(
                identity=WorktreeIdentity.for_run(
                    run.project_id, run.id, run.branch_name, run.database_name is not None
                ),
                path=Path(run.worktree_path),
                base_sha=run.base_sha,
            )
            adapter = NamedCheckOperationAdapter.for_recovery(
                worktree=worktree,
                policy=policy,
                artifacts=work.artifacts,
                artifact_store=verification_store
                if verification_store is not None
                else self._artifact_store,
            )
            verified = await adapter.reconcile(intent)
            if verified.status is not OperationStatus.SUCCEEDED:
                return ToolRecoveryResult(call.id, ToolRecoveryDisposition.UNRESOLVED)
            if intent.outcome is None or canonical_digest(intent.outcome) != canonical_digest(
                verified.payload
            ):
                raise ValueError()
            result = _named_result(call, verified.payload, 0)
        except TypeError, ValueError, NamedCheckOperationError:
            return ToolRecoveryResult(call.id, ToolRecoveryDisposition.INTERVENTION)
        if verify_only:
            disposition = (
                ToolRecoveryDisposition.TERMINAL
                if self._matches_terminal(call, result)
                else ToolRecoveryDisposition.INTERVENTION
            )
            return ToolRecoveryResult(call.id, disposition)
        return await self._finalize(work, call, run, result)

    async def _recover_git(
        self,
        work: UnitOfWork,
        call: ToolCallRecord,
        run: RunSnapshot,
        *,
        verify_only: bool = False,
        verification_store: ArtifactStore | None = None,
    ) -> ToolRecoveryResult:
        if (
            not call.authorized
            or _request_authority(call) is None
            or (
                call.subscription_task_id is not None
                and (
                    call.subscription_purpose
                    not in {SpecialistPurpose.INTEGRATION.value, SpecialistPurpose.PRIMARY.value}
                    or call.operation_intent_id != call.id
                )
            )
            or call.policy_version != run.policy_version
            or call.invocation_schema_version != 1
            or call.arguments_schema_version != 1
            or call.operation_intent_id is None
            or call.request_digest is None
            or call.resource_id is None
            or call.correlation_id != call.id
            or run.branch_name is None
            or run.base_sha is None
            or run.worktree_path is None
        ):
            return ToolRecoveryResult(call.id, ToolRecoveryDisposition.INTERVENTION)
        preparation = await work.operations.get(call.operation_intent_id)
        publication = await work.operations.get_by_idempotency_key(f"git.commit:{call.id}:publish")
        if publication is None or publication.status is not OperationStatus.SUCCEEDED:
            return ToolRecoveryResult(call.id, ToolRecoveryDisposition.UNRESOLVED)
        try:
            worktree = ManagedWorktree(
                identity=WorktreeIdentity.for_run(
                    run.project_id, run.id, run.branch_name, run.database_name is not None
                ),
                path=Path(run.worktree_path),
                base_sha=run.base_sha,
            )
            context = _recovery_context(call)
            message = preparation.request_payload.get("message")
            if not isinstance(message, str) or (
                preparation.idempotency_key != f"git.commit:{call.id}:prepare"
                or preparation.request_payload
                != _git_prepare_payload(
                    context,
                    worktree,
                    message,
                    call.request_digest,
                    owned_paths=_primary_paths(preparation.request_payload),
                )
                or call.resource_id != worktree.identity.worktree_name
                or call.normalized_arguments
                != {"message_digest": hashlib.sha256(message.encode()).hexdigest()}
                or publication.outcome_schema_version != 1
                or publication.outcome is None
            ):
                raise GitCommitOperationError()
            values = _publish_request(publication, worktree)
            prepared = _prepared_from_intent(preparation, values, worktree)
            outcome = publication.outcome
            observed = PublishedGitCommit(
                worktree_identity=worktree.identity,
                previous_sha=cast(str, outcome.get("previous_sha")),
                tree_sha=cast(str, outcome.get("tree_sha")),
                new_sha=cast(str, outcome.get("new_sha")),
                message=message,
            )
            verified = _publish_outcome(publication, values, prepared, observed)
            if canonical_digest(verified.payload) != canonical_digest(outcome):
                raise GitCommitOperationError()
            metadata = _git_result_metadata(outcome, publication.id, context, call.request_digest)
        except TypeError, ValueError, GitCommitOperationError, ToolInvocationError:
            return ToolRecoveryResult(call.id, ToolRecoveryDisposition.INTERVENTION)
        result = ToolResult(
            tool_name=call.tool_name,
            status=ToolCallStatus.SUCCEEDED,
            metadata=metadata,
            tool_call_id=call.id,
            operation_intent_id=preparation.id,
            correlation_id=call.id,
            agent_execution_id=call.agent_execution_id,
            step_id=call.step_id,
            duration_ms=0,
        )
        if verify_only:
            retained = await _git_terminal_result(
                call,
                context,
                call.request_digest,
                work.artifacts,
                verification_store if verification_store is not None else self._artifact_store,
            )
            valid = retained.metadata == metadata and self._matches_terminal(
                call, replace(result, artifact_digests=retained.artifact_digests)
            )
            return ToolRecoveryResult(
                call.id,
                ToolRecoveryDisposition.TERMINAL if valid else ToolRecoveryDisposition.INTERVENTION,
            )
        data = _git_result_artifact_bytes(
            call.id, preparation.id, publication.id, call.request_digest, result.status, metadata
        )
        return await self._settle(work, call, run, result, data)

    async def recover_all(self, *, allow_unresolved: bool = False) -> int:
        """Finalize known receipts before startup opens command admission."""
        settled = 0
        cursor = None
        while page := await self.recover_page(cursor, 100):
            if not allow_unresolved and any(
                result.disposition
                not in {ToolRecoveryDisposition.SETTLED, ToolRecoveryDisposition.TERMINAL}
                for result in page
            ):
                raise RecoveryError("startup tool recovery has unresolved evidence")
            settled += sum(result.disposition is ToolRecoveryDisposition.SETTLED for result in page)
            cursor = page[-1].call_id
        return settled

    async def recover_page(
        self, after_id: UUID | None, limit: int
    ) -> tuple[ToolRecoveryResult, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("recovery page limit must be 1..100")
        async with self._uow_factory() as work:
            candidates = await work.tool_calls.list_running_with_operations(
                after_id=after_id, limit=limit
            )
            await work.rollback()
        return tuple([await self.recover_one(candidate.id) for candidate in candidates])

    @staticmethod
    def valid_write_request(
        call: ToolCallRecord,
        operation: OperationIntent,
        run: RunSnapshot,
    ) -> bool:
        request = operation.request_payload
        authority = _request_authority(call)
        if (
            not call.authorized
            or call.request_digest is None
            or call.resource_id is None
            or call.invocation_schema_version != 1
            or call.arguments_schema_version != 1
            or authority is None
            or call.policy_version != run.policy_version
            or call.correlation_id != call.id
            or operation.id != call.operation_intent_id
            or operation.kind != ToolName.REPOSITORY_WRITE_FILE.value
            or operation.run_id != call.run_id
            or operation.request_schema_version != 1
            or operation.idempotency_key != f"tool:{call.id}"
            or operation.request_digest != canonical_digest(request)
            or type(request.get("policy_version")) is not int
        ):
            return False
        expected = {
            **authority,
            "run_id": str(call.run_id),
            "project_id": str(run.project_id),
            "policy_version": call.policy_version,
            "request_digest": call.request_digest,
            "worktree_id": call.resource_id,
            **dict(call.normalized_arguments),
        }
        if set(call.normalized_arguments) != {"path", "content_digest", "content_byte_count"}:
            return False
        if (
            not isinstance(request.get("path"), str)
            or type(request.get("content_byte_count")) is not int
            or not 0 <= cast(int, request["content_byte_count"]) <= MAX_REPOSITORY_WRITE_BYTES
        ):
            return False
        try:
            validate_artifact_digest(cast(str, request.get("content_digest")))
        except TypeError, ValueError:
            return False
        return request == expected

    @staticmethod
    def _valid_write(
        call: ToolCallRecord,
        operation: OperationIntent,
        run: RunSnapshot,
        payload: Mapping[str, object],
    ) -> bool:
        request = operation.request_payload
        if (
            not ToolRecoveryService.valid_write_request(call, operation, run)
            or operation.outcome_schema_version != 1
        ):
            return False
        if type(payload.get("reconciled")) is not bool:
            return False
        expected_keys = {"path", "output_digest", "byte_count", "reconciled"}
        if not payload["reconciled"]:
            expected_keys |= {"created", "previous_digest"}
        if set(payload) != expected_keys:
            return False
        try:
            validate_artifact_digest(cast(str, payload["output_digest"]))
            if not payload["reconciled"]:
                FileWrite(
                    path=cast(str, payload["path"]),
                    output_digest=cast(str, payload["output_digest"]),
                    byte_count=cast(int, payload["byte_count"]),
                    created=cast(bool, payload["created"]),
                    previous_digest=cast(str | None, payload["previous_digest"]),
                )
        except TypeError, ValueError:
            return False
        return (
            isinstance(payload["path"], str)
            and payload["path"] == request["path"]
            and payload["output_digest"] == request["content_digest"]
            and type(request["content_byte_count"]) is int
            and type(payload["byte_count"]) is int
            and payload["byte_count"] == request["content_byte_count"]
            and 0 <= payload["byte_count"] <= MAX_REPOSITORY_WRITE_BYTES
        )

    @staticmethod
    def _valid_repository_mutation(
        call: ToolCallRecord,
        operation: OperationIntent,
        run: RunSnapshot,
        payload: Mapping[str, object],
    ) -> bool:
        if call.tool_name is ToolName.REPOSITORY_WRITE_FILE:
            return ToolRecoveryService._valid_write(call, operation, run, payload)
        if call.tool_name not in {ToolName.REPOSITORY_DELETE_FILE, ToolName.REPOSITORY_RENAME_FILE}:
            return False
        request = operation.request_payload
        authority = _request_authority(call)
        if (
            not call.authorized
            or authority is None
            or call.request_digest is None
            or call.resource_id is None
            or call.policy_version != run.policy_version
            or operation.id != call.operation_intent_id
            or operation.kind != call.tool_name.value
            or operation.run_id != call.run_id
            or operation.idempotency_key != f"tool:{call.id}"
            or operation.request_digest != canonical_digest(request)
            or operation.outcome_schema_version != 1
            or any(request.get(key) != value for key, value in authority.items())
            or request.get("run_id") != str(call.run_id)
            or request.get("project_id") != str(run.project_id)
            or request.get("policy_version") != call.policy_version
            or request.get("request_digest") != call.request_digest
            or request.get("worktree_id") != call.resource_id
            or request.get("expected_digest") != call.normalized_arguments.get("expected_digest")
            or type(payload.get("byte_count")) is not int
            or payload.get("output_digest") != request.get("expected_digest")
            or payload.get("reconciled") is not False
        ):
            return False
        try:
            validate_artifact_digest(cast(str, request.get("expected_digest")))
            validate_artifact_digest(cast(str, payload.get("output_digest")))
        except TypeError, ValueError:
            return False
        if call.tool_name is ToolName.REPOSITORY_DELETE_FILE:
            return (
                set(call.normalized_arguments) == {"path", "expected_digest"}
                and request.get("path") == call.normalized_arguments.get("path")
                and set(payload)
                == {
                    "path",
                    "expected_digest",
                    "mutation",
                    "output_digest",
                    "byte_count",
                    "reconciled",
                }
                and payload.get("path") == request.get("path")
                and payload.get("mutation") == "delete"
            )
        return (
            set(call.normalized_arguments) == {"source", "destination", "expected_digest"}
            and request.get("path") == call.normalized_arguments.get("source")
            and request.get("destination") == call.normalized_arguments.get("destination")
            and set(payload)
            == {
                "source",
                "destination",
                "expected_digest",
                "mutation",
                "output_digest",
                "byte_count",
                "reconciled",
            }
            and payload.get("source") == request.get("path")
            and payload.get("destination") == request.get("destination")
            and payload.get("mutation") == "rename"
        )

    valid_repository_mutation = _valid_repository_mutation
