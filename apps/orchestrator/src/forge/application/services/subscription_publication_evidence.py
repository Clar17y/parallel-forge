"""Build and re-prove frozen publication inputs from actual acceptance sources."""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from forge.application.adapters.named_check_receipts import decode_command_result
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceKind,
    EvidenceSetDescriptor,
    SubscriptionAcceptanceEvidenceDraft,
)
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.runner import CommandTerminalResult
from forge.application.ports.subscription_acceptance import RetainedSubscriptionAcceptance
from forge.application.ports.subscription_validation import AcceptanceValidationBinding
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlan
from forge.application.services.paused_approvals import approval_gate_origin
from forge.application.services.resume_source import resume_origin
from forge.application.services.validation import (
    ValidationService,
    validation_acceptance_attempt,
    validation_command_binding,
)
from forge.domain.approval import (
    ApprovalGate,
    SubscriptionPlanApprovalEvidence,
    SubscriptionPrApprovalEvidence,
    canonical_digest,
    decode_pr_approval_evidence,
)
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.evidence import (
    EvidenceStatus,
    SubscriptionAcceptanceEvidenceManifest,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
    encode_evidence_manifest,
)
from forge.domain.operation import canonical_digest as record_digest
from forge.domain.run import RunState
from forge.domain.validation import command_spec_digest

MEDIA_TYPE = "application/vnd.forge.evidence-manifest+json"
PUBLICATION_EVENT = "run.subscription_acceptance_decided"


def is_reviewed_publication_event(event: RunEvent, actor_id: UUID | None) -> bool:
    """Select the explicit legacy review or subscription acceptance event variant."""
    return (
        event.actor_class == "worker"
        and event.payload_schema_version == 1
        and event.payload.get("target") == RunState.MONITORING_PR.value
        and (
            (event.event_type == "run.review_decided" and event.actor_id is None)
            or (
                event.event_type == PUBLICATION_EVENT
                and actor_id is not None
                and event.actor_id == actor_id
            )
        )
    )


@dataclass(frozen=True, slots=True)
class FrozenSubscriptionPublication:
    evidence: SubscriptionPrApprovalEvidence
    body: bytes
    acceptance: EvidenceSetDescriptor
    validation: EvidenceSetDescriptor
    source: RetainedSubscriptionAcceptance

    @property
    def digest(self) -> str:
        return canonical_digest(self.evidence)


@dataclass(frozen=True, slots=True)
class VerifiedSubscriptionValidation:
    source: RetainedSubscriptionAcceptance
    binding: AcceptanceValidationBinding
    validation: EvidenceSetDescriptor
    manifest: ValidationEvidenceManifest
    parents: frozenset[str]
    runner_results: tuple[dict[str, object], ...]

    @property
    def passed(self) -> bool:
        return all(member.status is EvidenceStatus.PASSED for member in self.manifest.members)


def publication_decision_payload(
    command: CommandEnvelope, approved: ApprovedPlan, frozen: FrozenSubscriptionPublication,
    *, push: CommandEnvelope | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_command_id": str(command.id),
        "source_payload_digest": record_digest(command.payload),
        "source_expected_version": command.expected_run_version,
        "approval_id": str(approved.approval_id),
        "acceptance_attempt_id": str(validation_acceptance_attempt(command)),
        "acceptance_evidence_set_id": str(frozen.acceptance.evidence_set_id),
        "acceptance_digest": frozen.acceptance.manifest_digest,
        "validation_evidence_set_id": str(frozen.validation.evidence_set_id),
        "validation_digest": frozen.validation.manifest_digest,
        "pr_evidence_digest": frozen.digest,
        "target": RunState.AWAITING_PR_APPROVAL.value,
    }
    if push is not None:
        payload.update(
            target=RunState.MONITORING_PR.value,
            queued_command_id=str(push.id),
            queued_key=push.idempotency_key,
            queued_payload=dict(push.payload),
        )
    return payload


def acceptance_manifest(
    source: RetainedSubscriptionAcceptance,
    validation: ValidationEvidenceManifest,
    receipt_digest: str,
) -> SubscriptionAcceptanceEvidenceManifest:
    review, candidate = source.review, source.review.candidate
    return SubscriptionAcceptanceEvidenceManifest(
        evidence_set_id=uuid5(validation.evidence_set_id, f"acceptance:{source.attempt_id}"),
        run_id=source.decision.run_id,
        step_id=validation.step_id,
        policy_version=source.policy.version,
        head_sha=candidate.head_sha,
        base_sha=candidate.base_sha,
        candidate_tree_digest=candidate.tree_digest,
        candidate_manifest_digest=candidate.manifest_digest,
        candidate_epoch=review.candidate_epoch,
        producer_task_id=source.decision.task_id,
        producer_attempt_id=source.attempt_id,
        producer_result_digest=source.result_digest,
        acceptance=source.decision,
        selection_attempt_id=review.selection_attempt_id,
        selection_result_digest=review.selection_result_digest,
        selection_application_digest=review.selection_application_digest,
        selection=review.selection,
        review_handoff=review.review_handoff,
        review_result_digest=review.review_result_digest,
        review_application_digest=review.review_application_digest,
        receipt_evidence_digest=receipt_digest,
        validation_evidence_set_id=validation.evidence_set_id,
    )


class SubscriptionPublicationEvidence:
    def __init__(self, store: ArtifactStore) -> None:
        self._store = store
        self._receipts = ValidationService(store)

    async def at_pr_gate(
        self, work: UnitOfWork, approved: ApprovedPlan
    ) -> FrozenSubscriptionPublication:
        """Rebuild the exact frozen gate without requiring current Git or a remote."""
        run = approved.run
        if (
            run.state is not RunState.AWAITING_PR_APPROVAL
            or run.pending_gate is not ApprovalGate.PR
            or not run.pending_evidence_digest
            or run.base_ref is None
            or run.base_sha is None
        ):
            raise CommandRecoveryRequired("subscription PR gate is not current")
        _, wire = await self._artifact(work, run.id, run.pending_evidence_digest)
        evidence = decode_pr_approval_evidence(wire)
        if (
            not isinstance(evidence, SubscriptionPrApprovalEvidence)
            or evidence.base_ref != run.base_ref
            or evidence.base_sha != run.base_sha
        ):
            raise CommandRecoveryRequired("subscription PR gate evidence differs")
        gate_version = await approval_gate_origin(
            work, run.id, run.version, "pr", run.pending_evidence_digest
        )
        events = [
            event
            for event in await work.events.list_for_version(run.id, gate_version)
            if event.event_type == PUBLICATION_EVENT
        ]
        if len(events) != 1:
            raise CommandRecoveryRequired("subscription PR gate source differs")
        event = events[0]
        command = await work.commands.get(UUID(str(event.payload.get("source_command_id"))))
        frozen = await self.freeze(
            work, approved, command, diff_digest=evidence.diff_digest, read_only=True
        )
        if (
            event.actor_class != "worker"
            or event.actor_id != approved.approval_actor_id
            or event.payload_schema_version != 1
            or event.run_version != command.expected_run_version + 1
            or event.run_version > run.version
            or evidence != frozen.evidence
            or frozen.digest != run.pending_evidence_digest
            or record_digest(event.payload)
            != record_digest(publication_decision_payload(command, approved, frozen))
        ):
            raise CommandRecoveryRequired("subscription PR gate decision differs")
        return frozen

    async def verify_validation(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        command: CommandEnvelope,
    ) -> VerifiedSubscriptionValidation:
        """Re-prove actual terminal outcomes before choosing publication or repair."""
        origin = await resume_origin(
            work, command, historical=command.status is not CommandStatus.LEASED
        )
        validation_command_binding(command, origin)
        attempt_id = validation_acceptance_attempt(command)
        if attempt_id is None or not isinstance(
            approved.evidence, SubscriptionPlanApprovalEvidence
        ):
            raise CommandRecoveryRequired("subscription publication source is absent")
        binding = await work.subscription_decisions.acceptance_validation_binding(attempt_id)
        original = origin or command
        if (
            binding is None
            or binding.approval_id != approved.approval_id
            or command.run_id != approved.run.id
            or command.actor_id != approved.approval_actor_id
            or any(
                getattr(binding.command, field) != getattr(original, field)
                for field in (
                    "id",
                    "run_id",
                    "command_type",
                    "payload",
                    "payload_schema_version",
                    "idempotency_key",
                    "actor_id",
                    "expected_run_version",
                )
            )
        ):
            raise CommandRecoveryRequired("subscription publication dispatch differs")
        source = await work.subscription_decisions.retained_acceptance_source(attempt_id)
        if source.policy != approved.policy or source.review.candidate != binding.candidate:
            raise CommandRecoveryRequired("subscription publication plan differs")
        proof_wire = self._json(source.receipts.payload())
        if hashlib.sha256(proof_wire).hexdigest() != binding.receipt_evidence_digest:
            raise CommandRecoveryRequired("subscription publication receipt differs")
        _, retained_proof = await self._artifact(
            work, approved.run.id, binding.receipt_evidence_digest
        )
        if retained_proof != proof_wire:
            raise CommandRecoveryRequired("subscription publication receipt bytes differ")
        for digest, _ in source.receipts.artifact_proofs:
            await self._artifact(work, approved.run.id, digest)
        step_id = uuid5(NAMESPACE_URL, f"forge:validate:{command.id}")
        validation_id = uuid5(step_id, "validation-evidence")
        validation = await work.evidence.get_by_id(validation_id, run_id=approved.run.id)
        va, validation_wire = await self._artifact(
            work, approved.run.id, validation.manifest_digest
        )
        vm = decode_evidence_manifest(validation_wire)
        step = await work.controller_steps.get(approved.run.id, step_id)
        if (
            not isinstance(vm, ValidationEvidenceManifest)
            or vm.schema_version != 2
            or validation.kind is not EvidenceKind.VALIDATION
            or validation.evidence_set_id != validation_id
            or vm.evidence_set_id != validation_id
            or validation.step_id != step_id
            or vm.step_id != step_id
            or vm.run_id != approved.run.id
            or validation.run_id != approved.run.id
            or validation.policy_version != approved.policy.version
            or vm.policy_version != approved.policy.version
            or validation.head_sha != binding.candidate.head_sha
            or vm.head_sha != binding.candidate.head_sha
            or validation.candidate_tree_digest != binding.candidate.tree_digest
            or vm.candidate_tree_digest != binding.candidate.tree_digest
            or validation.manifest_byte_count != len(validation_wire)
            or validation.manifest_schema_version != 2
            or validation.manifest_media_type != MEDIA_TYPE
            or validation.manifest_artifact_id != va.artifact_id
            or va.producer_type != "evidence_set"
            or va.producer_id != validation_id
            or va.media_type != MEDIA_TYPE
            or va.schema_version != 2
            or validation.prior_review_evidence_set_id is not None
            or vm.prior_review_evidence_set_id is not None
            or step is None
            or step.kind != "validate"
            or step.status is not ExecutionStatus.SUCCEEDED
            or step.output_artifact_id != validation.manifest_artifact_id
            or len(vm.members) != len(approved.policy.required_checks)
            or {m.command_name: m.command_digest for m in vm.members}
            != {s.name: command_spec_digest(s) for s in approved.policy.required_checks}
        ):
            raise CommandRecoveryRequired("subscription publication validation differs")
        started = [
            event
            for event in await work.events.list_after(approved.run.id, 0)
            if event.event_type == "run.validation_started"
            and event.payload.get("step_id") == str(step_id)
        ]
        expected_started = {
            "command_id": str(command.id),
            "step_id": str(step_id),
            "head_sha": vm.head_sha,
            "policy_version": vm.policy_version,
            "evidence_set_id": str(validation_id),
            "candidate": binding.candidate.payload(),
            "checks": tuple(command_spec_digest(s) for s in approved.policy.required_checks),
        }
        if (
            len(started) != 1
            or started[0].actor_class != "worker"
            or started[0].actor_id is not None
            or started[0].payload_schema_version != 1
            or started[0].run_version != command.expected_run_version
            or record_digest(started[0].payload) != record_digest(expected_started)
        ):
            raise CommandRecoveryRequired("subscription validation admission differs")
        parents: set[str] = set()
        runner_results: list[dict[str, object]] = []
        specs = {s.name: s for s in approved.policy.required_checks}
        for member in vm.members:
            _, wire = await self._artifact(work, approved.run.id, member.command_result_digest)
            result = decode_command_result(wire)
            status = (
                EvidenceStatus.FAILED
                if result.exit_code != 0 or result.timed_out
                else EvidenceStatus.PASSED
            )
            if (
                result.command_name != member.command_name
                or member.check_name != result.command_name
                or result.command_digest != member.command_digest
                or result.policy_version != approved.policy.version
                or member.command_version != result.policy_version
                or result.stdout_digest != member.stdout_digest
                or result.stderr_digest != member.stderr_digest
                or member.status is not status
                or member.exit_code != result.exit_code
                or member.started_at != result.started_at
                or member.completed_at
                != result.started_at + timedelta(milliseconds=result.duration_ms)
                or (result.timed_out and result.exit_code == 0)
            ):
                raise CommandRecoveryRequired("subscription publication runner result differs")
            receipt = await self._receipts._candidate_receipt(
                work,
                approved.run,
                approved.policy,
                step_id,
                member.result_id,
                specs[member.command_name],
                binding.candidate,
                CommandTerminalResult(result=result, caller_cancelled=False),
            )
            if receipt != member.controller_receipt_digest:
                raise CommandRecoveryRequired("subscription validation receipt differs")
            for digest in (
                member.stdout_digest,
                member.stderr_digest,
                member.command_result_digest,
                receipt,
            ):
                await self._artifact(work, approved.run.id, digest)
                parents.add(digest)
            runner_results.append(
                {
                    "result_id": str(member.result_id),
                    "digest": member.command_result_digest,
                    "receipt_digest": receipt,
                    "result": json.loads(wire),
                }
            )
        if va.parent_digests != tuple(sorted(parents)):
            raise CommandRecoveryRequired("subscription validation parents differ")
        return VerifiedSubscriptionValidation(
            source, binding, validation, vm, frozenset(parents), tuple(runner_results)
        )

    async def freeze(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        command: CommandEnvelope,
        *,
        diff_digest: str,
        read_only: bool = False,
    ) -> FrozenSubscriptionPublication:
        """Use identical calculations for first storage and historical verification."""
        verified = await self.verify_validation(work, approved, command)
        if not verified.passed:
            raise CommandRecoveryRequired("subscription publication requires passed validation")
        source, binding = verified.source, verified.binding
        validation, vm = verified.validation, verified.manifest
        parents, runner_results = set(verified.parents), verified.runner_results
        step_id, validation_id = vm.step_id, vm.evidence_set_id
        attempt_id = source.attempt_id
        manifest = acceptance_manifest(source, vm, binding.receipt_evidence_digest)
        wire = encode_evidence_manifest(manifest)
        aa = await self._retain(
            work,
            approved.run.id,
            manifest.evidence_set_id,
            "evidence_set",
            wire,
            MEDIA_TYPE,
            {validation.manifest_digest, binding.receipt_evidence_digest},
            read_only,
        )
        if read_only:
            acceptance = await work.evidence.get_by_id(
                manifest.evidence_set_id, run_id=approved.run.id
            )
            if (
                acceptance.kind is not EvidenceKind.ACCEPTANCE
                or acceptance.manifest_digest != aa.digest
                or acceptance.manifest_artifact_id != aa.artifact_id
                or acceptance.step_id != step_id
                or acceptance.policy_version != approved.policy.version
                or acceptance.head_sha != vm.head_sha
                or acceptance.candidate_tree_digest != binding.candidate.tree_digest
                or acceptance.producer_task_id != source.decision.task_id
                or acceptance.producer_attempt_id != attempt_id
                or acceptance.producer_execution_id is not None
                or acceptance.validation_evidence_set_id != validation_id
                or acceptance.manifest_schema_version != 1
                or acceptance.manifest_media_type != MEDIA_TYPE
                or acceptance.manifest_byte_count != len(wire)
                or acceptance.prior_review_evidence_set_id is not None
            ):
                raise CommandRecoveryRequired("subscription acceptance evidence differs")
        else:
            acceptance = await work.evidence.record_set(
                SubscriptionAcceptanceEvidenceDraft(manifest),
                CanonicalEvidenceArtifact(aa, manifest, wire),
            )
        runner = await self._retain(
            work,
            approved.run.id,
            manifest.evidence_set_id,
            "pr_runner_evidence",
            self._json(
                {
                    "schema_version": 2,
                    "run_id": str(approved.run.id),
                    "head_sha": vm.head_sha,
                    "candidate_tree_digest": binding.candidate.tree_digest,
                    "policy_version": approved.policy.version,
                    "validation_evidence_set_id": str(validation_id),
                    "results": runner_results,
                }
            ),
            "application/json",
            parents | {validation.manifest_digest},
            read_only,
        )
        review_text = (
            source.review.selection.no_review_reason
            if source.review.review_handoff is None
            else source.review.review_handoff.review_output.summary
            + "\n"
            + "\n".join(
                f"- {f.finding_id} ({f.severity.value}): {f.summary}"
                for f in source.review.review_handoff.review_output.findings
            )
        )
        if source.review.review_handoff is not None and approved.policy.blocks_publication(
            source.review.review_handoff.review_output.findings
        ):
            raise CommandRecoveryRequired("subscription review blocks publication")
        body = (
            f"## Task\n\n{approved.task.title}\n\n{approved.task.body}\n\n"
            f"## Approved plan\n\n{approved.plan.summary}\n\n## Validation\n\n"
            + "\n".join(f"- {m.command_name}: {m.status.value}" for m in vm.members)
            + f"\n\n## Primary acceptance\n\n{source.decision.rationale}\n\n## Review selection\n\n{review_text}"
            + f"\n\nCandidate: `{vm.head_sha}`\nValidation evidence: `{validation.manifest_digest}`"
            + f"\nAcceptance evidence: `{aa.digest}`\n"
        ).encode()
        body_artifact = await self._retain(
            work,
            approved.run.id,
            manifest.evidence_set_id,
            "pr_approval_body",
            body,
            "text/markdown",
            {aa.digest, validation.manifest_digest},
            read_only,
        )
        evidence = SubscriptionPrApprovalEvidence(
            candidate_commit=vm.head_sha,
            candidate_tree_digest=binding.candidate.tree_digest,
            diff_digest=diff_digest,
            validation_digest=validation.manifest_digest,
            acceptance_digest=aa.digest,
            repository=approved.policy.github_repository,
            base_ref=approved.run.base_ref or "",
            base_sha=binding.candidate.base_sha,
            title=approved.task.title,
            body_digest=body_artifact.digest,
            runner_mode=approved.policy.runner_mode,
            runner_evidence_digest=runner.digest,
            remote_remediation_limit=approved.policy.remote_remediation_limit,
        )
        await self._retain(
            work,
            approved.run.id,
            manifest.evidence_set_id,
            "pr_approval_evidence",
            self._json(evidence.model_dump(mode="json")),
            "application/json",
            {aa.digest, validation.manifest_digest, body_artifact.digest, runner.digest},
            read_only,
        )
        return FrozenSubscriptionPublication(evidence, body, acceptance, validation, source)

    @staticmethod
    def _json(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    async def _artifact(
        self, work: UnitOfWork, run_id: UUID, digest: str
    ) -> tuple[ArtifactDescriptor, bytes]:
        descriptor = await work.artifacts.get_by_digest(digest, run_id=run_id)
        wire = await self._store.open_bytes(digest)
        if (
            descriptor.byte_count != len(wire)
            or descriptor.truncated
            or hashlib.sha256(wire).hexdigest() != digest
        ):
            raise CommandRecoveryRequired("subscription publication artifact bytes differ")
        return descriptor, wire

    async def _retain(
        self,
        work: UnitOfWork,
        run_id: UUID,
        producer_id: UUID,
        kind: str,
        wire: bytes,
        media: str,
        parents: set[str],
        read_only: bool,
    ) -> ArtifactDescriptor:
        digest = hashlib.sha256(wire).hexdigest()
        if read_only:
            descriptor, actual = await self._artifact(work, run_id, digest)
            if (
                actual != wire
                or descriptor.producer_type != kind
                or descriptor.producer_id != producer_id
                or descriptor.media_type != media
                or descriptor.schema_version != 1
                or descriptor.parent_digests != tuple(sorted(parents))
            ):
                raise CommandRecoveryRequired("subscription frozen artifact differs")
            return descriptor
        stored = await self._store.put_bytes(wire, media_type=media)
        if (
            stored.digest != digest
            or stored.byte_count != len(wire)
            or stored.truncated
            or await self._store.open_bytes(digest) != wire
        ):
            raise CommandRecoveryRequired("subscription publication storage differs")
        return await work.artifacts.record(
            replace(stored, schema_version=1),
            run_id=run_id,
            producer_type=kind,
            producer_id=producer_id,
            parent_digests=tuple(sorted(parents)),
        )
