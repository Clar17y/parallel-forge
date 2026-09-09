"""Verify terminal publication before resuming an undecided local stage."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid5

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.evidence import EvidenceKind, EvidenceSetDescriptor
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.resume_source import resume_origin
from forge.application.services.review import _EXECUTION_NAMESPACE, _STEP_NAMESPACE, ReviewService
from forge.application.services.validation import validation_command_binding
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.evidence import (
    EvidenceStatus,
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
    encode_evidence_manifest,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot, RunState
from forge.domain.validation import command_spec_digest


async def published_stage(
    work: UnitOfWork, store: ArtifactStore, run: RunSnapshot, source: CommandEnvelope
) -> tuple[UUID, UUID | None, UUID] | None:
    """Verify historical publication without changing terminal steps or usage.

    Caller owns the run lock, causal pause and expired-source lease CAS. The
    fresh stage must still perform its normal current-worktree admission checks.
    """
    try:
        return await _published_stage(work, store, run, source)
    except CommandRecoveryRequired:
        raise
    except Exception:  # noqa: BLE001 - unreadable authority cannot authorize recovery
        raise CommandRecoveryRequired(
            "published stage authority is unavailable or invalid"
        ) from None


async def _published_stage(
    work: UnitOfWork, store: ArtifactStore, run: RunSnapshot, source: CommandEnvelope
) -> tuple[UUID, UUID | None, UUID] | None:
    if source.command_type not in {"validate", "review"}:
        return None
    kind = source.command_type
    state = RunState.VALIDATING if kind == "validate" else RunState.REVIEWING
    if (
        source.status is not CommandStatus.CANCELLED
        or source.run_id != run.id
        or source.expected_run_version != run.version - 1
        or source.payload_schema_version != 1
        or source.attempt < 1
        or run.state is not RunState.PAUSED
        or run.suspended_state is not state
    ):
        raise CommandRecoveryRequired("published stage source differs")
    attempt = source.payload.get("semantic_attempt")
    if type(attempt) is not int or attempt < 1:
        raise CommandRecoveryRequired("published stage attempt differs")
    step_id = (
        uuid5(NAMESPACE_URL, f"forge:validate:{source.id}")
        if kind == "validate"
        else uuid5(_STEP_NAMESPACE, str(source.id))
    )
    execution_id = None
    outcome = None
    if kind == "validate":
        step = await work.controller_steps.get(run.id, step_id)
        if step is None or step.status is not ExecutionStatus.SUCCEEDED:
            return None
        if step.kind != kind or step.attempt != attempt or step.output_artifact_id is None:
            raise CommandRecoveryRequired("published controller lineage differs")
        artifact_id = step.output_artifact_id
    else:
        outcome = await work.executions.get_outcome(run.id, kind, attempt)
        if outcome is None or outcome.status is not ExecutionStatus.SUCCEEDED:
            return None
        execution_id = uuid5(_EXECUTION_NAMESPACE, str(source.id))
        if (
            outcome.step_id != step_id
            or outcome.agent_execution_id != execution_id
            or outcome.run_id != run.id
            or outcome.kind != kind
            or outcome.attempt != attempt
            or outcome.role is not AgentRole.REVIEWER
            or outcome.finish_status is not AgentFinishStatus.SUCCEEDED
            or outcome.output_artifact_id is None
        ):
            raise CommandRecoveryRequired("published reviewer lineage differs")
        artifact_id = outcome.output_artifact_id
    decision_type = "run.validation_decided" if kind == "validate" else "run.review_decided"
    if any(
        event.event_type == decision_type
        and event.payload.get("source_command_id") == str(source.id)
        for event in await work.events.list_after(run.id, 0)
    ):
        raise CommandRecoveryRequired("published stage already has a decision")
    approved = await ApprovedPlanLoader(store).load(work, run.id)
    if approved.run != run or source.actor_id != approved.approval_actor_id:
        raise CommandRecoveryRequired("published stage approval differs")
    # Historical projection for pure parsing only; never used to claim or dispatch.
    binding = replace(
        source,
        status=CommandStatus.LEASED,
        completed_at=None,
        lease_owner="historical-binding-only",
        lease_expires_at=source.available_at,
    )
    origin = await resume_origin(work, binding)
    if kind == "validate":
        bound_attempt, prior_id = validation_command_binding(binding, origin)
        validation_id = None
    else:
        bound_attempt, validation_id, prior_id = ReviewService._validate(binding, origin)
        assert outcome is not None
        if (
            outcome.provider != approved.policy.reviewer_model.provider
            or outcome.model != approved.policy.reviewer_model.model
        ):
            raise CommandRecoveryRequired("published reviewer model differs")
    if bound_attempt != attempt:
        raise CommandRecoveryRequired("published stage command attempt differs")
    evidence_id = uuid5(step_id, "validation-evidence" if kind == "validate" else "review-evidence")
    evidence, manifest = await _manifest(work, store, run.id, evidence_id, approved.policy)
    if (
        evidence.step_id != step_id
        or evidence.manifest_artifact_id != artifact_id
        or evidence.producer_execution_id != execution_id
    ):
        raise CommandRecoveryRequired("published stage output lineage differs")
    if kind == "validate":
        if (
            not isinstance(manifest, ValidationEvidenceManifest)
            or manifest.prior_review_evidence_set_id != prior_id
        ):
            raise CommandRecoveryRequired("published validation inputs differ")
        _validation_members(manifest, approved.policy)
    else:
        if (
            not isinstance(manifest, ReviewEvidenceManifest)
            or manifest.validation_evidence_set_id != validation_id
        ):
            raise CommandRecoveryRequired("published review inputs differ")
        assert execution_id is not None and validation_id is not None
        # The immutable published manifest and its artifact parent bind the
        # completed reviewer to its input; runtime tool reads require RUNNING.
        descriptor, vm = await _manifest(work, store, run.id, validation_id, approved.policy)
        if (
            not isinstance(vm, ValidationEvidenceManifest)
            or vm.head_sha != manifest.head_sha
            or vm.prior_review_evidence_set_id != prior_id
            or any(member.status is not EvidenceStatus.PASSED for member in vm.members)
        ):
            raise CommandRecoveryRequired("published reviewer validation differs")
        validation_step = await work.controller_steps.get(run.id, descriptor.step_id)
        if (
            validation_step is None
            or validation_step.kind != "validate"
            or validation_step.status is not ExecutionStatus.SUCCEEDED
            or validation_step.output_artifact_id != descriptor.manifest_artifact_id
        ):
            raise CommandRecoveryRequired("published reviewer validation step differs")
        _validation_members(vm, approved.policy)
    return step_id, execution_id, artifact_id


def _validation_members(manifest: ValidationEvidenceManifest, policy: ProjectPolicy) -> None:
    if len(manifest.members) != len(policy.required_checks) or {
        member.command_name: member.command_digest for member in manifest.members
    } != {spec.name: command_spec_digest(spec) for spec in policy.required_checks}:
        raise CommandRecoveryRequired("published validation required checks differ")


async def _manifest(
    work: UnitOfWork, store: ArtifactStore, run_id: UUID, evidence_id: UUID, policy: ProjectPolicy
) -> tuple[EvidenceSetDescriptor, ValidationEvidenceManifest | ReviewEvidenceManifest]:
    evidence = await work.evidence.get_by_id(evidence_id, run_id=run_id)
    wire = await store.open_bytes(evidence.manifest_digest)
    manifest = decode_evidence_manifest(wire)
    if (
        evidence.evidence_set_id != evidence_id
        or evidence.run_id != run_id
        or evidence.manifest_digest != hashlib.sha256(wire).hexdigest()
        or evidence.manifest_byte_count != len(wire)
        or evidence.manifest_media_type != "application/vnd.forge.evidence-manifest+json"
        or evidence.manifest_schema_version != 1
        or encode_evidence_manifest(manifest) != wire
        or manifest.evidence_set_id != evidence_id
        or manifest.run_id != run_id
        or manifest.step_id != evidence.step_id
        or manifest.head_sha != evidence.head_sha
        or manifest.policy_version != evidence.policy_version
        or evidence.policy_version != policy.version
    ):
        raise CommandRecoveryRequired("published stage manifest differs")
    parents: set[str] = set()
    if isinstance(manifest, ValidationEvidenceManifest):
        if (
            evidence.kind is not EvidenceKind.VALIDATION
            or evidence.producer_execution_id is not None
            or evidence.validation_evidence_set_id is not None
            or evidence.prior_review_evidence_set_id != manifest.prior_review_evidence_set_id
        ):
            raise CommandRecoveryRequired("published validation descriptor differs")
        for member in manifest.members:
            parents.update(
                (member.command_result_digest, member.stdout_digest, member.stderr_digest)
            )
        if manifest.prior_review_evidence_set_id is not None:
            prior = await work.evidence.get_by_id(
                manifest.prior_review_evidence_set_id, run_id=run_id
            )
            if prior.kind is not EvidenceKind.REVIEW or prior.policy_version != policy.version:
                raise CommandRecoveryRequired("published validation prior review differs")
            parents.add(prior.manifest_digest)
    elif isinstance(manifest, ReviewEvidenceManifest):
        if (
            evidence.kind is not EvidenceKind.REVIEW
            or evidence.producer_execution_id != manifest.producer_execution_id
            or evidence.validation_evidence_set_id != manifest.validation_evidence_set_id
            or evidence.prior_review_evidence_set_id is not None
        ):
            raise CommandRecoveryRequired("published review descriptor differs")
        validation = await work.evidence.get_by_id(
            manifest.validation_evidence_set_id, run_id=run_id
        )
        parents.add(validation.manifest_digest)
    else:
        raise CommandRecoveryRequired("published stage manifest kind differs")
    artifacts = await work.artifacts.get_by_producer(
        run_id=run_id, producer_type="evidence_set", producer_id=evidence_id
    )
    if len(artifacts) != 1:
        raise CommandRecoveryRequired("published stage artifact lineage is ambiguous")
    artifact = artifacts[0]
    if (
        artifact.artifact_id != evidence.manifest_artifact_id
        or artifact.digest != evidence.manifest_digest
        or artifact.byte_count != len(wire)
        or artifact.media_type != evidence.manifest_media_type
        or artifact.schema_version != 1
        or artifact.truncated
        or artifact.parent_digests != tuple(sorted(parents))
    ):
        raise CommandRecoveryRequired("published stage artifact lineage differs")
    return evidence, manifest


__all__ = ["published_stage"]
