"""Durable, transaction-separated orchestration for Planner executions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final
from uuid import UUID, uuid5

from forge.agents.errors import (
    AgentBudgetExceeded,
    AgentGatewayError,
    AgentOutputInvalid,
    AgentPromptDrift,
    AgentRepairFailure,
)
from forge.agents.prompt_loader import LoadedPrompt, PromptChanged, PromptLoader, PromptLoadError
from forge.application.ports.agents import AgentGateway
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.commands import (
    CommandLeaseLost,
    CommandRecoveryRequired,
    CommandSuspended,
)
from forge.application.ports.executions import (
    ExecutionAdmission,
    ExecutionStatus,
    ExecutionUnsettledError,
)
from forge.application.ports.projects import ProjectPolicyRecord, ProjectRecord
from forge.application.ports.repository import (
    InstructionDocument,
    RepositoryEntry,
    RepositoryError,
    RepositoryReader,
)
from forge.application.ports.tasks import TaskRecord
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.agent_results import (
    safe_usage,
    safe_usage_attempts,
    usage_attempts_bytes,
    usage_bound,
    validate_agent_result,
)
from forge.application.services.control_settlement import (
    controlled_stop,
    pending_current_control_stop,
)
from forge.application.services.plan_restart import restart_source
from forge.application.services.resume_source import (
    RESUME_FIELDS,
    resume_command_ids,
    resume_origin,
)
from forge.application.services.suspended_delivery import record_suspended_delivery
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    PlannerInput,
    PolicySummary,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.approval import ApprovalGate, PlanApprovalEvidence, canonical_digest
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.plan import PlanOutput
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import ToolName
from forge.observability.usage import UsageRecord
from forge.persistence.repositories.artifacts import ArtifactNotFound

_MAX_JSON_BYTES: Final = 1_048_576
_MAX_TREE_ENTRIES: Final = 10_000
_MAX_TREE_BYTES: Final = 1_048_576
_MAX_INSTRUCTIONS: Final = 100
_MAX_INSTRUCTION_BYTES: Final = 1_048_576

# These namespaces are module constants rather than configuration.  Thus a
# retry on another worker always names the same durable step and execution.
_STEP_NAMESPACE: Final = UUID("3b7b0e4a-5e6a-4b8d-9db0-3c3c12f1f411")
_EXECUTION_NAMESPACE: Final = UUID("e516bb38-63f8-4c28-b4e4-6cc9b3a9e9c5")

_PLANNER_TOOLS: Final = (
    ToolName.REPOSITORY_LIST_FILES,
    ToolName.REPOSITORY_READ_FILE,
    ToolName.REPOSITORY_SEARCH,
    ToolName.REPOSITORY_READ_INSTRUCTIONS,
)


class PlanningError(RuntimeError):
    """Base class for bounded, context-free planning failures."""

    _MESSAGE = "planning execution failed"

    def __init__(self, _detail: object = None) -> None:
        del _detail
        super().__init__(self._MESSAGE)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._MESSAGE!r})"


class PlanningValidationError(PlanningError):
    """The command or immutable planning inputs are not admissible."""

    _MESSAGE = "planning request is invalid"


class PlanningRecoveryRequired(CommandRecoveryRequired, PlanningError):
    """A prior planning admission is ambiguous and needs explicit recovery."""

    _MESSAGE = "planning recovery is required"


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    """Detached, non-sensitive result of one planning command."""

    changed: bool
    run_id: UUID
    run_state: RunState
    execution_id: UUID
    execution_status: ExecutionStatus
    plan_artifact: ArtifactDescriptor | None = None
    evidence_artifact: ArtifactDescriptor | None = None
    failure_artifact: ArtifactDescriptor | None = None
    evidence_digest: str | None = None
    finish_status: AgentFinishStatus | None = None

    @property
    def state(self) -> RunState:
        return self.run_state

    @property
    def plan(self) -> ArtifactDescriptor | None:
        return self.plan_artifact

    @property
    def evidence(self) -> ArtifactDescriptor | None:
        return self.evidence_artifact

    @property
    def failure(self) -> ArtifactDescriptor | None:
        return self.failure_artifact


@dataclass(frozen=True, slots=True)
class _Binding:
    run: RunSnapshot
    task: TaskRecord
    project: ProjectRecord
    policy_record: ProjectPolicyRecord
    policy: ProjectPolicy


class PlanningService:
    """Coordinate admission, provider execution, and atomic plan approval."""

    def __init__(
        self,
        agent_gateway: AgentGateway,
        artifact_store: ArtifactStore,
        prompt_loader: PromptLoader,
        repository_reader_factory: Callable[[ProjectPolicy], RepositoryReader],
        clock: Clock | None = None,
    ) -> None:
        self._gateway = agent_gateway
        self._artifact_store = artifact_store
        self._prompt_loader = prompt_loader
        self._reader_factory = repository_reader_factory
        self._clock = clock or SystemClock()

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> PlanningOutcome:
        self._validate_command(command)
        step_id = uuid5(_STEP_NAMESPACE, str(command.id))
        execution_id = uuid5(_EXECUTION_NAMESPACE, str(command.id))
        semantic_attempt = _semantic_attempt(command)
        attempt = semantic_attempt

        # Snapshot and durable command.started admission must be committed before
        # any potentially slow or untrusted boundary is touched.
        try:
            binding = await self._snapshot(work, command)
        except asyncio.CancelledError:
            await _rollback_preserving_cancellation(work)
            raise
        except PlanningError:
            await _rollback(work)
            raise
        except Exception:  # noqa: BLE001 - snapshot boundary is fail-closed
            await _rollback(work)
            raise PlanningValidationError from None
        if (
            binding.run.state is RunState.AWAITING_PLAN_APPROVAL
            and binding.run.pending_gate is ApprovalGate.PLAN
            and binding.run.pending_evidence_digest is not None
        ):
            await work.commit()
            return PlanningOutcome(
                changed=False,
                run_id=binding.run.id,
                run_state=binding.run.state,
                execution_id=execution_id,
                execution_status=ExecutionStatus.SUCCEEDED,
                evidence_digest=binding.run.pending_evidence_digest,
                finish_status=AgentFinishStatus.SUCCEEDED,
            )
        if binding.run.state is RunState.PLANNING and command.payload == {}:
            await work.commit()
            raise PlanningRecoveryRequired
        if binding.run.state is not RunState.CREATED and not (
            binding.run.state is RunState.PLANNING and semantic_attempt > 1
        ):
            await _rollback(work)
            raise PlanningValidationError
        if binding.run.version != command.expected_run_version:
            await _rollback(work)
            raise PlanningValidationError

        await self._require_semantic_attempt(work, command)

        await work.commit()

        try:
            feedback = await self._revision_feedback(command, work, binding.run.id)
            planner_input = await self._read_context(binding, feedback=feedback)
            loaded_prompt = self._load_prompt()
            budget = AgentBudget.from_model_policy(binding.policy.planner_model)
            request = AgentRequest(
                execution_id=execution_id,
                run_id=binding.run.id,
                task_id=binding.task.id,
                role=AgentRole.PLANNER,
                context=planner_input,
                parent_execution_id=None,
                provider=binding.policy.planner_model.provider,
                model=binding.policy.planner_model.model,
                instruction_version=loaded_prompt.version,
                system_instruction=loaded_prompt.instruction,
                instruction_digest=loaded_prompt.digest,
                allowed_tools=_PLANNER_TOOLS,
                budget=budget,
            )
            input_bytes = _canonical_json_bytes(planner_input.model_dump(mode="json"))
            input_descriptor = await self._store_json(input_bytes)
        except asyncio.CancelledError:
            raise
        except PlanningError:
            raise
        except RepositoryError, PromptLoadError, PromptChanged, TypeError, ValueError:
            raise PlanningValidationError from None
        except Exception:  # noqa: BLE001 - provider boundary is fail-closed
            raise PlanningError from None

        # The second short transaction records immutable input lineage and the
        # CREATED -> PLANNING admission.  A non-new admission is never replayed.
        try:
            admitted = await self._admit(
                work,
                command,
                binding,
                input_descriptor,
                step_id,
                execution_id,
                attempt,
                request,
            )
            if not admitted.is_new:
                await work.commit()
                raise PlanningRecoveryRequired
            await work.commit()
        except asyncio.CancelledError:
            await _rollback_preserving_cancellation(work)
            raise
        except PlanningRecoveryRequired:
            raise
        except CommandLeaseLost:
            await _rollback(work)
            raise
        except Exception:  # noqa: BLE001 - persistence boundary is fail-closed
            await _rollback(work)
            raise PlanningError from None
        try:
            try:
                verifier = self._prompt_loader.verify_unchanged
            except AttributeError:
                verifier = None
            if verifier is not None:
                verifier(request)
            result = await self._gateway.execute(request)
        except asyncio.CancelledError:
            raise
        except AgentOutputInvalid as error:
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.INVALID_OUTPUT,
                "agent_output_invalid",
                error.usage,
                error.usage_attempts,
            )
        except AgentBudgetExceeded as error:
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.BUDGET_EXCEEDED,
                "budget_exceeded",
                error.usage,
                error.usage_attempts,
            )
        except AgentPromptDrift as error:
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.FAILED,
                "prompt_drift",
                error.usage,
                error.usage_attempts,
            )
        except AgentRepairFailure as error:
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.FAILED,
                "gateway_failure",
                error.usage,
                error.usage_attempts,
            )
        except PromptChanged, PromptLoadError:
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.FAILED,
                "prompt_drift",
                None,
            )
        except AgentGatewayError:
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.FAILED,
                "gateway_failure",
                None,
            )
        except Exception:  # noqa: BLE001 - provider boundary is fail-closed
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.FAILED,
                "gateway_failure",
                None,
            )

        status, usage, usage_attempts, reason = self._validate_result(result, request)
        if status is not AgentFinishStatus.SUCCEEDED:
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                status,
                reason,
                usage,
                usage_attempts,
            )
        if type(result.output) is not PlanOutput:
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.INVALID_OUTPUT,
                "plan_output_invalid",
                usage,
                usage_attempts,
            )
        plan = result.output
        required = {command_spec.name for command_spec in binding.policy.required_checks}
        if not required.issubset(set(plan.required_checks)):
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.FAILED,
                "required_checks_missing",
                usage,
                usage_attempts,
            )
        try:
            plan_bytes = _canonical_json_bytes(plan.model_dump(mode="json"))
            plan_descriptor = await self._store_json(plan_bytes)
            evidence = PlanApprovalEvidence(
                plan_attempt=attempt,
                task_version=1,
                task_digest=binding.task.task_digest,
                plan_digest=_sha256(plan_bytes),
                repository=binding.project.github_repository,
                base_ref=_required_text(binding.run.base_ref),
                base_sha=_required_text(binding.run.base_sha),
                policy_version=binding.policy.version,
                dependency_changes=tuple(sorted(plan.dependency_changes)),
                required_checks={name: "planned" for name in sorted(plan.required_checks)},
                runner_mode=binding.policy.runner_mode,
                local_remediation_limit=binding.policy.local_remediation_limit,
                token_budget=request.budget.max_input_tokens + request.budget.max_output_tokens,
                cost_budget_minor=request.budget.max_cost_minor,
                duration_budget_seconds=request.budget.max_duration_seconds,
            )
            evidence_bytes = _canonical_json_bytes(evidence.model_dump(mode="json"))
            if _sha256(evidence_bytes) != _evidence_digest(evidence):
                raise PlanningValidationError
            evidence_descriptor = await self._store_json(evidence_bytes)
        except asyncio.CancelledError:
            raise
        except PlanningError:
            raise
        except Exception:  # noqa: BLE001 - artifact validation is fail-closed
            return await self._failure(
                work,
                command,
                binding,
                request,
                step_id,
                execution_id,
                attempt,
                AgentFinishStatus.FAILED,
                "plan_artifact_invalid",
                usage,
                usage_attempts,
            )
        return await self._finalize_success(
            work,
            command,
            binding,
            request,
            step_id,
            execution_id,
            attempt,
            usage,
            usage_attempts,
            plan_descriptor,
            evidence_descriptor,
            _evidence_digest(evidence),
        )

    async def _snapshot(self, work: UnitOfWork, command: CommandEnvelope) -> _Binding:
        try:
            run = await work.runs.get_for_update(command.run_id)
            task = await work.tasks.get(run.task_id, for_update=True)
            project = await work.projects.get(run.project_id, for_update=True)
            if run.policy_version is None:
                raise PlanningValidationError
            policy_record = await work.projects.get_policy(
                run.project_id, run.policy_version, for_update=True
            )
            policy = _parse_policy(policy_record)
            self._validate_binding(command, run, task, project, policy_record, policy)
            return _Binding(run, task, project, policy_record, policy)
        except PlanningValidationError:
            raise
        except Exception:  # noqa: BLE001 - persistence boundary is fail-closed
            raise PlanningValidationError from None

    async def _read_context(
        self, binding: _Binding, *, feedback: UntrustedContent | None = None
    ) -> PlannerInput:
        task_bytes = binding.task.normalized_text.encode("utf-8")
        if not task_bytes or len(task_bytes) > _MAX_JSON_BYTES:
            raise PlanningValidationError
        if (
            not isinstance(binding.task.task_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", binding.task.task_digest) is None
        ):
            raise PlanningValidationError

        def read() -> PlannerInput:
            reader = self._reader_factory(binding.policy)
            entries = tuple(reader.list_files("."))
            if len(entries) > _MAX_TREE_ENTRIES:
                raise PlanningValidationError
            normalized_entries = _normalize_entries(entries, binding.policy)
            tree_text = (
                "\n".join(
                    f"{entry.path}\t{entry.kind}\t{entry.byte_count}"
                    for entry in normalized_entries
                )
                or "(empty)"
            )
            if len(tree_text.encode("utf-8")) > _MAX_TREE_BYTES:
                raise PlanningValidationError
            instructions = tuple(reader.read_instructions("."))
            normalized_instructions = _normalize_instructions(instructions, binding.policy)
            original_task = UntrustedContent.from_text(
                binding.task.normalized_text,
                source_kind=UntrustedSourceKind.TASK,
                source_reference=str(binding.task.id),
            )
            tree = UntrustedContent.from_text(
                tree_text,
                source_kind=UntrustedSourceKind.REPOSITORY_TREE,
                source_reference=".",
            )
            instruction_envelopes = tuple(
                UntrustedContent.from_text(
                    item.content,
                    source_kind=UntrustedSourceKind.INSTRUCTION,
                    source_reference=item.path,
                    original_byte_count=item.original_byte_count,
                    truncated=item.truncated,
                )
                for item in normalized_instructions
            )
            return PlannerInput(
                original_task=original_task,
                base_commit=_required_text(binding.run.base_sha),
                repository_tree=tree,
                relevant_instructions=instruction_envelopes,
                policy_summary=PolicySummary.from_policy(binding.policy),
                revision_feedback=feedback,
            )

        return await asyncio.to_thread(read)

    async def _revision_feedback(
        self, command: CommandEnvelope, work: UnitOfWork, run_id: UUID
    ) -> UntrustedContent | None:
        digest = command.payload.get("feedback_digest")
        if digest is None:
            return None
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise PlanningValidationError
        descriptor = await work.artifacts.get_by_digest(digest, run_id=run_id)
        if (
            descriptor.run_id != run_id
            or descriptor.producer_type != "plan_revision_feedback"
            or descriptor.producer_id is None
            or descriptor.truncated
        ):
            raise PlanningValidationError
        revision = await work.commands.get(descriptor.producer_id)
        origin = await resume_origin(work, command)
        authority = origin if origin is not None else command
        if (
            revision.run_id != run_id
            or revision.command_type != "request_plan_revision"
            or revision.actor_id != command.actor_id
            or revision.expected_run_version + 1 != authority.expected_run_version
            or revision.status is not CommandStatus.COMPLETED
        ):
            raise PlanningValidationError
        await work.commit()
        raw = await self._artifact_store.open_bytes(descriptor.digest)
        if hashlib.sha256(raw).hexdigest() != digest or not raw or len(raw) > 131_072:
            raise PlanningValidationError
        document = json.loads(raw)
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "command_id", "feedback"}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["command_id"] != str(revision.id)
            or document["feedback"] != revision.payload.get("feedback")
        ):
            raise PlanningValidationError
        text = document["feedback"]
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 16_384:
            raise PlanningValidationError
        return UntrustedContent.from_text(
            text, source_kind=UntrustedSourceKind.TASK, source_reference=f"revision:{digest}"
        )

    def _load_prompt(self) -> LoadedPrompt:
        try:
            loaded = self._prompt_loader.load(AgentRole.PLANNER)
            if type(loaded) is not LoadedPrompt:
                raise PromptLoadError
            return loaded
        except PromptLoadError, TypeError, ValueError:
            raise PlanningValidationError from None

    async def _store_json(self, data: bytes) -> ArtifactDescriptor:
        if len(data) > _MAX_JSON_BYTES:
            raise PlanningValidationError
        try:
            descriptor = await self._artifact_store.put_bytes(data, media_type="application/json")
            if type(descriptor) is not ArtifactDescriptor or descriptor.digest != _sha256(data):
                raise PlanningValidationError
            return descriptor
        except PlanningValidationError:
            raise
        except Exception:  # noqa: BLE001 - persisted policy is untrusted
            raise PlanningError from None

    async def _admit(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        binding: _Binding,
        input_descriptor: ArtifactDescriptor,
        step_id: UUID,
        execution_id: UUID,
        attempt: int,
        request: AgentRequest,
    ) -> ExecutionAdmission:
        await work.commands.assert_current_lease(command)
        latest = await self._snapshot(work, command)
        if latest.run.version != command.expected_run_version or (
            latest.run.state is not RunState.CREATED
            and not (latest.run.state is RunState.PLANNING and attempt > 1)
        ):
            await _rollback(work)
            raise PlanningRecoveryRequired
        await self._require_semantic_attempt(work, command)
        persisted_input = await self._record_shared_content(
            work,
            input_descriptor,
            run_id=latest.run.id,
            producer_type="planning_context",
            producer_id=execution_id,
        )
        input_id = _artifact_id(persisted_input)
        if input_id is None:
            raise PlanningValidationError
        if latest.run.state is RunState.CREATED:
            await work.runs.transition(
                latest.run.id,
                latest.run.version,
                RunState.PLANNING,
                "run.planning_started",
                {
                    "command_id": str(command.id),
                    "step_id": str(step_id),
                    "agent_execution_id": str(execution_id),
                },
                occurred_at=self._clock.now(),
            )
        admission = await work.executions.admit(
            latest.run.id,
            step_id,
            execution_id,
            "plan",
            attempt,
            AgentRole.PLANNER,
            request.instruction_version,
            request.provider,
            request.model,
            instruction_digest=request.instruction_digest,
            input_artifact_id=input_id,
            transition_from=(
                RunState.CREATED.value if latest.run.state is RunState.CREATED else None
            ),
            transition_to=(
                RunState.PLANNING.value if latest.run.state is RunState.CREATED else None
            ),
            admitted_at=self._clock.now(),
        )
        return admission

    async def _finalize_success(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        binding: _Binding,
        request: AgentRequest,
        step_id: UUID,
        execution_id: UUID,
        attempt: int,
        usage: UsageRecord,
        usage_attempts: tuple[UsageRecord, ...],
        plan_descriptor: ArtifactDescriptor,
        evidence_descriptor: ArtifactDescriptor,
        evidence_digest: str,
    ) -> PlanningOutcome:
        try:
            await work.commands.assert_current_lease(command)
            run = await work.runs.get_for_update(command.run_id)
            task = await work.tasks.get(run.task_id, for_update=True)
            project = await work.projects.get(run.project_id, for_update=True)
            if run.policy_version is None:
                raise PlanningValidationError
            policy_record = await work.projects.get_policy(
                run.project_id, run.policy_version, for_update=True
            )
            policy = _parse_policy(policy_record)
            self._validate_binding(command, run, task, project, policy_record, policy)
            if run.state is not RunState.PLANNING or run.version != command.expected_run_version + (
                1 if binding.run.state is RunState.CREATED else 0
            ):
                raise PlanningRecoveryRequired
            if await pending_current_control_stop(work, run):
                raise PlanningRecoveryRequired(
                    "planning finalization is fenced by operator control"
                )
            await self._record_usage_attempts(work, run.id, execution_id, usage_attempts)
            persisted_plan = await self._record_shared_content(
                work,
                plan_descriptor,
                run_id=run.id,
                producer_type="implementation_plan",
                producer_id=execution_id,
            )
            persisted_evidence = await self._record_shared_content(
                work,
                evidence_descriptor,
                run_id=run.id,
                producer_type="plan_approval_evidence",
                producer_id=execution_id,
                parent_digests=(plan_descriptor.digest,),
            )
            plan_id = _artifact_id(persisted_plan)
            evidence_id = _artifact_id(persisted_evidence)
            if plan_id is None or evidence_id is None:
                raise PlanningValidationError
            execution = await work.executions.finalize(
                run.id,
                step_id,
                execution_id,
                AgentFinishStatus.SUCCEEDED,
                usage,
                output_artifact_id=plan_id,
                completed_at=self._clock.now(),
                provider=request.provider,
                model=request.model,
                instruction_version=request.instruction_version,
                kind="plan",
                attempt=attempt,
                role=AgentRole.PLANNER,
            )
            final_run = await work.runs.await_approval(
                run.id,
                run.version,
                ApprovalGate.PLAN,
                evidence_digest,
                "run.plan_approval_requested",
                {
                    "evidence_digest": evidence_digest,
                    "plan_artifact_id": str(plan_id),
                    "evidence_artifact_id": str(evidence_id),
                },
                occurred_at=self._clock.now(),
            )
            await work.commit()
            return PlanningOutcome(
                changed=True,
                run_id=final_run.id,
                run_state=final_run.state,
                execution_id=execution.execution_id,
                execution_status=execution.status,
                plan_artifact=persisted_plan,
                evidence_artifact=persisted_evidence,
                evidence_digest=evidence_digest,
                finish_status=execution.finish_status,
            )
        except asyncio.CancelledError:
            await _rollback_preserving_cancellation(work)
            raise
        except CommandLeaseLost:
            await _rollback(work)
            await self._record_late_usage(work, command, request, usage, usage_attempts)
            raise
        except PlanningRecoveryRequired:
            await _rollback(work)
            admitted_run = (
                binding.run.with_state(RunState.PLANNING)
                if binding.run.state is RunState.CREATED
                else binding.run
            )
            if await controlled_stop(work, command, admitted_run):
                await self._record_late_usage(
                    work,
                    command,
                    request,
                    usage,
                    usage_attempts,
                    cancelled=True,
                    output=plan_descriptor,
                    admitted_run=admitted_run,
                )
                raise CommandSuspended
            await self._record_late_usage(
                work,
                command,
                request,
                usage,
                usage_attempts,
                output=plan_descriptor,
            )
            raise
        except Exception:  # noqa: BLE001 - persistence boundary is fail-closed
            await _rollback(work)
            raise PlanningError from None

    async def _failure(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        binding: _Binding,
        request: AgentRequest,
        step_id: UUID,
        execution_id: UUID,
        attempt: int,
        finish_status: AgentFinishStatus,
        reason: str,
        usage: UsageRecord | None,
        usage_attempts: tuple[UsageRecord, ...] = (),
    ) -> PlanningOutcome:
        safe_usage = _safe_usage(usage, request)
        safe_attempts = _safe_usage_attempts(usage, usage_attempts, request)
        failure_bytes = _canonical_json_bytes(
            {"finish_status": finish_status.value, "reason": reason}
        )
        failure_descriptor: ArtifactDescriptor | None = None
        try:
            failure_descriptor = await self._store_json(failure_bytes)
            await work.commands.assert_current_lease(command)
            run = await work.runs.get_for_update(command.run_id)
            task = await work.tasks.get(run.task_id, for_update=True)
            project = await work.projects.get(run.project_id, for_update=True)
            if run.policy_version is None:
                raise PlanningValidationError
            policy_record = await work.projects.get_policy(
                run.project_id, run.policy_version, for_update=True
            )
            policy = _parse_policy(policy_record)
            self._validate_binding(command, run, task, project, policy_record, policy)
            if run.state is not RunState.PLANNING or run.version != command.expected_run_version + (
                1 if binding.run.state is RunState.CREATED else 0
            ):
                raise PlanningRecoveryRequired
            if await pending_current_control_stop(work, run):
                raise PlanningRecoveryRequired(
                    "planning finalization is fenced by operator control"
                )
            await self._record_usage_attempts(work, run.id, execution_id, safe_attempts)
            persisted_failure = await work.artifacts.record(
                failure_descriptor,
                run_id=run.id,
                producer_type="planning_failure",
                producer_id=execution_id,
            )
            failure_id = _artifact_id(persisted_failure)
            if failure_id is None:
                raise PlanningValidationError
            execution = await work.executions.finalize(
                run.id,
                step_id,
                execution_id,
                finish_status,
                safe_usage,
                output_artifact_id=failure_id,
                completed_at=self._clock.now(),
                provider=request.provider,
                model=request.model,
                instruction_version=request.instruction_version,
                kind="plan",
                attempt=attempt,
                role=AgentRole.PLANNER,
            )
            if finish_status is AgentFinishStatus.CANCELLED:
                final_run = await work.runs.transition(
                    run.id,
                    run.version,
                    RunState.CANCELLED,
                    "run.cancelled",
                    {"reason": reason},
                    occurred_at=self._clock.now(),
                )
            else:
                final_run = await work.runs.intervene(
                    run.id,
                    run.version,
                    "run.intervention_required",
                    {"reason": reason},
                    occurred_at=self._clock.now(),
                )
            await work.commit()
            return PlanningOutcome(
                changed=True,
                run_id=final_run.id,
                run_state=final_run.state,
                execution_id=execution.execution_id,
                execution_status=execution.status,
                failure_artifact=persisted_failure,
                finish_status=execution.finish_status,
            )
        except asyncio.CancelledError:
            await _rollback_preserving_cancellation(work)
            raise
        except CommandLeaseLost:
            await _rollback(work)
            await self._record_late_usage(work, command, request, safe_usage, safe_attempts)
            raise
        except PlanningRecoveryRequired:
            await _rollback(work)
            admitted_run = (
                binding.run.with_state(RunState.PLANNING)
                if binding.run.state is RunState.CREATED
                else binding.run
            )
            if await controlled_stop(work, command, admitted_run):
                await self._record_late_usage(
                    work,
                    command,
                    request,
                    safe_usage,
                    safe_attempts,
                    cancelled=True,
                    output=failure_descriptor,
                    admitted_run=admitted_run,
                )
                raise CommandSuspended
            await self._record_late_usage(
                work,
                command,
                request,
                safe_usage,
                safe_attempts,
                output=failure_descriptor,
            )
            raise
        except Exception:  # noqa: BLE001 - persistence boundary is fail-closed
            await _rollback(work)
            raise PlanningError from None

    async def _record_late_usage(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        request: AgentRequest,
        usage: UsageRecord,
        attempts: tuple[UsageRecord, ...],
        *,
        cancelled: bool = False,
        output: ArtifactDescriptor | None = None,
        admitted_run: RunSnapshot | None = None,
    ) -> None:
        """Retain measured evidence without settling an execution we no longer own."""
        try:
            run = await work.runs.get_for_update(command.run_id)
            admission = await work.executions.get_admission(run.id, request.execution_id)
            if (
                admission is None
                or admission.agent_execution_id != uuid5(_EXECUTION_NAMESPACE, str(command.id))
                or admission.role is not AgentRole.PLANNER
                or admission.provider != request.provider
                or admission.model != request.model
                or admission.instruction_version != request.instruction_version
            ):
                raise PlanningValidationError
            descriptor = await self._store_json(
                _canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "command_id": str(command.id),
                        "delivery_attempt": command.attempt,
                        "execution_id": str(request.execution_id),
                        "usage": json.loads(_usage_attempts_bytes((usage,)))["attempts"][0],
                        "attempts": json.loads(_usage_attempts_bytes(attempts))["attempts"],
                    }
                )
            )
            persisted = await work.artifacts.record(
                descriptor,
                run_id=run.id,
                producer_type="planning_late_usage",
                producer_id=request.execution_id,
            )
            await work.events.append(
                RunEvent(
                    run_id=run.id,
                    run_version=run.version,
                    event_type="planning.late_usage_observed",
                    payload={
                        "command_id": str(command.id),
                        "execution_id": str(request.execution_id),
                        "usage_artifact_digest": persisted.digest,
                    },
                    actor_class="worker",
                )
            )
            output_id = None
            if output is not None:
                # A late observation belongs to this execution, even when its
                # plan bytes match a later accepted plan. Keep it in a receipt
                # envelope so immutable content lineage cannot alias publication.
                late_output = await self._store_json(
                    _canonical_json_bytes(
                        {
                            "schema_version": 1,
                            "command_id": str(command.id),
                            "execution_id": str(request.execution_id),
                            "output_digest": output.digest,
                            "output": json.loads(
                                await self._artifact_store.open_bytes(output.digest)
                            ),
                        }
                    )
                )
                retained = await work.artifacts.record(
                    late_output,
                    run_id=run.id,
                    producer_type="planning_late_result",
                    producer_id=request.execution_id,
                )
                output_id = retained.artifact_id
            if cancelled:
                await work.executions.finalize(
                    run.id,
                    uuid5(_STEP_NAMESPACE, str(command.id)),
                    request.execution_id,
                    AgentFinishStatus.CANCELLED,
                    usage,
                    output_artifact_id=output_id,
                    completed_at=self._clock.now(),
                    provider=request.provider,
                    model=request.model,
                    instruction_version=request.instruction_version,
                    kind="plan",
                    attempt=_semantic_attempt(command),
                    role=AgentRole.PLANNER,
                )
                if admitted_run is None:
                    raise PlanningRecoveryRequired("suspended planning admission is absent")
                await record_suspended_delivery(
                    work,
                    command,
                    admitted_run,
                    uuid5(_STEP_NAMESPACE, str(command.id)),
                    "plan",
                    _semantic_attempt(command),
                )
            await work.commit()
        except asyncio.CancelledError:
            await _rollback_preserving_cancellation(work)
            raise
        except Exception:  # noqa: BLE001 - stale receipt failure cannot authorize settlement
            await _rollback(work)
            # Receipt failure must never give this stale delivery authority to
            # fail the replacement command or settle the admitted execution.
            raise CommandLeaseLost("late usage persistence requires recovery") from None

    async def _require_semantic_attempt(self, work: UnitOfWork, command: CommandEnvelope) -> None:
        if command.payload == {}:
            return
        try:
            expected = await work.executions.next_attempt(command.run_id, "plan")
        except ExecutionUnsettledError:
            await work.rollback()
            raise PlanningRecoveryRequired from None
        if _semantic_attempt(command) != expected:
            await work.rollback()
            raise PlanningValidationError
        if await resume_origin(work, command) is not None:
            return
        if await restart_source(work, command) is None:
            await work.rollback()
            raise PlanningValidationError

    async def _record_shared_content(
        self,
        work: UnitOfWork,
        descriptor: ArtifactDescriptor,
        *,
        run_id: UUID,
        producer_type: str,
        producer_id: UUID,
        parent_digests: tuple[str, ...] = (),
    ) -> ArtifactDescriptor:
        """Reuse identical content without rewriting its original provenance.

        Each execution retains its own input/output artifact references. Repeated
        plans and contexts therefore share bytes while their executions remain
        distinct. The repository still verifies every immutable metadata field.
        """
        try:
            existing = await work.artifacts.get_by_digest(descriptor.digest, run_id=run_id)
        except ArtifactNotFound:
            existing = None
        if existing is not None:
            if existing.producer_type != producer_type or existing.producer_id is None:
                raise PlanningValidationError
            producer_id = existing.producer_id
        return await work.artifacts.record(
            descriptor,
            run_id=run_id,
            producer_type=producer_type,
            producer_id=producer_id,
            parent_digests=parent_digests,
        )

    async def _record_usage_attempts(
        self,
        work: UnitOfWork,
        run_id: UUID,
        execution_id: UUID,
        attempts: tuple[UsageRecord, ...],
    ) -> None:
        if not attempts:
            return
        descriptor = await self._store_json(_usage_attempts_bytes(attempts))
        await work.artifacts.record(
            descriptor,
            run_id=run_id,
            producer_type="agent_usage_attempts",
            producer_id=execution_id,
        )

    @staticmethod
    def _validate_command(command: CommandEnvelope) -> None:
        if (
            type(command) is not CommandEnvelope
            or command.command_type != "start_planning"
            or not _valid_planning_payload(command.payload)
            or command.payload_schema_version != 1
            or command.status is not CommandStatus.LEASED
            or command.expected_run_version < 0
            or command.run_id.int == 0
        ):
            raise PlanningValidationError
        identities = resume_command_ids(command.payload)
        if identities is not None and command.idempotency_key != (
            f"{command.run_id}:resume:{identities[0]}:start_planning:{_semantic_attempt(command)}"
        ):
            raise PlanningValidationError

    @staticmethod
    def _validate_result(
        result: object, request: AgentRequest
    ) -> tuple[AgentFinishStatus, UsageRecord, tuple[UsageRecord, ...], str]:
        """Validate provider identity before trusting status or measured usage."""

        return _validate_result(result, request)

    @staticmethod
    def _validate_binding(
        command: CommandEnvelope,
        run: RunSnapshot,
        task: TaskRecord,
        project: ProjectRecord,
        policy_record: ProjectPolicyRecord,
        policy: ProjectPolicy,
    ) -> None:
        if (
            run.id != command.run_id
            or run.project_id != project.id
            or run.task_id != task.id
            or task.project_id != project.id
            or policy_record.project_id != project.id
            or policy_record.version != run.policy_version
            or policy.id != project.id
            or policy.version != policy_record.version
            or policy.repository_path != project.canonical_path
            or policy.github_repository != project.github_repository
            or policy.default_branch != project.default_branch
            or run.base_ref != f"refs/heads/{policy.default_branch}"
            or run.base_sha is None
            or run.base_ref is None
            or re.fullmatch(r"[0-9a-f]{40}", run.base_sha) is None
            or _sha256(_canonical_json_bytes(policy_record.document)) != policy_record.policy_digest
        ):
            raise PlanningValidationError


def _parse_policy(record: ProjectPolicyRecord) -> ProjectPolicy:
    try:
        if type(record.document_schema_version) is not int or record.document_schema_version != 1:
            raise ValueError
        return ProjectPolicy.model_validate(record.document)
    except Exception:  # noqa: BLE001 - persisted policy is untrusted
        raise PlanningValidationError from None


def _normalize_entries(
    entries: Sequence[RepositoryEntry], policy: ProjectPolicy
) -> tuple[RepositoryEntry, ...]:
    normalized: list[RepositoryEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if (
            type(entry) is not RepositoryEntry
            or type(entry.path) is not str
            or type(entry.kind) is not str
        ):
            raise PlanningValidationError
        path = _normalize_repository_path(entry.path)
        key = path.casefold() if os.name == "nt" else path
        if key in seen or _secret_path(path, policy.effective_secret_paths):
            raise PlanningValidationError
        if (
            not entry.kind.strip()
            or len(entry.kind) > 96
            or _has_ascii_control(entry.kind)
            or type(entry.byte_count) is not int
            or entry.byte_count < 0
        ):
            raise PlanningValidationError
        seen.add(key)
        normalized.append(RepositoryEntry(path=path, kind=entry.kind, byte_count=entry.byte_count))
    return tuple(sorted(normalized, key=lambda item: (item.path, item.kind, item.byte_count)))


def _normalize_instructions(
    items: Sequence[InstructionDocument], policy: ProjectPolicy
) -> tuple[InstructionDocument, ...]:
    if len(items) > _MAX_INSTRUCTIONS:
        raise PlanningValidationError
    result: list[InstructionDocument] = []
    seen: set[str] = set()
    for item in items:
        if type(item) is not InstructionDocument or item.untrusted_repository_content is not True:
            raise PlanningValidationError
        path = _normalize_repository_path(item.path)
        key = path.casefold() if os.name == "nt" else path
        if key in seen or _secret_path(path, policy.effective_secret_paths):
            raise PlanningValidationError
        if (
            type(item.content) is not str
            or not item.content
            or len(item.content.encode("utf-8")) > _MAX_INSTRUCTION_BYTES
        ):
            raise PlanningValidationError
        if type(item.original_byte_count) is not int or item.original_byte_count < len(
            item.content.encode("utf-8")
        ):
            raise PlanningValidationError
        if type(item.truncated) is not bool or (
            not item.truncated and item.original_byte_count != len(item.content.encode("utf-8"))
        ):
            raise PlanningValidationError
        seen.add(key)
        result.append(
            InstructionDocument(
                path=path,
                content=item.content,
                original_byte_count=item.original_byte_count,
                truncated=item.truncated,
            )
        )
    return tuple(sorted(result, key=lambda item: item.path))


def _normalize_repository_path(value: str) -> str:
    if (
        not value
        or value == "."
        or "\\" in value
        or value.startswith("/")
        or "\x00" in value
        or _has_ascii_control(value)
    ):
        raise PlanningValidationError
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts) or "/".join(parts) != value:
        raise PlanningValidationError
    return value


def _has_ascii_control(value: str) -> bool:
    return any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value)


def _secret_path(path: str, secrets: Sequence[str]) -> bool:
    folded = path.casefold() if os.name == "nt" else path
    return any(
        folded == (secret.casefold() if os.name == "nt" else secret)
        or folded.startswith((secret.casefold() if os.name == "nt" else secret) + "/")
        for secret in secrets
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonicalize(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda p: str(p[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_canonicalize(item) for item in value),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    if hasattr(value, "value") and type(value.value) is str:
        return value.value
    return value


def _evidence_digest(evidence: PlanApprovalEvidence) -> str:
    return canonical_digest(evidence)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_id(descriptor: ArtifactDescriptor) -> UUID | None:
    value = descriptor.artifact_id
    return value if isinstance(value, UUID) and value.int != 0 else None


def _required_text(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningValidationError
    return value


_safe_usage = safe_usage
_safe_usage_attempts = safe_usage_attempts
_usage_attempts_bytes = usage_attempts_bytes
_usage_bound = usage_bound


def _validate_result(
    result: object, request: AgentRequest
) -> tuple[AgentFinishStatus, UsageRecord, tuple[UsageRecord, ...], str]:
    return validate_agent_result(result, request, expected_role=AgentRole.PLANNER)


async def _rollback(work: UnitOfWork) -> None:
    await work.rollback()


async def _rollback_preserving_cancellation(work: UnitOfWork) -> None:
    """Complete rollback even while cancellation is being delivered."""

    await asyncio.shield(work.rollback())


__all__ = [
    "PlanningError",
    "PlanningOutcome",
    "PlanningRecoveryRequired",
    "PlanningService",
    "PlanningValidationError",
    "canonical_json_bytes",
]


def _semantic_attempt(command: CommandEnvelope) -> int:
    if command.payload == {}:
        return 1
    value = command.payload.get("semantic_attempt")
    minimum = 1 if resume_command_ids(command.payload) is not None else 2
    if type(value) is not int or value < minimum:
        raise PlanningValidationError
    return value


def _valid_planning_payload(payload: Mapping[str, object]) -> bool:
    if payload == {}:
        return True
    resumed = resume_command_ids(payload) is not None
    if set(payload) - RESUME_FIELDS not in (
        {"semantic_attempt"},
        {"semantic_attempt", "feedback_digest"},
    ):
        return False
    value = payload.get("semantic_attempt")
    digest = payload.get("feedback_digest")
    return (
        type(value) is int
        and value >= (1 if resumed else 2)
        and (
            digest is None
            or (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None)
        )
    )


canonical_json_bytes = _canonical_json_bytes
