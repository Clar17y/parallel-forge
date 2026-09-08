"""Durably admit and settle approved Developer and remediation executions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast
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
from forge.application.ports.commands import (
    CommandLeaseLost,
    CommandRecoveryRequired,
    CommandSuspended,
)
from forge.application.ports.evidence import EvidenceKind
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
from forge.application.services.control_settlement import (
    controlled_stop,
    pending_current_control_stop,
)
from forge.application.services.developer_result import verify_developer_output
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
    DeveloperInput,
    DeveloperOutput,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.evidence import (
    EvidenceStatus,
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
    encode_evidence_manifest,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.review import ReviewFinding
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import ToolName
from forge.domain.validation import command_spec_digest
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
        origin = await resume_origin(work, command)
        attempt, validation_id, prior_review_id = self._validate(command, origin)
        step_id, execution_id = (
            uuid5(_STEP_NAMESPACE, str(command.id)),
            uuid5(_EXECUTION_NAMESPACE, str(command.id)),
        )
        replay = await self._replay(
            command, work, step_id, execution_id, attempt, validation_id, prior_review_id
        )
        if replay is not None:
            return replay
        approved = await self._load_current(command, work, origin)
        worktree, git = self._worktree(approved)
        # Do not retain database locks while reading untrusted repository files.
        await work.commit()
        context = await self._context(
            approved,
            worktree,
            work,
            validation_id,
            prior_review_id,
            cast(str | None, command.payload.get("feedback_digest")),
            UUID(str(command.payload["feedback_command_id"]))
            if command.payload.get("feedback_command_id") is not None
            else None,
        )
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
        input_descriptor = await self._put(
            self._json(
                {
                    "schema_version": 1,
                    "execution_id": str(execution_id),
                    "context": context.model_dump(mode="json"),
                }
            )
        )
        try:
            await self._admit(
                command,
                work,
                approved,
                request,
                input_descriptor,
                step_id,
                attempt,
                validation_id,
                prior_review_id,
            )
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
                attempt,
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
                attempt,
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
                attempt,
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
                attempt,
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
                attempt,
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
                attempt,
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
            attempt,
            AgentFinishStatus.SUCCEEDED,
            usage,
            attempts,
            descriptor,
            "",
            verified_output=output,
        )

    async def _replay(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        step_id: UUID,
        execution_id: UUID,
        attempt: int,
        validation_id: UUID | None,
        prior_review_id: UUID | None,
    ) -> ExecutionOutcome | None:
        await self._fence(command, work)
        outcome = await work.executions.get_outcome(command.run_id, "implement", attempt)
        if outcome is None:
            return None
        approved = await self._approved.load(work, command.run_id)
        succeeded = outcome.finish_status is AgentFinishStatus.SUCCEEDED
        target = RunState.VALIDATING if succeeded else RunState.AWAITING_HUMAN_INTERVENTION
        if (
            command.payload.get("automatic") is not False
            and command.actor_id != approved.approval_actor_id
            or outcome.agent_execution_id != execution_id
            or outcome.step_id != step_id
            or outcome.role is not AgentRole.DEVELOPER
            or outcome.provider != approved.policy.developer_model.provider
            or outcome.model != approved.policy.developer_model.model
            or approved.run.state is not target
            or approved.run.version != command.expected_run_version + 1
        ):
            raise DevelopmentRecoveryRequired("implementation replay authority differs")
        event_type = (
            (
                "run.remediation_completed"
                if command.command_type == "remediate"
                else "run.implementation_completed"
            )
            if succeeded
            else "run.intervention_required"
        )
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
            or events[0].payload.get("command_payload") != command.payload
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
            next_validation = await work.controller_steps.next_attempt(command.run_id, "validate")
            queued = await work.commands.get_by_idempotency_key(
                f"{command.run_id}:validate:{next_validation}"
            )
            expected_payload: dict[str, object] = {"semantic_attempt": next_validation}
            if prior_review_id is not None:
                expected_payload["prior_review_evidence_set_id"] = str(prior_review_id)
            if (
                queued is None
                or queued.command_type != "validate"
                or queued.status is not CommandStatus.PENDING
                or queued.payload != expected_payload
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

    async def _load_current(
        self, command: CommandEnvelope, work: UnitOfWork, origin: CommandEnvelope | None = None
    ) -> ApprovedPlan:
        if origin is None:
            origin = await resume_origin(work, command)
        await self._fence(command, work)
        try:
            approved = await self._approved.load(work, command.run_id)
        except ApprovedPlanError:
            raise DevelopmentRecoveryRequired(
                "implementation approval authority is invalid"
            ) from None
        if (
            approved.run.state
            is not (
                RunState.REMEDIATING
                if command.command_type == "remediate"
                else RunState.IMPLEMENTING
            )
            or approved.run.version != command.expected_run_version
        ):
            raise DevelopmentRecoveryRequired("implementation run is not current")
        if (
            command.payload.get("automatic") is not False
            and command.actor_id != approved.approval_actor_id
        ):
            raise DevelopmentRecoveryRequired("implementation actor is invalid")
        if command.command_type == "remediate":
            authority = origin or command
            _attempt, validation_id, _prior_review_id = self._validate(command, origin)
            if authority.payload.get("automatic") is False:
                events = [
                    event
                    for event in await work.events.list_after(command.run_id, 0)
                    if event.event_type == "run.candidate_revision_requested"
                    and event.payload.get("queued_command_id") == str(authority.id)
                ]
                if (
                    len(events) != 1
                    or events[0].actor_class != "operator"
                    or events[0].actor_id != command.actor_id
                    or events[0].run_version != authority.expected_run_version
                    or events[0].payload.get("queued_payload") != authority.payload
                    or events[0].payload.get("queued_key") != authority.idempotency_key
                    or events[0].payload.get("local_remediation_count")
                    != approved.run.local_remediation_count
                    or events[0].payload.get("approval_id") != str(approved.approval_id)
                    or events[0].payload.get("source_command_id")
                    != authority.payload["feedback_command_id"]
                ):
                    raise DevelopmentRecoveryRequired("human remediation authority differs")
                source = await work.commands.get(
                    UUID(str(authority.payload["feedback_command_id"]))
                )
                if (
                    source.run_id != command.run_id
                    or source.command_type != "request_candidate_changes"
                    or source.status is not CommandStatus.COMPLETED
                    or source.actor_id != command.actor_id
                    or source.payload_schema_version != 1
                    or source.expected_run_version + 1 != authority.expected_run_version
                    or set(source.payload) != {"feedback"}
                ):
                    raise DevelopmentRecoveryRequired("human feedback command authority differs")
                return approved
            events = [
                event
                for event in await work.events.list_after(command.run_id, 0)
                if event.event_type in {"run.validation_decided", "run.review_decided"}
                and event.run_version == authority.expected_run_version
                and event.payload.get("queued_command_id") == str(authority.id)
            ]
            event = events[0] if len(events) == 1 else None
            expected_payload = None if event is None else event.payload.get("queued_payload")
            if event is not None and event.event_type == "run.review_decided":
                expected_payload = {
                    "semantic_attempt": event.payload.get("semantic_attempt"),
                    "validation_evidence_set_id": event.payload.get("validation_evidence_set_id"),
                    "prior_review_evidence_set_id": event.payload.get("review_evidence_set_id"),
                    "automatic": True,
                }
            if (
                len(events) != 1
                or event is None
                or event.actor_class != "worker"
                or event.actor_id is not None
                or event.payload.get("approval_id") != str(approved.approval_id)
                or event.payload.get("validation_evidence_set_id") != str(validation_id)
                or expected_payload != authority.payload
                or event.payload.get("queued_key") != authority.idempotency_key
                or event.payload.get("target") != RunState.REMEDIATING.value
                or event.payload.get("local_remediation_count")
                != approved.run.local_remediation_count
                or approved.run.local_remediation_count < 1
                or approved.run.local_remediation_count > approved.evidence.local_remediation_limit
            ):
                raise DevelopmentRecoveryRequired("remediation decision authority differs")
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

    async def _context(
        self,
        approved: ApprovedPlan,
        worktree: ManagedWorktree,
        work: UnitOfWork,
        validation_id: UUID | None,
        prior_review_id: UUID | None,
        feedback_digest: str | None = None,
        feedback_command_id: UUID | None = None,
    ) -> DeveloperInput:
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
        check_evidence: tuple[UntrustedContent, ...] = ()
        findings: tuple[ReviewFinding, ...] = ()
        operator_feedback = None
        if feedback_digest is not None:
            descriptor = await work.artifacts.get_by_digest(feedback_digest, run_id=approved.run.id)
            if (
                descriptor.producer_type != "candidate_revision_feedback"
                or feedback_command_id is None
                or descriptor.producer_id != feedback_command_id
                or descriptor.media_type != "application/json"
                or descriptor.schema_version != 1
                or descriptor.truncated
            ):
                raise DevelopmentRecoveryRequired("operator feedback authority differs")
            wire = await self._store.open_bytes(feedback_digest)
            try:
                payload = json.loads(wire)
                feedback = payload["feedback"]
            except ValueError, UnicodeError, KeyError, TypeError:
                raise DevelopmentRecoveryRequired("operator feedback artifact is invalid") from None
            if (
                not isinstance(feedback, str)
                or not feedback.strip()
                or len(feedback.encode("utf-8")) > 16_384
                or "\x00" in feedback
                or descriptor.byte_count != len(wire)
                or payload
                != {
                    "schema_version": 1,
                    "command_id": str(feedback_command_id),
                    "feedback": feedback,
                }
                or wire != self._json(payload)
                or hashlib.sha256(wire).hexdigest() != feedback_digest
            ):
                raise DevelopmentRecoveryRequired("operator feedback artifact is invalid")
            operator_feedback = UntrustedContent.from_text(
                feedback,
                source_kind=UntrustedSourceKind.OPERATOR_FEEDBACK,
                source_reference=feedback_digest,
            )
        if validation_id is not None:
            validation, review = await self._remediation_evidence(
                work,
                approved,
                validation_id,
                prior_review_id,
                require_blocking=feedback_digest is None,
            )
            check_evidence = (
                UntrustedContent.from_text(
                    encode_evidence_manifest(validation).decode(),
                    source_kind=UntrustedSourceKind.CHECK,
                    source_reference=str(validation_id),
                ),
            )
            findings = (
                ()
                if review is None
                else tuple(f for f in review.review.findings if not f.is_resolved)
            )
        return DeveloperInput(
            original_task=UntrustedContent.from_text(
                approved.task.normalized_text,
                source_kind=UntrustedSourceKind.TASK,
                source_reference=str(approved.task.id),
            ),
            plan=approved.plan,
            worktree_id=worktree.identity.worktree_name,
            base_commit=worktree.base_sha,
            operator_feedback=operator_feedback,
            remediation_findings=findings,
            check_evidence=check_evidence,
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
        attempt: int,
        validation_id: UUID | None,
        prior_review_id: UUID | None,
    ) -> None:
        current = await self._load_current(command, work)
        if current.approval_id != approved.approval_id or current.run != approved.run:
            raise DevelopmentRecoveryRequired("implementation approval changed")
        self._worktree(current)
        try:
            expected = await work.executions.next_attempt(command.run_id, "implement")
        except ExecutionUnsettledError:
            raise DevelopmentRecoveryRequired("implementation admission is unsettled") from None
        if expected != attempt:
            raise DevelopmentRecoveryRequired("developer semantic attempt is not next")
        parent_digests: tuple[str, ...] = ()
        if validation_id is not None:
            # Re-read immediately before durable admission: the context may only
            # claim authority from the immutable manifests it names as parents.
            validation, review = await self._remediation_evidence(
                work,
                current,
                validation_id,
                prior_review_id,
                require_blocking=command.payload.get("automatic") is not False,
            )
            expected_check = UntrustedContent.from_text(
                encode_evidence_manifest(validation).decode(),
                source_kind=UntrustedSourceKind.CHECK,
                source_reference=str(validation_id),
            )
            expected_findings = (
                ()
                if review is None
                else tuple(f for f in review.review.findings if not f.is_resolved)
            )
            if (
                not isinstance(request.context, DeveloperInput)
                or request.context.check_evidence != (expected_check,)
                or request.context.remediation_findings != expected_findings
            ):
                raise DevelopmentRecoveryRequired("remediation context differs from evidence")
            validation_descriptor = await work.evidence.get_by_id(
                validation.evidence_set_id, run_id=current.run.id
            )
            parent_digests = (validation_descriptor.manifest_digest,)
            if review is not None:
                review_descriptor = await work.evidence.get_by_id(
                    review.evidence_set_id, run_id=current.run.id
                )
                parent_digests += (review_descriptor.manifest_digest,)
        if command.payload.get("automatic") is False:
            parent_digests += (cast(str, command.payload["feedback_digest"]),)
        persisted = await work.artifacts.record(
            descriptor,
            run_id=command.run_id,
            producer_type="developer_context",
            producer_id=request.execution_id,
            parent_digests=tuple(sorted(set(parent_digests))),
        )
        if persisted.artifact_id is None:
            raise DevelopmentError("developer input lineage is invalid")
        if validation_id is not None:
            await self._remediation_evidence(
                work,
                current,
                validation_id,
                prior_review_id,
                require_blocking=command.payload.get("automatic") is not False,
            )
        await self._fence(command, work)
        admission = await work.executions.admit(
            command.run_id,
            step_id,
            request.execution_id,
            "implement",
            attempt,
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
        attempt: int,
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
            if await pending_current_control_stop(work, current.run):
                raise DevelopmentRecoveryRequired(
                    "implementation finalization is fenced by operator control"
                )
            for usage_attempt in attempts:
                d = await self._put(usage_attempts_bytes((usage_attempt,)))
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
                attempt=attempt,
                role=AgentRole.DEVELOPER,
            )
            run = current.run
            if finish is AgentFinishStatus.SUCCEEDED:
                next_validation = await work.controller_steps.next_attempt(run.id, "validate")
                payload: dict[str, object] = {"semantic_attempt": next_validation}
                if command.command_type == "remediate":
                    prior = command.payload.get("prior_review_evidence_set_id")
                    if isinstance(prior, str):
                        payload["prior_review_evidence_set_id"] = prior
                queued = await work.commands.enqueue(
                    run_id=run.id,
                    command_type="validate",
                    idempotency_key=f"{run.id}:validate:{next_validation}",
                    payload=payload,
                    expected_run_version=run.version + 1,
                    actor_id=approved.approval_actor_id,
                )
                if (
                    queued.status is not CommandStatus.PENDING
                    or queued.command_type != "validate"
                    or queued.payload != payload
                    or queued.payload_schema_version != 1
                    or queued.expected_run_version != run.version + 1
                    or queued.actor_id != approved.approval_actor_id
                ):
                    raise DevelopmentRecoveryRequired("validation command is not pending")
                await work.runs.transition(
                    run.id,
                    run.version,
                    RunState.VALIDATING,
                    "run.remediation_completed"
                    if command.command_type == "remediate"
                    else "run.implementation_completed",
                    {
                        "command_id": str(command.id),
                        "command_payload": dict(command.payload),
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
                        "command_payload": dict(command.payload),
                        "approval_id": str(approved.approval_id),
                    },
                    actor_class="worker",
                )
            await work.commit()
            return outcome
        except CommandLeaseLost, DevelopmentRecoveryRequired:
            await work.rollback()
            if await controlled_stop(work, command, approved.run):
                await self._late_usage(
                    work,
                    command,
                    request,
                    usage,
                    attempts,
                    output,
                    cancelled=True,
                    admitted_run=approved.run,
                )
                raise CommandSuspended("developer stopped by operator control") from None
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
        *,
        cancelled: bool = False,
        admitted_run: RunSnapshot | None = None,
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
            output_id = None
            if output is not None:
                # A late observation belongs to this execution even when its
                # developer result bytes match a later accepted execution.
                # Keep it in an execution-bound receipt so immutable artifact
                # lineage cannot alias the successful result.
                late_output = await self._put(
                    self._json(
                        {
                            "schema_version": 1,
                            "command_id": str(command.id),
                            "execution_id": str(request.execution_id),
                            "output_digest": output.digest,
                            "output": json.loads(await self._store.open_bytes(output.digest)),
                        }
                    )
                )
                persisted_output = await work.artifacts.record(
                    late_output,
                    run_id=command.run_id,
                    producer_type="developer_late_result",
                    producer_id=request.execution_id,
                )
                output_id = persisted_output.artifact_id
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
            if cancelled:
                await work.executions.finalize(
                    command.run_id,
                    admission.step_id,
                    request.execution_id,
                    AgentFinishStatus.CANCELLED,
                    usage,
                    output_artifact_id=output_id,
                    provider=request.provider,
                    model=request.model,
                    instruction_version=request.instruction_version,
                    kind="implement",
                    attempt=admission.attempt,
                    role=AgentRole.DEVELOPER,
                )
                if admitted_run is None:
                    raise DevelopmentRecoveryRequired(
                        "suspended implementation admission is absent"
                    )
                await record_suspended_delivery(
                    work,
                    command,
                    admitted_run,
                    admission.step_id,
                    "implement",
                    admission.attempt,
                )
            await work.commit()
        except Exception:  # noqa: BLE001 - stale receipt failures cannot authorize settlement
            await work.rollback()
            raise DevelopmentRecoveryRequired(
                "implementation late usage requires recovery"
            ) from None

    async def _remediation_evidence(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        validation_id: UUID,
        prior_review_id: UUID | None,
        *,
        require_blocking: bool = True,
    ) -> tuple[ValidationEvidenceManifest, ReviewEvidenceManifest | None]:
        """Load only immutable controller evidence named by the leased command."""
        descriptor = await work.evidence.get_by_id(validation_id, run_id=approved.run.id)
        wire = await self._store.open_bytes(descriptor.manifest_digest)
        validation = decode_evidence_manifest(wire)
        worktree, git = self._worktree(approved)
        if (
            not isinstance(validation, ValidationEvidenceManifest)
            or descriptor.kind is not EvidenceKind.VALIDATION
            or descriptor.policy_version != approved.policy.version
            or descriptor.manifest_digest != hashlib.sha256(wire).hexdigest()
            or descriptor.manifest_byte_count != len(wire)
            or validation.evidence_set_id != validation_id
            or validation.run_id != approved.run.id
            or validation.policy_version != approved.policy.version
            or descriptor.head_sha != validation.head_sha
            or descriptor.step_id != validation.step_id
            or descriptor.producer_execution_id is not None
            or descriptor.prior_review_evidence_set_id != validation.prior_review_evidence_set_id
            or git.head_sha(worktree) != validation.head_sha
            or {m.command_name: m.command_digest for m in validation.members}
            != {s.name: command_spec_digest(s) for s in approved.policy.required_checks}
            or len(validation.members) != len(approved.policy.required_checks)
        ):
            raise DevelopmentRecoveryRequired("remediation validation evidence differs")
        review = None
        if prior_review_id is not None:
            prior = await work.evidence.get_by_id(prior_review_id, run_id=approved.run.id)
            review_wire = await self._store.open_bytes(prior.manifest_digest)
            decoded_review = decode_evidence_manifest(review_wire)
            review = cast(ReviewEvidenceManifest | None, decoded_review)
            if (
                not isinstance(review, ReviewEvidenceManifest)
                or prior.kind is not EvidenceKind.REVIEW
                or prior.policy_version != approved.policy.version
                or prior.manifest_digest != hashlib.sha256(review_wire).hexdigest()
                or prior.manifest_byte_count != len(review_wire)
                or review.evidence_set_id != prior_review_id
                or review.run_id != approved.run.id
                or review.policy_version != approved.policy.version
                or prior.step_id != review.step_id
                or prior.head_sha != review.head_sha
                or prior.producer_execution_id != review.producer_execution_id
                or prior.validation_evidence_set_id != review.validation_evidence_set_id
                or not (
                    validation.prior_review_evidence_set_id == prior_review_id
                    or (
                        review.validation_evidence_set_id == validation_id
                        and review.head_sha == validation.head_sha
                    )
                )
            ):
                raise DevelopmentRecoveryRequired("remediation review evidence differs")
        if (
            require_blocking
            and all(member.status is EvidenceStatus.PASSED for member in validation.members)
            and (
                review is None
                or review.validation_evidence_set_id != validation_id
                or review.head_sha != validation.head_sha
                or (
                    not review.review.missing_evidence
                    and not approved.policy.blocks_publication(review.review.findings)
                )
            )
        ):
            raise DevelopmentRecoveryRequired("remediation has no blocking controller evidence")
        return validation, review

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
    def _validate(
        command: CommandEnvelope, origin: CommandEnvelope | None = None
    ) -> tuple[int, UUID | None, UUID | None]:
        resumed = origin is not None
        if origin is not None:
            resume = resume_command_ids(command.payload)
            if resume is None:
                raise DevelopmentRecoveryRequired("resume implementation authority is invalid")
            resume_id, _source_id = resume
            payload = {
                key: value for key, value in command.payload.items() if key not in RESUME_FIELDS
            }
            if command.idempotency_key != (
                f"{command.run_id}:resume:{resume_id}:{command.command_type}:{payload.get('semantic_attempt')}"
            ):
                raise DevelopmentRecoveryRequired("resume implementation authority is invalid")
            DevelopmentService._validate(
                replace(
                    origin,
                    status=CommandStatus.LEASED,
                    lease_owner=command.lease_owner,
                    lease_expires_at=command.lease_expires_at,
                    completed_at=None,
                )
            )
            if {key: value for key, value in payload.items() if key != "semantic_attempt"} != {
                key: value for key, value in origin.payload.items() if key != "semantic_attempt"
            }:
                raise DevelopmentRecoveryRequired("resume implementation authority differs")
            command = replace(
                command,
                payload=payload,
                idempotency_key=(
                    f"{command.run_id}:human-remediate:{payload['semantic_attempt']}"
                    if payload.get("automatic") is False
                    else f"{command.run_id}:{command.command_type}:{payload['semantic_attempt']}"
                ),
            )
        elif resume_command_ids(command.payload) is not None:
            raise DevelopmentRecoveryRequired("resume implementation authority is invalid")
        attempt = command.payload.get("semantic_attempt")
        validation_value = command.payload.get("validation_evidence_set_id")
        prior_value = command.payload.get("prior_review_evidence_set_id")
        remediation = command.command_type == "remediate"
        if (
            type(command) is not CommandEnvelope
            or command.command_type not in {"implement", "remediate"}
            or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1
            or type(attempt) is not int
            or attempt < 1
            or command.idempotency_key
            != (
                f"{command.run_id}:human-remediate:{attempt}"
                if remediation and command.payload.get("automatic") is False
                else f"{command.run_id}:{command.command_type}:{attempt}"
            )
        ):
            raise DevelopmentRecoveryRequired("implementation command authority is invalid")
        if not remediation:
            if command.payload != {"semantic_attempt": attempt} or (attempt != 1 and not resumed):
                raise DevelopmentRecoveryRequired("implementation command authority is invalid")
            return attempt, None, None
        if command.payload.get("automatic") is False:
            if set(command.payload) != {
                "semantic_attempt",
                "automatic",
                "feedback_digest",
                "feedback_command_id",
                "pr_evidence_digest",
                "candidate_commit",
                "validation_evidence_set_id",
                "prior_review_evidence_set_id",
            }:
                raise DevelopmentRecoveryRequired("human remediation authority is invalid")
            try:
                feedback_command_id = UUID(str(command.payload["feedback_command_id"]))
            except TypeError, ValueError:
                raise DevelopmentRecoveryRequired("human feedback authority is invalid") from None
            if not feedback_command_id.int or not isinstance(
                command.payload["feedback_digest"], str
            ):
                raise DevelopmentRecoveryRequired("human feedback authority is invalid")
            try:
                human_validation_id = UUID(str(command.payload["validation_evidence_set_id"]))
                human_prior_id = UUID(str(command.payload["prior_review_evidence_set_id"]))
            except ValueError:
                raise DevelopmentRecoveryRequired("human evidence authority is invalid") from None
            return attempt, human_validation_id, human_prior_id
        if command.payload.get("automatic") is not True or set(command.payload) - {
            "semantic_attempt",
            "validation_evidence_set_id",
            "prior_review_evidence_set_id",
            "automatic",
        }:
            raise DevelopmentRecoveryRequired("remediation command authority is invalid")
        try:
            validation_id = UUID(validation_value) if isinstance(validation_value, str) else None
            prior_id = UUID(prior_value) if isinstance(prior_value, str) else None
        except ValueError:
            raise DevelopmentRecoveryRequired(
                "remediation evidence identifier is invalid"
            ) from None
        if (
            validation_id is None
            or not validation_id.int
            or str(validation_id) != validation_value
            or (
                "prior_review_evidence_set_id" in command.payload
                and (prior_id is None or not prior_id.int or str(prior_id) != prior_value)
            )
        ):
            raise DevelopmentRecoveryRequired("remediation evidence identifier is invalid")
        return attempt, validation_id, prior_id


__all__ = ["DevelopmentError", "DevelopmentRecoveryRequired", "DevelopmentService"]
