"""Durably admit, execute, and preserve a fresh Reviewer evaluation."""

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
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceKind,
    EvidenceSetDescriptor,
    ReviewEvidenceDraft,
)
from forge.application.ports.executions import ExecutionUnsettledError, ReviewerEvidenceBinding
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
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    ReviewerInput,
    ReviewOutput,
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
from forge.domain.run import RunState
from forge.domain.tool import ToolName
from forge.domain.validation import command_spec_digest
from forge.observability.usage import UsageRecord

_STEP_NAMESPACE = UUID("11c37116-b982-454c-b101-0f7e4bc4e3ed")
_EXECUTION_NAMESPACE = UUID("29a779ce-d141-498d-b6bc-a18bececf766")
_TOOLS = (
    ToolName.REPOSITORY_LIST_FILES,
    ToolName.REPOSITORY_READ_FILE,
    ToolName.REPOSITORY_SEARCH,
    ToolName.REPOSITORY_READ_INSTRUCTIONS,
    ToolName.GIT_STATUS,
    ToolName.GIT_DIFF,
    ToolName.VALIDATION_RESULTS_READ,
    ToolName.REVIEW_ARTIFACTS_READ,
)


class ReviewError(RuntimeError):
    pass


class ReviewRecoveryRequired(CommandRecoveryRequired, ReviewError):
    pass


class ReviewService:
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

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> EvidenceSetDescriptor:
        attempt, validation_id, prior_id = self._validate(command)
        step_id, execution_id = (
            uuid5(_STEP_NAMESPACE, str(command.id)),
            uuid5(_EXECUTION_NAMESPACE, str(command.id)),
        )
        approved = await self._load_current(command, work)
        worktree, git = self._worktree(approved)
        candidate = git.candidate_diff(worktree)
        validation = await self._validation(
            work, approved, validation_id, candidate.head_sha, prior_id
        )
        outcome = await work.executions.get_outcome(command.run_id, "review", attempt)
        if outcome is not None:
            evidence_id = uuid5(step_id, "review-evidence")
            evidence = await work.evidence.get_by_id(evidence_id, run_id=command.run_id)
            wire = await self._store.open_bytes(evidence.manifest_digest)
            manifest = decode_evidence_manifest(wire)
            if (
                outcome.agent_execution_id != execution_id
                or outcome.step_id != step_id
                or outcome.role is not AgentRole.REVIEWER
                or outcome.finish_status is not AgentFinishStatus.SUCCEEDED
                or outcome.provider != approved.policy.reviewer_model.provider
                or outcome.model != approved.policy.reviewer_model.model
                or evidence.manifest_artifact_id != outcome.output_artifact_id
                or evidence.kind is not EvidenceKind.REVIEW
                or evidence.producer_execution_id != execution_id
                or evidence.head_sha != candidate.head_sha
                or evidence.policy_version != approved.policy.version
                or evidence.validation_evidence_set_id != validation_id
                or evidence.manifest_digest != hashlib.sha256(wire).hexdigest()
                or not isinstance(manifest, ReviewEvidenceManifest)
                or manifest.evidence_set_id != evidence_id
                or manifest.run_id != command.run_id
                or manifest.step_id != step_id
                or manifest.producer_execution_id != execution_id
                or manifest.head_sha != candidate.head_sha
                or manifest.policy_version != approved.policy.version
                or manifest.validation_evidence_set_id != validation_id
            ):
                raise ReviewRecoveryRequired("review replay evidence differs")
            await self._fence(command, work)
            await work.commit()
            return evidence
        await work.commit()
        context = await self._context(approved, worktree, candidate.diff.text, validation)
        prompt = self._prompt()
        request = AgentRequest(
            execution_id=execution_id,
            run_id=approved.run.id,
            task_id=approved.task.id,
            role=AgentRole.REVIEWER,
            parent_execution_id=None,
            context=context,
            provider=approved.policy.reviewer_model.provider,
            model=approved.policy.reviewer_model.model,
            instruction_version=prompt.version,
            system_instruction=prompt.instruction,
            instruction_digest=prompt.digest,
            allowed_tools=_TOOLS,
            budget=AgentBudget.from_model_policy(approved.policy.reviewer_model),
        )
        input_descriptor = await self._put(self._json(context.model_dump(mode="json")))
        await self._admit(
            command,
            work,
            approved,
            request,
            input_descriptor,
            step_id,
            attempt,
            validation_id,
            prior_id,
            candidate.head_sha,
        )
        await work.commit()
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
                validation_id,
                candidate.head_sha,
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
                validation_id,
                candidate.head_sha,
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
                validation_id,
                candidate.head_sha,
                AgentFinishStatus.FAILED,
                error.usage,
                error.usage_attempts,
                None,
                "gateway_failure",
            )
        except AgentGatewayError, PromptChanged, PromptLoadError:
            # Provider outcome may be unknown; the admitted row intentionally blocks reinvocation.
            raise ReviewRecoveryRequired("review gateway requires recovery") from None
        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.REVIEWER
        )
        output = (
            result.output
            if status is AgentFinishStatus.SUCCEEDED and type(result.output) is ReviewOutput
            else None
        )
        return await self._settle(
            command,
            work,
            approved,
            request,
            step_id,
            attempt,
            validation_id,
            candidate.head_sha,
            status
            if output is not None
            else (
                AgentFinishStatus.INVALID_OUTPUT
                if status is AgentFinishStatus.SUCCEEDED
                else status
            ),
            usage,
            attempts,
            output,
            reason or "review_output_invalid",
        )

    async def _load_current(self, command: CommandEnvelope, work: UnitOfWork) -> ApprovedPlan:
        await self._fence(command, work)
        try:
            approved = await self._approved.load(work, command.run_id)
        except ApprovedPlanError:
            raise ReviewRecoveryRequired("review approval authority is invalid") from None
        if (
            approved.run.state is not RunState.REVIEWING
            or approved.run.version != command.expected_run_version
            or command.actor_id != approved.approval_actor_id
        ):
            raise ReviewRecoveryRequired("review run is not current")
        return approved

    def _worktree(self, approved: ApprovedPlan) -> tuple[ManagedWorktree, ControlledGitPort]:
        run = approved.run
        if not run.worktree_path or not run.branch_name or not run.base_sha:
            raise ReviewRecoveryRequired("review worktree is absent")
        tree = ManagedWorktree(
            identity=WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, approved.policy.database.enabled
            ),
            path=Path(run.worktree_path),
            base_sha=run.base_sha,
        )
        git = self._git_factory(approved.policy)
        if git.inspect_worktree(tree.identity, tree.base_sha) != tree or not git.is_ancestor(tree):
            raise ReviewRecoveryRequired("review worktree differs")
        return tree, git

    async def _validation(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        evidence_id: UUID,
        head: str,
        prior: UUID | None,
    ) -> ValidationEvidenceManifest:
        descriptor = await work.evidence.get_by_id(evidence_id, run_id=approved.run.id)
        wire = await self._store.open_bytes(descriptor.manifest_digest)
        manifest = decode_evidence_manifest(wire)
        if (
            not isinstance(manifest, ValidationEvidenceManifest)
            or descriptor.kind is not EvidenceKind.VALIDATION
            or descriptor.policy_version != approved.policy.version
            or descriptor.head_sha != head
            or descriptor.prior_review_evidence_set_id != prior
            or descriptor.manifest_digest != hashlib.sha256(wire).hexdigest()
            or descriptor.manifest_byte_count != len(wire)
            or manifest.evidence_set_id != evidence_id
            or manifest.run_id != approved.run.id
            or manifest.step_id != descriptor.step_id
            or manifest.policy_version != approved.policy.version
            or manifest.head_sha != head
            or manifest.prior_review_evidence_set_id != prior
            or {member.command_name: member.command_digest for member in manifest.members}
            != {spec.name: command_spec_digest(spec) for spec in approved.policy.required_checks}
            or len(manifest.members) != len(approved.policy.required_checks)
            or any(member.status is not EvidenceStatus.PASSED for member in manifest.members)
        ):
            raise ReviewRecoveryRequired("review validation evidence differs")
        return manifest

    async def _context(
        self,
        approved: ApprovedPlan,
        tree: ManagedWorktree,
        diff: str,
        validation: ValidationEvidenceManifest,
    ) -> ReviewerInput:
        try:
            documents = await asyncio.to_thread(
                lambda: tuple(self._reader_factory(approved.policy, tree).read_instructions("."))
            )
            instructions = tuple(
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
        except RepositoryError, TypeError, ValueError:
            raise ReviewError("review context is invalid") from None
        return ReviewerInput(
            original_task=UntrustedContent.from_text(
                approved.task.normalized_text,
                source_kind=UntrustedSourceKind.TASK,
                source_reference=str(approved.task.id),
            ),
            plan=approved.plan,
            current_diff=UntrustedContent.from_text(
                diff, source_kind=UntrustedSourceKind.DIFF, source_reference="candidate_diff"
            ),
            check_evidence=(
                UntrustedContent.from_text(
                    encode_evidence_manifest(validation).decode(),
                    source_kind=UntrustedSourceKind.CHECK,
                    source_reference=str(validation.evidence_set_id),
                ),
            ),
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
        validation_id: UUID,
        prior_id: UUID | None,
        head: str,
    ) -> None:
        current = await self._load_current(command, work)
        if current.approval_id != approved.approval_id or current.run != approved.run:
            raise ReviewRecoveryRequired("review approval changed")
        self._fence_candidate(current, request, head)
        try:
            expected = await work.executions.next_attempt(command.run_id, "review")
        except ExecutionUnsettledError:
            raise ReviewRecoveryRequired("review admission is unsettled") from None
        if expected != attempt:
            raise ReviewRecoveryRequired("review semantic attempt is not next")
        persisted = await work.artifacts.record(
            descriptor,
            run_id=command.run_id,
            producer_type="reviewer_context",
            producer_id=request.execution_id,
        )
        if persisted.artifact_id is None:
            raise ReviewError("review input lineage is invalid")
        admission = await work.executions.admit(
            command.run_id,
            step_id,
            request.execution_id,
            "review",
            attempt,
            AgentRole.REVIEWER,
            request.instruction_version,
            request.provider,
            request.model,
            input_artifact_id=persisted.artifact_id,
            reviewer_input=ReviewerEvidenceBinding(
                validation_evidence_set_id=validation_id,
                prior_review_evidence_set_id=prior_id,
                policy_version=approved.policy.version,
                head_sha=head,
            ),
        )
        if not admission.is_new:
            raise ReviewRecoveryRequired("review admission requires recovery")

    async def _settle(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        request: AgentRequest,
        step_id: UUID,
        attempt: int,
        validation_id: UUID,
        head: str,
        finish: AgentFinishStatus,
        usage: UsageRecord | None,
        attempts: tuple[UsageRecord, ...],
        output: ReviewOutput | None,
        reason: str,
    ) -> EvidenceSetDescriptor:
        usage = safe_usage(usage, request)
        attempts = safe_usage_attempts(usage, attempts, request)
        settled = False
        try:
            current = await self._load_current(command, work)
            if current.approval_id != approved.approval_id or current.run != approved.run:
                raise ReviewRecoveryRequired("review approval changed")
            self._fence_candidate(current, request, head)
            for item in attempts:
                await work.artifacts.record(
                    await self._put(usage_attempts_bytes((item,))),
                    run_id=command.run_id,
                    producer_type="agent_usage_attempts",
                    producer_id=request.execution_id,
                )
            output_id = None
            if output is not None:
                evidence_id = uuid5(step_id, "review-evidence")
                manifest = ReviewEvidenceManifest(
                    evidence_set_id=evidence_id,
                    run_id=command.run_id,
                    step_id=step_id,
                    policy_version=approved.policy.version,
                    head_sha=head,
                    producer_execution_id=request.execution_id,
                    validation_evidence_set_id=validation_id,
                    review=output,
                )
                wire = encode_evidence_manifest(manifest)
                stored = await self._store.put_bytes(
                    wire, media_type="application/vnd.forge.evidence-manifest+json"
                )
                validation = await work.evidence.get_by_id(validation_id, run_id=command.run_id)
                artifact = await work.artifacts.record(
                    stored,
                    run_id=command.run_id,
                    producer_type="evidence_set",
                    producer_id=evidence_id,
                    parent_digests=(validation.manifest_digest,),
                )
                evidence = await work.evidence.record_set(
                    ReviewEvidenceDraft(manifest),
                    CanonicalEvidenceArtifact(artifact, manifest, wire),
                )
                output_id = artifact.artifact_id
            self._fence_candidate(current, request, head)
            await self._fence(command, work)
            await work.executions.finalize(
                command.run_id,
                step_id,
                request.execution_id,
                finish,
                usage,
                output_artifact_id=output_id,
                provider=request.provider,
                model=request.model,
                instruction_version=request.instruction_version,
                kind="review",
                attempt=attempt,
                role=AgentRole.REVIEWER,
            )
            if finish is not AgentFinishStatus.SUCCEEDED:
                await work.runs.intervene(
                    current.run.id,
                    current.run.version,
                    "run.intervention_required",
                    {"reason": reason, "execution_id": str(request.execution_id)},
                    actor_class="worker",
                )
            await work.commit()
            settled = True
            if output is None:
                raise ReviewRecoveryRequired("review did not produce evidence")
            return evidence
        except CommandLeaseLost, ReviewRecoveryRequired:
            await work.rollback()
            if not settled:
                await self._late_result(
                    work, command, request, head, finish, usage, attempts, output
                )
            raise

    async def _late_result(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        request: AgentRequest,
        head: str,
        finish: AgentFinishStatus,
        usage: UsageRecord,
        attempts: tuple[UsageRecord, ...],
        output: ReviewOutput | None,
    ) -> None:
        """Retain a known outcome without certifying a stale candidate."""
        try:
            await work.runs.get_for_update(command.run_id)
            admission = await work.executions.get_admission(command.run_id, request.execution_id)
            if (
                admission is None
                or admission.agent_execution_id != uuid5(_EXECUTION_NAMESPACE, str(command.id))
                or admission.step_id != uuid5(_STEP_NAMESPACE, str(command.id))
                or admission.role is not AgentRole.REVIEWER
                or admission.provider != request.provider
                or admission.model != request.model
                or admission.instruction_version != request.instruction_version
            ):
                raise ReviewRecoveryRequired("review late result cannot be bound")
            wire = self._json(
                {
                    "schema_version": 1,
                    "command_id": str(command.id),
                    "execution_id": str(request.execution_id),
                    "head_sha": head,
                    "finish_status": finish.value,
                    "output": output.model_dump(mode="json") if output is not None else None,
                    "usage": json.loads(usage_attempts_bytes((usage,)))["attempts"],
                    "attempts": json.loads(usage_attempts_bytes(attempts))["attempts"],
                }
            )
            await work.artifacts.record(
                await self._put(wire),
                run_id=command.run_id,
                producer_type="review_late_result",
                producer_id=request.execution_id,
            )
            await work.commit()
        except Exception:  # noqa: BLE001 - late evidence cannot authorize candidate publication
            await work.rollback()
            raise ReviewRecoveryRequired("review late result requires recovery") from None

    def _fence_candidate(self, approved: ApprovedPlan, request: AgentRequest, head: str) -> None:
        try:
            tree, git = self._worktree(approved)
            candidate = git.candidate_diff(tree)
        except Exception:  # noqa: BLE001 - a failed candidate inspection requires reconciliation
            raise ReviewRecoveryRequired("review candidate requires recovery") from None
        if (
            not isinstance(request.context, ReviewerInput)
            or candidate.head_sha != head
            or candidate.diff.truncated
            or hashlib.sha256(candidate.diff.text.encode("utf-8")).hexdigest()
            != request.context.current_diff.content_digest
        ):
            raise ReviewRecoveryRequired("review candidate changed")

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
            raise ReviewRecoveryRequired("review lease differs")

    def _prompt(self) -> LoadedPrompt:
        loaded = self._prompts.load(AgentRole.REVIEWER)
        if type(loaded) is not LoadedPrompt:
            raise ReviewError("review prompt is invalid")
        return loaded

    async def _put(self, value: bytes) -> ArtifactDescriptor:
        descriptor = await self._store.put_bytes(value, media_type="application/json")
        if (
            type(descriptor) is not ArtifactDescriptor
            or descriptor.digest != hashlib.sha256(value).hexdigest()
        ):
            raise ReviewError("review artifact is invalid")
        return descriptor

    @staticmethod
    def _json(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _validate(command: CommandEnvelope) -> tuple[int, UUID, UUID | None]:
        payload = command.payload
        attempt = payload.get("semantic_attempt")
        if (
            type(command) is not CommandEnvelope
            or command.command_type != "review"
            or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1
            or type(attempt) is not int
            or attempt < 1
            or command.idempotency_key != f"{command.run_id}:review:{attempt}"
        ):
            raise ReviewRecoveryRequired("review command authority is invalid")
        try:
            validation = UUID(str(payload["validation_evidence_set_id"]))
            prior = (
                None
                if "prior_review_evidence_set_id" not in payload
                else UUID(str(payload["prior_review_evidence_set_id"]))
            )
        except KeyError, ValueError, TypeError:
            raise ReviewRecoveryRequired("review evidence authority is invalid") from None
        expected = {"semantic_attempt": attempt, "validation_evidence_set_id": str(validation)}
        if prior is not None:
            expected["prior_review_evidence_set_id"] = str(prior)
        if payload != expected:
            raise ReviewRecoveryRequired("review command authority is invalid")
        return attempt, validation, prior


__all__ = ["ReviewError", "ReviewRecoveryRequired", "ReviewService"]
