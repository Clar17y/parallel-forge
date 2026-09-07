"""Durably admit and settle the first Developer execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
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
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.executions import ExecutionOutcome, ExecutionUnsettledError
from forge.application.ports.repository import (
    InstructionDocument,
    RepositoryError,
    RepositoryReader,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.agent_results import (
    safe_usage,
    safe_usage_attempts,
    usage_attempts_bytes,
    validate_agent_result,
)
from forge.application.services.approved_plan import (
    ApprovedPlan,
    ApprovedPlanError,
    ApprovedPlanLoader,
)
from forge.application.services.developer_result import verify_developer_output
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    DeveloperInput,
    DeveloperOutput,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.tool import ToolName
from forge.observability.usage import UsageRecord

_STEP_NAMESPACE = UUID("5f7bc719-a867-443c-b6a8-936c6663a983")
_EXECUTION_NAMESPACE = UUID("6649eb62-7e4a-421f-9861-8be14cefa22b")
_TOOLS = (
    ToolName.REPOSITORY_LIST_FILES,
    ToolName.REPOSITORY_READ_FILE,
    ToolName.REPOSITORY_SEARCH,
    ToolName.REPOSITORY_READ_INSTRUCTIONS,
    ToolName.REPOSITORY_WRITE_FILE,
    ToolName.GIT_STATUS,
    ToolName.GIT_DIFF,
    ToolName.GIT_COMMIT,
    ToolName.BUILD_RUN_NAMED_CHECK,
)


class DevelopmentError(RuntimeError):
    pass


class DevelopmentRecoveryRequired(CommandRecoveryRequired, DevelopmentError):
    pass


class DevelopmentService:
    def __init__(
        self,
        agent_gateway: AgentGateway,
        artifact_store: ArtifactStore,
        prompt_loader: PromptLoader,
        approved_plans: ApprovedPlanLoader,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
        repository_reader_factory: Callable[[ProjectPolicy, ManagedWorktree], RepositoryReader],
    ) -> None:
        self._gateway, self._store, self._prompts = agent_gateway, artifact_store, prompt_loader
        self._approved, self._git_factory, self._reader_factory = (
            approved_plans,
            git_factory,
            repository_reader_factory,
        )

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> ExecutionOutcome:
        self._validate(command)
        step_id, execution_id = (
            uuid5(_STEP_NAMESPACE, str(command.id)),
            uuid5(_EXECUTION_NAMESPACE, str(command.id)),
        )
        replay = await self._replay(command, work, step_id, execution_id)
        if replay is not None:
            return replay
        approved = await self._load_current(command, work)
        worktree, git = self._worktree(approved)
        # Do not retain database locks while reading untrusted repository files.
        await work.commit()
        context = await self._context(approved, worktree)
        prompt = self._prompt()
        request = AgentRequest(
            execution_id=execution_id,
            run_id=approved.run.id,
            task_id=approved.task.id,
            role=AgentRole.DEVELOPER,
            context=context,
            provider=approved.policy.developer_model.provider,
            model=approved.policy.developer_model.model,
            instruction_version=prompt.version,
            system_instruction=prompt.instruction,
            instruction_digest=prompt.digest,
            allowed_tools=_TOOLS,
            budget=AgentBudget.from_model_policy(approved.policy.developer_model),
        )
        input_descriptor = await self._put(self._json(context.model_dump(mode="json")))
        try:
            await self._admit(command, work, approved, request, input_descriptor, step_id)
            await work.commit()
        except Exception:
            await work.rollback()
            raise
        try:
            self._prompts.verify_unchanged(request)
            result = await self._gateway.execute(request)
        except AgentOutputInvalid as error:
            return await self._settle(
                command,
                work,
                approved,
                request,
                step_id,
                AgentFinishStatus.INVALID_OUTPUT,
                error.usage,
                error.usage_attempts,
                None,
                "agent_output_invalid",
            )
        except AgentBudgetExceeded as error:
            return await self._settle(
                command,
                work,
                approved,
                request,
                step_id,
                AgentFinishStatus.BUDGET_EXCEEDED,
                error.usage,
                error.usage_attempts,
                None,
                "budget_exceeded",
            )
        except (AgentRepairFailure, AgentPromptDrift) as error:
            return await self._settle(
                command,
                work,
                approved,
                request,
                step_id,
                AgentFinishStatus.FAILED,
                error.usage,
                error.usage_attempts,
                None,
                "gateway_failure",
            )
        except AgentGatewayError, PromptChanged, PromptLoadError:
            return await self._settle(
                command,
                work,
                approved,
                request,
                step_id,
                AgentFinishStatus.FAILED,
                None,
                (),
                None,
                "gateway_failure",
            )
        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )
        output = (
            result.output
            if status is AgentFinishStatus.SUCCEEDED and type(result.output) is DeveloperOutput
            else None
        )
        if output is None:
            return await self._settle(
                command,
                work,
                approved,
                request,
                step_id,
                AgentFinishStatus.INVALID_OUTPUT
                if status is AgentFinishStatus.SUCCEEDED
                else status,
                usage,
                attempts,
                None,
                reason or "developer_output_invalid",
            )
        try:
            verification = await verify_developer_output(
                output, git=git, worktree=worktree, approved_plan=approved.plan
            )
            if not verification.accepted:
                raise DevelopmentError(
                    verification.intervention_reason or "developer_result_invalid"
                )
            descriptor = await self._put(self._json(output.model_dump(mode="json")))
        except Exception:  # noqa: BLE001 - stale usage must never settle a replacement
            return await self._settle(
                command,
                work,
                approved,
                request,
                step_id,
                AgentFinishStatus.FAILED,
                usage,
                attempts,
                None,
                "developer_result_invalid",
            )
        return await self._settle(
            command,
            work,
            approved,
            request,
            step_id,
            AgentFinishStatus.SUCCEEDED,
            usage,
            attempts,
            descriptor,
            "",
            verified_output=output,
        )

    async def _replay(
        self, command: CommandEnvelope, work: UnitOfWork, step_id: UUID, execution_id: UUID
    ) -> ExecutionOutcome | None:
        await self._fence(command, work)
        outcome = await work.executions.get_outcome(command.run_id, "implement", 1)
        if outcome is None:
            return None
        approved = await self._approved.load(work, command.run_id)
        succeeded = outcome.finish_status is AgentFinishStatus.SUCCEEDED
        target = RunState.VALIDATING if succeeded else RunState.AWAITING_HUMAN_INTERVENTION
        if (
            command.actor_id != approved.approval_actor_id
            or outcome.agent_execution_id != execution_id
            or outcome.step_id != step_id
            or outcome.role is not AgentRole.DEVELOPER
            or outcome.provider != approved.policy.developer_model.provider
            or outcome.model != approved.policy.developer_model.model
            or approved.run.state is not target
            or approved.run.version != command.expected_run_version + 1
        ):
            raise DevelopmentRecoveryRequired("implementation replay authority differs")
        event_type = "run.implementation_completed" if succeeded else "run.intervention_required"
        events = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == event_type
            and event.payload.get("execution_id") == str(execution_id)
        ]
        if (
            len(events) != 1
            or events[0].run_version != approved.run.version
            or events[0].actor_class != "worker"
            or events[0].actor_id is not None
            or events[0].payload.get("command_id") != str(command.id)
            or events[0].payload.get("approval_id") != str(approved.approval_id)
        ):
            raise DevelopmentRecoveryRequired("implementation replay evidence differs")
        if succeeded:
            artifacts = await work.artifacts.get_by_producer(
                run_id=command.run_id, producer_type="developer_result", producer_id=execution_id
            )
            if len(artifacts) != 1 or artifacts[0].artifact_id != outcome.output_artifact_id:
                raise DevelopmentRecoveryRequired("implementation replay output differs")
            descriptor = artifacts[0]
            wire = await self._store.open_bytes(descriptor.digest)
            output = DeveloperOutput.model_validate_json(wire)
            if (
                descriptor.media_type != "application/json"
                or hashlib.sha256(wire).hexdigest() != descriptor.digest
                or wire != self._json(output.model_dump(mode="json"))
            ):
                raise DevelopmentRecoveryRequired("implementation replay output differs")
            worktree, git = self._worktree(approved)
            verified = await verify_developer_output(
                output, git=git, worktree=worktree, approved_plan=approved.plan
            )
            if not verified.accepted:
                raise DevelopmentRecoveryRequired("implementation replay candidate differs")
            queued = await work.commands.get_by_idempotency_key(f"{command.run_id}:validate:1")
            if (
                queued is None
                or queued.command_type != "validate"
                or queued.status is not CommandStatus.PENDING
                or queued.payload != {"semantic_attempt": 1}
                or type(queued.payload.get("semantic_attempt")) is not int
                or queued.payload_schema_version != 1
                or queued.actor_id != approved.approval_actor_id
                or queued.expected_run_version != approved.run.version
                or events[0].payload.get("queued_command_id") != str(queued.id)
                or events[0].payload.get("output_artifact_id") != str(outcome.output_artifact_id)
            ):
                raise DevelopmentRecoveryRequired("implementation replay queue differs")
        await self._fence(command, work)
        await work.commit()
        return outcome

    async def _load_current(self, command: CommandEnvelope, work: UnitOfWork) -> ApprovedPlan:
        await self._fence(command, work)
        try:
            approved = await self._approved.load(work, command.run_id)
        except ApprovedPlanError:
            raise DevelopmentRecoveryRequired(
                "implementation approval authority is invalid"
            ) from None
        if (
            approved.run.state is not RunState.IMPLEMENTING
            or approved.run.version != command.expected_run_version
        ):
            raise DevelopmentRecoveryRequired("implementation run is not current")
        if command.actor_id != approved.approval_actor_id:
            raise DevelopmentRecoveryRequired("implementation actor is invalid")
        return approved

    def _worktree(self, approved: ApprovedPlan) -> tuple[ManagedWorktree, ControlledGitPort]:
        run = approved.run
        if not run.worktree_path or not run.branch_name or not run.base_sha:
            raise DevelopmentRecoveryRequired("implementation worktree is absent")
        identity = WorktreeIdentity.for_run(
            run.project_id, run.id, run.branch_name, approved.policy.database.enabled
        )
        worktree = ManagedWorktree(
            identity=identity, path=Path(run.worktree_path), base_sha=run.base_sha
        )
        git = self._git_factory(approved.policy)
        if git.inspect_worktree(identity, run.base_sha) != worktree or not git.is_ancestor(
            worktree
        ):
            raise DevelopmentRecoveryRequired("implementation worktree differs")
        return worktree, git

    async def _context(self, approved: ApprovedPlan, worktree: ManagedWorktree) -> DeveloperInput:
        def read() -> tuple[UntrustedContent, ...]:
            reader = self._reader_factory(approved.policy, worktree)
            documents = tuple(reader.read_instructions("."))
            return tuple(
                UntrustedContent.from_text(
                    d.content,
                    source_kind=UntrustedSourceKind.INSTRUCTION,
                    source_reference=d.path,
                    original_byte_count=d.original_byte_count,
                    truncated=d.truncated,
                )
                for d in documents
                if type(d) is InstructionDocument
            )

        try:
            instructions = await asyncio.to_thread(read)
        except RepositoryError, TypeError, ValueError:
            raise DevelopmentError("developer context is invalid") from None
        return DeveloperInput(
            original_task=UntrustedContent.from_text(
                approved.task.normalized_text,
                source_kind=UntrustedSourceKind.TASK,
                source_reference=str(approved.task.id),
            ),
            plan=approved.plan,
            worktree_id=worktree.identity.worktree_name,
            base_commit=worktree.base_sha,
            relevant_instructions=instructions,
        )

    async def _admit(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        request: AgentRequest,
        descriptor: ArtifactDescriptor,
        step_id: UUID,
    ) -> None:
        current = await self._load_current(command, work)
        if current.approval_id != approved.approval_id or current.run != approved.run:
            raise DevelopmentRecoveryRequired("implementation approval changed")
        self._worktree(current)
        try:
            expected = await work.executions.next_attempt(command.run_id, "implement")
        except ExecutionUnsettledError:
            raise DevelopmentRecoveryRequired("implementation admission is unsettled") from None
        if expected != 1:
            raise DevelopmentRecoveryRequired("remediation is not implemented")
        persisted = await work.artifacts.record(
            descriptor,
            run_id=command.run_id,
            producer_type="developer_context",
            producer_id=request.execution_id,
        )
        if persisted.artifact_id is None:
            raise DevelopmentError("developer input lineage is invalid")
        admission = await work.executions.admit(
            command.run_id,
            step_id,
            request.execution_id,
            "implement",
            1,
            AgentRole.DEVELOPER,
            request.instruction_version,
            request.provider,
            request.model,
            input_artifact_id=persisted.artifact_id,
        )
        if not admission.is_new:
            raise DevelopmentRecoveryRequired("implementation admission requires recovery")

    async def _settle(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        request: AgentRequest,
        step_id: UUID,
        finish: AgentFinishStatus,
        usage: UsageRecord | None,
        attempts: tuple[UsageRecord, ...],
        output: ArtifactDescriptor | None,
        reason: str,
        *,
        verified_output: DeveloperOutput | None = None,
    ) -> ExecutionOutcome:
        usage = safe_usage(usage, request)
        attempts = safe_usage_attempts(usage, attempts, request)
        try:
            current = await self._load_current(command, work)
            if current.approval_id != approved.approval_id or current.run != approved.run:
                raise DevelopmentRecoveryRequired("implementation approval changed")
            for attempt in attempts:
                d = await self._put(usage_attempts_bytes((attempt,)))
                await work.artifacts.record(
                    d,
                    run_id=command.run_id,
                    producer_type="agent_usage_attempts",
                    producer_id=request.execution_id,
                )
            output_id = None
            if output is not None:
                output = await work.artifacts.record(
                    output,
                    run_id=command.run_id,
                    producer_type="developer_result",
                    producer_id=request.execution_id,
                )
                output_id = output.artifact_id
            if finish is AgentFinishStatus.SUCCEEDED:
                if verified_output is None:
                    raise DevelopmentRecoveryRequired("implementation output is absent")
                worktree, git = self._worktree(current)
                verified = await verify_developer_output(
                    verified_output, git=git, worktree=worktree, approved_plan=current.plan
                )
                if not verified.accepted:
                    raise DevelopmentRecoveryRequired("implementation candidate changed")
            await self._fence(command, work)
            outcome = await work.executions.finalize(
                command.run_id,
                step_id,
                request.execution_id,
                finish,
                usage,
                output_artifact_id=output_id,
                provider=request.provider,
                model=request.model,
                instruction_version=request.instruction_version,
                kind="implement",
                attempt=1,
                role=AgentRole.DEVELOPER,
            )
            run = current.run
            if finish is AgentFinishStatus.SUCCEEDED:
                queued = await work.commands.enqueue(
                    run_id=run.id,
                    command_type="validate",
                    idempotency_key=f"{run.id}:validate:1",
                    payload={"semantic_attempt": 1},
                    expected_run_version=run.version + 1,
                    actor_id=approved.approval_actor_id,
                )
                if queued.status is not CommandStatus.PENDING:
                    raise DevelopmentRecoveryRequired("validation command is not pending")
                await work.runs.transition(
                    run.id,
                    run.version,
                    RunState.VALIDATING,
                    "run.implementation_completed",
                    {
                        "command_id": str(command.id),
                        "approval_id": str(approved.approval_id),
                        "execution_id": str(request.execution_id),
                        "output_artifact_id": str(output_id),
                        "queued_command_id": str(queued.id),
                    },
                    actor_class="worker",
                )
            else:
                await work.runs.intervene(
                    run.id,
                    run.version,
                    "run.intervention_required",
                    {
                        "reason": reason,
                        "execution_id": str(request.execution_id),
                        "command_id": str(command.id),
                        "approval_id": str(approved.approval_id),
                    },
                    actor_class="worker",
                )
            await work.commit()
            return outcome
        except CommandLeaseLost, DevelopmentRecoveryRequired:
            await work.rollback()
            # A reclaimed lease may not advance the run, but measured provider
            # usage remains auditable against the already-admitted execution.
            await self._late_usage(work, command, request, usage, attempts, output)
            raise
        except Exception:
            await work.rollback()
            raise

    async def _late_usage(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        request: AgentRequest,
        usage: UsageRecord,
        attempts: tuple[UsageRecord, ...],
        output: ArtifactDescriptor | None,
    ) -> None:
        try:
            await work.runs.get_for_update(command.run_id)
            admission = await work.executions.get_admission(command.run_id, request.execution_id)
            if (
                admission is None
                or admission.agent_execution_id != uuid5(_EXECUTION_NAMESPACE, str(command.id))
                or admission.kind != "implement"
                or admission.role is not AgentRole.DEVELOPER
                or admission.provider != request.provider
                or admission.model != request.model
                or admission.instruction_version != request.instruction_version
            ):
                raise DevelopmentRecoveryRequired("implementation late usage cannot be bound")
            if output is not None:
                await work.artifacts.record(
                    output,
                    run_id=command.run_id,
                    producer_type="developer_result",
                    producer_id=request.execution_id,
                )
            data = self._json(
                {
                    "schema_version": 1,
                    "command_id": str(command.id),
                    "execution_id": str(request.execution_id),
                    "usage": json.loads(usage_attempts_bytes((usage,))).get("attempts"),
                    "attempts": json.loads(usage_attempts_bytes(attempts)).get("attempts"),
                }
            )
            descriptor = await self._put(data)
            await work.artifacts.record(
                descriptor,
                run_id=command.run_id,
                producer_type="developer_late_usage",
                producer_id=request.execution_id,
            )
            await work.commit()
        except Exception:  # noqa: BLE001 - stale receipt failures cannot authorize settlement
            await work.rollback()
            raise DevelopmentRecoveryRequired(
                "implementation late usage requires recovery"
            ) from None

    async def _fence(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        fenced = await work.commands.assert_current_lease(command)
        if (
            fenced.command_type != command.command_type
            or fenced.idempotency_key != command.idempotency_key
            or fenced.payload != command.payload
            or fenced.payload_schema_version != command.payload_schema_version
            or fenced.expected_run_version != command.expected_run_version
            or fenced.actor_id != command.actor_id
        ):
            raise DevelopmentRecoveryRequired("implementation lease differs")

    def _prompt(self) -> LoadedPrompt:
        loaded = self._prompts.load(AgentRole.DEVELOPER)
        if type(loaded) is not LoadedPrompt:
            raise DevelopmentError("developer prompt is invalid")
        return loaded

    async def _put(self, value: bytes) -> ArtifactDescriptor:
        descriptor = await self._store.put_bytes(value, media_type="application/json")
        if (
            type(descriptor) is not ArtifactDescriptor
            or descriptor.digest != hashlib.sha256(value).hexdigest()
        ):
            raise DevelopmentError("developer artifact is invalid")
        return descriptor

    @staticmethod
    def _json(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _validate(command: CommandEnvelope) -> None:
        if (
            type(command) is not CommandEnvelope
            or command.command_type != "implement"
            or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1
            or command.payload != {"semantic_attempt": 1}
            or type(command.payload.get("semantic_attempt")) is not int
            or command.idempotency_key != f"{command.run_id}:implement:1"
        ):
            raise DevelopmentRecoveryRequired("implementation command authority is invalid")


__all__ = ["DevelopmentError", "DevelopmentRecoveryRequired", "DevelopmentService"]
