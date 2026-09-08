"""Atomically turn one completed reviewer result into the next delivery state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

from forge.application.adapters.named_check_receipts import decode_command_result
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.evidence import EvidenceKind, EvidenceSetDescriptor
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.approved_plan import (
    ApprovedPlan,
    ApprovedPlanError,
    ApprovedPlanLoader,
)
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.resume_source import resume_origin
from forge.application.services.review import _EXECUTION_NAMESPACE, _STEP_NAMESPACE, ReviewService
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.domain.approval import ApprovalGate, PrApprovalEvidence, canonical_digest
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.evidence import (
    EvidenceStatus,
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
)
from forge.domain.policy import ProjectPolicy, RunnerMode
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.validation import command_spec_digest, effective_network_enabled


class ReviewDecisionRecoveryRequired(CommandRecoveryRequired):
    pass


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    run_id: UUID
    state: RunState
    version: int
    review_evidence_set_id: UUID


class ReviewDecisionService:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
        approved_plans: ApprovedPlanLoader | None = None,
    ) -> None:
        self._store, self._git_factory = artifact_store, git_factory
        self._approved = approved_plans or ApprovedPlanLoader(artifact_store)

    async def decide(self, command: CommandEnvelope, work: UnitOfWork) -> ReviewDecision:
        attempt, validation_id, prior_id = self._command(
            command, await resume_origin(work, command)
        )
        await self._fence(command, work)
        try:
            approved = await self._approved.load(work, command.run_id)
        except ApprovedPlanError:
            raise ReviewDecisionRecoveryRequired("review decision approval is invalid") from None
        if command.actor_id != approved.approval_actor_id:
            raise ReviewDecisionRecoveryRequired("review decision run is not current")
        review_id = uuid5(uuid5(_STEP_NAMESPACE, str(command.id)), "review-evidence")
        review, validation, candidate = await self._evidence(
            work, approved, review_id, validation_id, prior_id
        )
        execution = await work.executions.get_outcome(command.run_id, "review", attempt)
        if (
            execution is None
            or execution.agent_execution_id != uuid5(_EXECUTION_NAMESPACE, str(command.id))
            or execution.step_id != review.step_id
            or execution.agent_execution_id != review.producer_execution_id
            or execution.role is not AgentRole.REVIEWER
            or execution.finish_status is not AgentFinishStatus.SUCCEEDED
            or execution.provider != approved.policy.reviewer_model.provider
            or execution.model != approved.policy.reviewer_model.model
            or execution.output_artifact_id != candidate[0].manifest_artifact_id
        ):
            raise ReviewDecisionRecoveryRequired("review decision execution differs")
        prior_events = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == "run.review_decided"
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if prior_events:
            return await self._replay(
                command, work, approved, review, validation, candidate, prior_events
            )
        if (
            approved.run.state is not RunState.REVIEWING
            or approved.run.version != command.expected_run_version
        ):
            raise ReviewDecisionRecoveryRequired("review decision run is not current")
        blocking = bool(review.review.missing_evidence) or approved.policy.blocks_publication(
            review.review.findings
        )
        payload: dict[str, object] = {
            "source_command_id": str(command.id),
            "approval_id": str(approved.approval_id),
            "review_evidence_set_id": str(review_id),
            "review_digest": candidate[0].manifest_digest,
            "validation_evidence_set_id": str(validation_id),
            "validation_digest": candidate[1].manifest_digest,
            "queued_key": None,
            "local_remediation_count": approved.run.local_remediation_count,
        }
        if blocking:
            if approved.run.local_remediation_count >= approved.evidence.local_remediation_limit:
                await self._refence(command, work, approved, candidate)
                run = await work.runs.intervene(
                    command.run_id,
                    approved.run.version,
                    "run.review_decided",
                    payload | {"target": RunState.AWAITING_HUMAN_INTERVENTION.value},
                    actor_class="worker",
                )
                await work.commit()
                return ReviewDecision(run.id, run.state, run.version, review_id)
            next_attempt = await work.executions.next_attempt(command.run_id, "implement")
            payload |= {"target": RunState.REMEDIATING.value, "semantic_attempt": next_attempt}
            run = approved.run
            queued_payload = {
                "semantic_attempt": next_attempt,
                "validation_evidence_set_id": str(validation_id),
                "prior_review_evidence_set_id": str(review_id),
                "automatic": True,
            }
            queued = await work.commands.enqueue(
                run_id=run.id,
                command_type="remediate",
                idempotency_key=f"{run.id}:remediate:{next_attempt}",
                payload=queued_payload,
                expected_run_version=run.version + 1,
                actor_id=approved.approval_actor_id,
            )
            if (
                queued.status is not CommandStatus.PENDING
                or queued.command_type != "remediate"
                or queued.payload != queued_payload
                or queued.payload_schema_version != 1
                or queued.actor_id != approved.approval_actor_id
                or queued.expected_run_version != run.version + 1
            ):
                raise ReviewDecisionRecoveryRequired("review remediation queue differs")
            payload.update(
                queued_key=queued.idempotency_key,
                queued_command_id=str(queued.id),
                local_remediation_count=run.local_remediation_count + 1,
            )
            await self._refence(command, work, approved, candidate)
            run = await work.runs.begin_local_remediation(
                command.run_id,
                run.version,
                automatic=True,
                limit=approved.evidence.local_remediation_limit,
                event_type="run.review_decided",
                event_payload=payload,
                actor_class="worker",
            )
            await work.commit()
            return ReviewDecision(run.id, run.state, run.version, review_id)
        evidence_digest = await self._freeze(work, approved, review_id, validation_id, candidate)
        await self._refence(command, work, approved, candidate)
        run = await work.runs.await_approval(
            command.run_id,
            approved.run.version,
            ApprovalGate.PR,
            evidence_digest,
            "run.review_decided",
            payload
            | {
                "target": RunState.AWAITING_PR_APPROVAL.value,
                "pr_evidence_digest": evidence_digest,
            },
            actor_class="worker",
        )
        await work.commit()
        return ReviewDecision(run.id, run.state, run.version, review_id)

    async def _evidence(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        review_id: UUID,
        validation_id: UUID,
        prior_id: UUID | None,
    ) -> tuple[
        ReviewEvidenceManifest,
        ValidationEvidenceManifest,
        tuple[EvidenceSetDescriptor, EvidenceSetDescriptor, str, str],
    ]:
        review = await work.evidence.get_by_id(review_id, run_id=approved.run.id)
        validation = await work.evidence.get_by_id(validation_id, run_id=approved.run.id)
        rw, vw = (
            await self._store.open_bytes(review.manifest_digest),
            await self._store.open_bytes(validation.manifest_digest),
        )
        rm, vm = decode_evidence_manifest(rw), decode_evidence_manifest(vw)
        run = approved.run
        if not run.worktree_path or not run.branch_name or not run.base_sha:
            raise ReviewDecisionRecoveryRequired("review decision worktree is absent")
        tree = ManagedWorktree(
            identity=WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, approved.policy.database.enabled
            ),
            path=Path(run.worktree_path),
            base_sha=run.base_sha,
        )
        git = self._git_factory(approved.policy)
        current = git.candidate_diff(tree)
        if (
            git.inspect_worktree(tree.identity, tree.base_sha) != tree
            or not git.is_ancestor(tree)
            or current.diff.truncated
            or not isinstance(rm, ReviewEvidenceManifest)
            or not isinstance(vm, ValidationEvidenceManifest)
            or review.kind is not EvidenceKind.REVIEW
            or validation.kind is not EvidenceKind.VALIDATION
            or review.producer_execution_id is None
            or rm.producer_execution_id != review.producer_execution_id
            or review.step_id != rm.step_id
            or review.policy_version != approved.policy.version
            or review.head_sha != current.head_sha
            or review.validation_evidence_set_id != validation_id
            or rm.validation_evidence_set_id != validation_id
            or rm.evidence_set_id != review_id
            or rm.run_id != run.id
            or rm.policy_version != approved.policy.version
            or rm.head_sha != current.head_sha
            or validation.policy_version != approved.policy.version
            or validation.producer_execution_id is not None
            or vm.evidence_set_id != validation_id
            or vm.run_id != run.id
            or vm.step_id != validation.step_id
            or vm.policy_version != approved.policy.version
            or validation.prior_review_evidence_set_id != prior_id
            or vm.prior_review_evidence_set_id != prior_id
            or validation.head_sha != current.head_sha
            or vm.head_sha != current.head_sha
            or review.manifest_digest != hashlib.sha256(rw).hexdigest()
            or validation.manifest_digest != hashlib.sha256(vw).hexdigest()
            or review.manifest_byte_count != len(rw)
            or validation.manifest_byte_count != len(vw)
            or len(vm.members) != len(approved.policy.required_checks)
            or {m.command_name: m.command_digest for m in vm.members}
            != {s.name: command_spec_digest(s) for s in approved.policy.required_checks}
            or any(m.status is not EvidenceStatus.PASSED for m in vm.members)
        ):
            raise ReviewDecisionRecoveryRequired("review decision evidence differs")
        return (
            rm,
            vm,
            (
                review,
                validation,
                current.head_sha,
                hashlib.sha256(current.diff.text.encode()).hexdigest(),
            ),
        )

    async def _freeze(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        review_id: UUID,
        validation_id: UUID,
        values: tuple[EvidenceSetDescriptor, EvidenceSetDescriptor, str, str],
        *,
        replay: bool = False,
    ) -> str:
        review, validation, head, diff_digest = values
        vm = decode_evidence_manifest(await self._store.open_bytes(validation.manifest_digest))
        rm = decode_evidence_manifest(await self._store.open_bytes(review.manifest_digest))
        if not isinstance(vm, ValidationEvidenceManifest) or not isinstance(
            rm, ReviewEvidenceManifest
        ):
            raise ReviewDecisionRecoveryRequired("PR evidence kinds differ")
        runner_results = []
        result_parents = {validation.manifest_digest}
        specs = {spec.name: spec for spec in approved.policy.required_checks}
        for member in vm.members:
            wire = await self._store.open_bytes(member.command_result_digest)
            result = decode_command_result(wire)
            artifact = await work.artifacts.get_by_digest(
                member.command_result_digest, run_id=approved.run.id
            )
            spec = specs[member.command_name]
            if (
                hashlib.sha256(wire).hexdigest() != member.command_result_digest
                or artifact.producer_type != "command_result"
                or artifact.producer_id != member.result_id
                or artifact.byte_count != len(wire)
                or artifact.truncated
                or artifact.media_type != "application/vnd.forge.command-result+json"
                or artifact.parent_digests
                != tuple(sorted({member.stdout_digest, member.stderr_digest}))
                or result.command_name != member.command_name
                or result.command_digest != member.command_digest
                or result.policy_version != approved.policy.version
                or result.runner_mode is not approved.policy.runner_mode
                or result.network_enabled
                != effective_network_enabled(approved.policy.runner_mode, spec.network_enabled)
                or result.unsandboxed
                is not (approved.policy.runner_mode is RunnerMode.TRUSTED_HOST)
                or result.stdout_digest != member.stdout_digest
                or result.stderr_digest != member.stderr_digest
                or result.exit_code != 0
                or result.timed_out
            ):
                raise ReviewDecisionRecoveryRequired("PR runner evidence differs")
            runner_results.append(
                {
                    "result_id": str(member.result_id),
                    "digest": member.command_result_digest,
                    "result": json.loads(wire),
                }
            )
            result_parents.add(member.command_result_digest)
        runner_wire = self._json(
            {
                "schema_version": 1,
                "run_id": str(approved.run.id),
                "head_sha": head,
                "policy_version": approved.policy.version,
                "validation_evidence_set_id": str(validation_id),
                "results": runner_results,
            }
        )
        runner_digest = await self._artifact(
            work,
            approved,
            review_id,
            "pr_runner_evidence",
            runner_wire,
            "application/json",
            result_parents,
            replay=replay,
        )
        body = (
            f"## Task\n\n{approved.task.title}\n\n{approved.task.body}\n\n"
            f"## Approved plan\n\n{approved.plan.summary}\n\n"
            "## Validation\n\n"
            + "\n".join(f"- {m.command_name}: {m.status.value}" for m in vm.members)
            + f"\n\n## Independent review\n\n{rm.review.summary}\n\n"
            + "\n".join(
                f"- {f.finding_id} ({f.severity.value}): {f.summary}" for f in rm.review.findings
            )
            + f"\n\nCandidate: `{head}`\nValidation evidence: `{validation.manifest_digest}`"
            + f"\nReview evidence: `{review.manifest_digest}`\n"
        ).encode("utf-8")
        body_digest = await self._artifact(
            work,
            approved,
            review_id,
            "pr_approval_body",
            body,
            "text/markdown",
            {review.manifest_digest, validation.manifest_digest},
            replay=replay,
        )
        evidence = PrApprovalEvidence(
            candidate_commit=head,
            diff_digest=diff_digest,
            validation_digest=validation.manifest_digest,
            review_digest=review.manifest_digest,
            repository=approved.policy.github_repository,
            base_ref=approved.run.base_ref or "",
            base_sha=approved.run.base_sha or "",
            title=approved.task.title,
            body_digest=body_digest,
            runner_mode=approved.policy.runner_mode,
            runner_evidence_digest=runner_digest,
            remote_remediation_limit=approved.policy.remote_remediation_limit,
        )
        digest = canonical_digest(evidence)
        wire = self._json(evidence.model_dump(mode="json"))
        if hashlib.sha256(wire).hexdigest() != digest:
            raise ReviewDecisionRecoveryRequired("PR evidence serialization differs")
        await self._artifact(
            work,
            approved,
            review_id,
            "pr_approval_evidence",
            wire,
            "application/json",
            {body_digest, runner_digest, review.manifest_digest, validation.manifest_digest},
            replay=replay,
        )
        return digest

    @staticmethod
    def _json(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    async def _artifact(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        producer_id: UUID,
        kind: str,
        wire: bytes,
        media_type: str,
        parents: set[str],
        *,
        replay: bool,
    ) -> str:
        digest = hashlib.sha256(wire).hexdigest()
        if replay:
            artifacts = await work.artifacts.get_by_producer(
                run_id=approved.run.id,
                producer_type=kind,
                producer_id=producer_id,
            )
            if (
                len(artifacts) != 1
                or artifacts[0].digest != digest
                or artifacts[0].parent_digests != tuple(sorted(parents))
                or artifacts[0].byte_count != len(wire)
                or artifacts[0].media_type != media_type
                or artifacts[0].truncated
                or await self._store.open_bytes(digest) != wire
            ):
                raise ReviewDecisionRecoveryRequired("PR frozen artifact replay differs")
        else:
            artifact = await self._store.put_bytes(wire, media_type=media_type)
            if artifact.digest != digest or artifact.truncated or artifact.byte_count != len(wire):
                raise ReviewDecisionRecoveryRequired("PR frozen artifact differs")
            await work.artifacts.record(
                artifact,
                run_id=approved.run.id,
                producer_type=kind,
                producer_id=producer_id,
                parent_digests=tuple(sorted(parents)),
            )
        return digest

    async def _refence(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        candidate: tuple[EvidenceSetDescriptor, EvidenceSetDescriptor, str, str],
    ) -> None:
        await self._fence(command, work)
        current = await self._approved.load(work, command.run_id)
        if current.run != approved.run or current.approval_id != approved.approval_id:
            raise ReviewDecisionRecoveryRequired("review decision authority changed")
        if await pending_current_control_stop(work, current.run):
            raise ReviewDecisionRecoveryRequired("review decision fenced by operator control")
        _, validation_id, prior_id = self._command(command, await resume_origin(work, command))
        _, _, refreshed = await self._evidence(
            work, current, candidate[0].evidence_set_id, validation_id, prior_id
        )
        if refreshed != candidate:
            raise ReviewDecisionRecoveryRequired("review decision candidate changed")
        await self._fence(command, work)

    async def _replay(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        review: ReviewEvidenceManifest,
        validation: ValidationEvidenceManifest,
        candidate: tuple[EvidenceSetDescriptor, EvidenceSetDescriptor, str, str],
        events: list[RunEvent],
    ) -> ReviewDecision:
        if (
            len(events) != 1
            or events[0].run_version != approved.run.version
            or approved.run.version != command.expected_run_version + 1
            or events[0].payload.get("review_digest") != candidate[0].manifest_digest
            or events[0].payload.get("validation_digest") != candidate[1].manifest_digest
            or events[0].actor_class != "worker"
            or events[0].actor_id is not None
            or events[0].payload.get("approval_id") != str(approved.approval_id)
            or events[0].payload.get("review_evidence_set_id") != str(review.evidence_set_id)
            or events[0].payload.get("validation_evidence_set_id")
            != str(validation.evidence_set_id)
            or events[0].payload.get("target") != approved.run.state.value
            or events[0].payload.get("local_remediation_count")
            != approved.run.local_remediation_count
        ):
            raise ReviewDecisionRecoveryRequired("review decision replay differs")
        blocking = bool(review.review.missing_evidence) or approved.policy.blocks_publication(
            review.review.findings
        )
        run = approved.run
        event = events[0]
        if not blocking:
            digest = await self._freeze(
                work,
                approved,
                review.evidence_set_id,
                validation.evidence_set_id,
                candidate,
                replay=True,
            )
            if (
                run.state is not RunState.AWAITING_PR_APPROVAL
                or run.pending_gate is not ApprovalGate.PR
                or run.pending_evidence_digest != digest
                or event.payload.get("pr_evidence_digest") != digest
                or event.payload.get("queued_key") is not None
            ):
                raise ReviewDecisionRecoveryRequired("review PR gate replay differs")
        elif run.state is RunState.REMEDIATING:
            attempt = event.payload.get("semantic_attempt")
            key = f"{run.id}:remediate:{attempt}"
            queued = await work.commands.get_by_idempotency_key(key)
            if (
                type(attempt) is not int
                or attempt < 1
                or queued is None
                or event.payload.get("queued_key") != key
                or event.payload.get("queued_command_id") != str(queued.id)
                or queued.command_type != "remediate"
                or queued.status is not CommandStatus.PENDING
                or queued.payload_schema_version != 1
                or queued.expected_run_version != run.version
                or queued.actor_id != approved.approval_actor_id
                or queued.payload
                != {
                    "semantic_attempt": attempt,
                    "automatic": True,
                    "validation_evidence_set_id": str(validation.evidence_set_id),
                    "prior_review_evidence_set_id": str(review.evidence_set_id),
                }
            ):
                raise ReviewDecisionRecoveryRequired("review remediation replay queue differs")
        elif (
            run.state is not RunState.AWAITING_HUMAN_INTERVENTION
            or run.local_remediation_count < approved.evidence.local_remediation_limit
            or event.payload.get("queued_key") is not None
        ):
            raise ReviewDecisionRecoveryRequired("review intervention replay differs")
        await self._refence(command, work, approved, candidate)
        await work.commit()
        return ReviewDecision(
            approved.run.id, approved.run.state, approved.run.version, review.evidence_set_id
        )

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
            raise ReviewDecisionRecoveryRequired("review decision lease differs")

    @staticmethod
    def _command(
        command: CommandEnvelope, origin: CommandEnvelope | None = None
    ) -> tuple[int, UUID, UUID | None]:
        try:
            attempt, validation, prior = ReviewService._validate(command, origin)
        except CommandRecoveryRequired:
            raise ReviewDecisionRecoveryRequired("review decision command is invalid") from None
        if not validation.int or (prior is not None and not prior.int):
            raise ReviewDecisionRecoveryRequired("review decision command is invalid")
        return attempt, validation, prior


__all__ = ["ReviewDecision", "ReviewDecisionRecoveryRequired", "ReviewDecisionService"]
