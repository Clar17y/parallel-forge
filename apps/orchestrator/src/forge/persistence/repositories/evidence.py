"""PostgreSQL persistence for immutable canonical evidence sets."""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.artifacts import ArtifactRepository
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceConflict,
    EvidenceCorruptLineage,
    EvidenceInputPurpose,
    EvidenceKind,
    EvidenceNotFound,
    EvidenceReadScope,
    EvidenceSetDescriptor,
    ReviewEvidenceDraft,
    ValidationEvidenceDraft,
)
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.evidence import (
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    ValidationEvidenceMember,
    decode_evidence_manifest,
    encode_evidence_manifest,
)
from forge.persistence.models import (
    AgentExecution,
    AgentExecutionEvidenceInput,
    EvidenceSet,
    Review,
    Run,
    Step,
    ValidationResult,
)

_MEDIA_TYPE = "application/vnd.forge.evidence-manifest+json"


class PostgresEvidenceRepository:
    """Uses the caller's session; it never commits or rolls back it."""

    def __init__(self, session: AsyncSession, *, artifacts: ArtifactRepository) -> None:
        self._session = session
        self._artifacts = artifacts

    async def record_set(
        self,
        draft: ValidationEvidenceDraft | ReviewEvidenceDraft,
        artifact: CanonicalEvidenceArtifact,
    ) -> EvidenceSetDescriptor:
        manifest = draft.manifest
        encoded = encode_evidence_manifest(manifest)
        canonical_manifest = decode_evidence_manifest(encoded)
        if canonical_manifest != manifest:
            raise EvidenceCorruptLineage("manifest changed during canonical encoding")
        manifest = canonical_manifest
        projection_members: tuple[tuple[ValidationEvidenceMember, UUID], ...] = ()
        if isinstance(draft, ValidationEvidenceDraft):
            projection_members = tuple((item.member, item.output_artifact_id) for item in draft.members)
            if not isinstance(manifest, ValidationEvidenceManifest) or (
                tuple(member for member, _ in projection_members) != manifest.members
            ):
                raise EvidenceConflict("validation draft members differ from manifest")
        descriptor = artifact.descriptor
        if artifact.manifest != manifest or artifact.canonical_bytes != encoded:
            raise EvidenceCorruptLineage("canonical bytes differ from manifest")
        if (
            descriptor.digest != hashlib.sha256(encoded).hexdigest()
            or descriptor.byte_count != len(encoded)
            or descriptor.media_type != _MEDIA_TYPE
            or descriptor.schema_version != 1
            or descriptor.truncated
            or descriptor.run_id != manifest.run_id
            or descriptor.producer_type != "evidence_set"
            or descriptor.producer_id != manifest.evidence_set_id
        ):
            raise EvidenceCorruptLineage("canonical evidence descriptor is invalid")
        run = await self._locked_run(manifest.run_id)
        if run.policy_version != manifest.policy_version:
            raise EvidenceCorruptLineage("evidence policy differs from locked run")
        if descriptor.parent_digests != await self._expected_parent_digests(manifest):
            raise EvidenceCorruptLineage("canonical evidence parents differ from manifest")
        stored = await self._artifacts.get_by_digest(descriptor.digest, run_id=manifest.run_id)
        if stored != descriptor:
            raise EvidenceCorruptLineage("stored artifact metadata differs")
        row = await self._session.get(EvidenceSet, manifest.evidence_set_id, with_for_update=True)
        if row is not None:
            await self._validate_members(manifest, projection_members)
            if not self._matches(row, manifest, descriptor):
                raise EvidenceConflict("same evidence-set id has different immutable data")
            return self._descriptor(row, descriptor)
        row = await self._row(manifest, descriptor)
        await self._validate_members(manifest, projection_members)
        self._session.add(row)
        await self._session.flush()
        if isinstance(manifest, ValidationEvidenceManifest):
            await self._project_validation(manifest, projection_members)
        else:
            await self._project_review(ReviewEvidenceDraft(manifest), row)
        return self._descriptor(row, descriptor)

    async def bind_input(
        self, consumer_execution_id: UUID, purpose: EvidenceInputPurpose,
        evidence_set_id: UUID, *, run_id: UUID,
    ) -> None:
        execution = await self._session.get(AgentExecution, consumer_execution_id, with_for_update=True)
        evidence = await self._session.get(EvidenceSet, evidence_set_id, with_for_update=True)
        if execution is None or evidence is None or execution.run_id != run_id or evidence.run_id != run_id:
            raise EvidenceNotFound("binding target was not found")
        expected_kind = self._purpose_kind(purpose).value
        if execution.role != "reviewer" or execution.status != "PENDING" or evidence.kind != expected_kind:
            raise EvidenceConflict("binding target is ineligible")
        existing = await self._input(consumer_execution_id, purpose)
        if existing is not None:
            if existing.id == evidence_set_id:
                return
            raise EvidenceConflict("evidence input is immutable")
        if purpose is EvidenceInputPurpose.PRIOR_REVIEW:
            validation = await self._input(consumer_execution_id, EvidenceInputPurpose.VALIDATION_RESULTS)
            if validation is None or validation.prior_review_evidence_set_id != evidence_set_id:
                raise EvidenceConflict("prior review is not causal")
        self._session.add(AgentExecutionEvidenceInput(
            consumer_execution_id=consumer_execution_id, run_id=run_id,
            purpose=purpose.value, evidence_set_id=evidence_set_id, evidence_kind=expected_kind,
        ))
        await self._session.flush()

    async def input_for_execution(
        self, purpose: EvidenceInputPurpose, scope: EvidenceReadScope,
    ) -> EvidenceSetDescriptor | None:
        execution = await self._session.get(AgentExecution, scope.consumer_execution_id)
        if (
            execution is None or execution.run_id != scope.run_id
            or execution.step_id != scope.consumer_step_id or execution.role != "reviewer"
            or execution.status != "RUNNING"
        ):
            raise EvidenceCorruptLineage("consumer does not match read scope")
        consumer_step = await self._session.get(Step, scope.consumer_step_id)
        if consumer_step is None or consumer_step.run_id != scope.run_id or consumer_step.status != "RUNNING":
            raise EvidenceCorruptLineage("consumer step is not running")
        row = await self._input(scope.consumer_execution_id, purpose)
        if row is None:
            return None
        if row.run_id != scope.run_id or row.policy_version != scope.policy_version or row.kind != self._purpose_kind(purpose).value:
            raise EvidenceCorruptLineage("input binding is malformed")
        if purpose is EvidenceInputPurpose.VALIDATION_RESULTS and row.head_sha != scope.head_sha:
            raise EvidenceCorruptLineage("validation head differs")
        validation = row
        if purpose is EvidenceInputPurpose.PRIOR_REVIEW:
            bound_validation = await self._input(
                scope.consumer_execution_id, EvidenceInputPurpose.VALIDATION_RESULTS
            )
            if bound_validation is None or bound_validation.prior_review_evidence_set_id != row.id:
                raise EvidenceCorruptLineage("prior review binding is not causal")
            if (
                bound_validation.kind != EvidenceKind.VALIDATION.value
                or bound_validation.run_id != scope.run_id
                or bound_validation.policy_version != scope.policy_version
                or bound_validation.head_sha != scope.head_sha
            ):
                raise EvidenceCorruptLineage("bound validation does not match read scope")
            validation = bound_validation
        validation_step = await self._session.get(Step, validation.step_id)
        if (
            validation_step is None
            or validation_step.run_id != scope.run_id
            or validation_step.status != "SUCCEEDED"
        ):
            raise EvidenceCorruptLineage("validation producer step is not succeeded")
        if purpose is EvidenceInputPurpose.PRIOR_REVIEW:
            producer = await self._session.get(AgentExecution, row.producer_execution_id)
            if (
                producer is None
                or producer.status != "SUCCEEDED"
                or producer.completed_at is None
                or execution.started_at is None
                or producer.completed_at > execution.started_at
            ):
                raise EvidenceCorruptLineage("prior review producer is not eligible")
        artifact = await self._artifacts.get_by_digest(await self._digest(row.manifest_artifact_id), run_id=row.run_id)
        return self._descriptor(row, artifact)

    async def _row(self, manifest: ValidationEvidenceManifest | ReviewEvidenceManifest, descriptor: ArtifactDescriptor) -> EvidenceSet:
        artifact_id = descriptor.artifact_id
        if isinstance(manifest, ValidationEvidenceManifest):
            prior = None
            if manifest.prior_review_evidence_set_id is not None:
                prior = await self._set(manifest.prior_review_evidence_set_id, manifest.run_id)
                if prior.kind != "review" or prior.policy_version != manifest.policy_version:
                    raise EvidenceCorruptLineage("prior review parent is invalid")
            return EvidenceSet(id=manifest.evidence_set_id, run_id=manifest.run_id, step_id=manifest.step_id, kind="validation", policy_version=manifest.policy_version, head_sha=manifest.head_sha, manifest_artifact_id=artifact_id, prior_review_evidence_set_id=manifest.prior_review_evidence_set_id, prior_review_parent_policy_version=None if prior is None else prior.policy_version, prior_review_parent_kind=None if prior is None else prior.kind, review_finding_ids=None)
        validation = await self._set(manifest.validation_evidence_set_id, manifest.run_id)
        producer = await self._session.get(AgentExecution, manifest.producer_execution_id)
        if (
            validation.kind != "validation" or validation.policy_version != manifest.policy_version
            or validation.head_sha != manifest.head_sha or producer is None
            or producer.run_id != manifest.run_id or producer.step_id != manifest.step_id
            or producer.role != "reviewer"
        ):
            raise EvidenceCorruptLineage("review parent or producer is invalid")
        finding_ids = tuple(sorted(f.finding_id for f in manifest.review.findings))
        return EvidenceSet(id=manifest.evidence_set_id, run_id=manifest.run_id, step_id=manifest.step_id, kind="review", policy_version=manifest.policy_version, head_sha=manifest.head_sha, producer_execution_id=manifest.producer_execution_id, producer_step_id=manifest.step_id, producer_role="reviewer", manifest_artifact_id=artifact_id, validation_evidence_set_id=validation.id, validation_parent_policy_version=validation.policy_version, validation_parent_kind=validation.kind, validation_parent_head_sha=validation.head_sha, review_finding_ids=list(finding_ids))

    async def _expected_parent_digests(
        self, manifest: ValidationEvidenceManifest | ReviewEvidenceManifest
    ) -> tuple[str, ...]:
        if isinstance(manifest, ValidationEvidenceManifest):
            parents = {
                digest
                for member in manifest.members
                for digest in (
                    member.command_result_digest,
                    member.stdout_digest,
                    member.stderr_digest,
                )
            }
            if manifest.prior_review_evidence_set_id is not None:
                prior = await self._set(manifest.prior_review_evidence_set_id, manifest.run_id)
                parents.add(await self._digest(prior.manifest_artifact_id))
            return tuple(sorted(parents))
        validation = await self._set(manifest.validation_evidence_set_id, manifest.run_id)
        return (await self._digest(validation.manifest_artifact_id),)

    async def _project_validation(
        self,
        manifest: ValidationEvidenceManifest,
        members: tuple[tuple[ValidationEvidenceMember, UUID], ...],
    ) -> None:
        """Append exactly the manifest members; never infer a latest result."""

        for member, output_artifact_id in members:
            existing = await self._session.get(ValidationResult, member.result_id)
            if existing is not None:
                raise EvidenceConflict("validation result id already exists")
            self._session.add(
                ValidationResult(
                    id=member.result_id,
                    run_id=manifest.run_id,
                    step_id=manifest.step_id,
                    check_name=member.check_name,
                    command_name=member.command_name,
                    command_version=member.command_version,
                    status=member.status.value,
                    exit_code=member.exit_code,
                    output_artifact_id=output_artifact_id,
                    started_at=member.started_at,
                    completed_at=member.completed_at,
                )
            )
        await self._session.flush()

    async def _project_review(self, draft: ReviewEvidenceDraft, row: EvidenceSet) -> None:
        """Update a finding only when the immediate causal review contained it."""

        validation = await self._set(draft.manifest.validation_evidence_set_id, row.run_id)
        prior_ids: set[str] = set()
        if validation.prior_review_evidence_set_id is not None:
            prior = await self._set(validation.prior_review_evidence_set_id, row.run_id)
            prior_ids = set(prior.review_finding_ids or ())
        for finding in draft.manifest.review.findings:
            existing = (
                await self._session.execute(
                    select(Review)
                    .where(Review.run_id == row.run_id, Review.finding_id == finding.finding_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None and finding.finding_id not in prior_ids:
                raise EvidenceConflict("review finding collision is not causal")
            if existing is not None and validation.prior_review_evidence_set_id is not None:
                prior = await self._set(validation.prior_review_evidence_set_id, row.run_id)
                if existing.reviewer_execution_id != prior.producer_execution_id:
                    raise EvidenceConflict("review finding belongs to a sibling causal chain")
            values = {
                "step_id": row.step_id,
                "reviewer_execution_id": row.producer_execution_id,
                "severity": finding.severity.value,
                "path": finding.path,
                "start_line": finding.start_line,
                "summary": finding.summary,
                "evidence": finding.evidence,
                "proposed_resolution": finding.proposed_resolution,
                "status": "RESOLVED" if finding.resolved_at is not None else "OPEN",
                "decision": "PASS" if draft.manifest.review.decision.value == "approve" else "FAIL",
                "resolved_at": finding.resolved_at,
            }
            if existing is None:
                self._session.add(
                    Review(id=uuid4(), run_id=row.run_id, finding_id=finding.finding_id, **values)
                )
            else:
                for name, value in values.items():
                    setattr(existing, name, value)
        await self._session.flush()

    async def _locked_run(self, run_id: UUID) -> Run:
        run = (
            await self._session.execute(select(Run).where(Run.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None:
            raise EvidenceNotFound("run was not found")
        return run

    async def _set(self, evidence_set_id: UUID, run_id: UUID) -> EvidenceSet:
        row = await self._session.get(EvidenceSet, evidence_set_id, with_for_update=True)
        if row is None or row.run_id != run_id:
            raise EvidenceCorruptLineage("evidence parent was not found")
        return row

    async def _input(self, execution_id: UUID, purpose: EvidenceInputPurpose) -> EvidenceSet | None:
        return (await self._session.execute(
            select(EvidenceSet).join(AgentExecutionEvidenceInput, AgentExecutionEvidenceInput.evidence_set_id == EvidenceSet.id).where(AgentExecutionEvidenceInput.consumer_execution_id == execution_id, AgentExecutionEvidenceInput.purpose == purpose.value)
        )).scalar_one_or_none()

    async def _digest(self, artifact_id: UUID) -> str:
        from forge.persistence.models import Artifact
        artifact = await self._session.get(Artifact, artifact_id)
        if artifact is None:
            raise EvidenceCorruptLineage("manifest artifact was not found")
        return artifact.digest

    async def _validate_members(
        self,
        manifest: ValidationEvidenceManifest | ReviewEvidenceManifest,
        members: tuple[tuple[ValidationEvidenceMember, UUID], ...],
    ) -> None:
        if isinstance(manifest, ValidationEvidenceManifest):
            for member, output_artifact_id in members:
                artifact = await self._artifacts.get_by_digest(
                    member.command_result_digest, run_id=manifest.run_id
                )
                if artifact.artifact_id != output_artifact_id:
                    raise EvidenceCorruptLineage("validation output artifact differs from manifest")

    @staticmethod
    def _purpose_kind(purpose: EvidenceInputPurpose) -> EvidenceKind:
        return EvidenceKind.VALIDATION if purpose is EvidenceInputPurpose.VALIDATION_RESULTS else EvidenceKind.REVIEW

    @staticmethod
    def _descriptor(row: EvidenceSet, artifact: ArtifactDescriptor) -> EvidenceSetDescriptor:
        return EvidenceSetDescriptor(row.id, row.run_id, row.step_id, EvidenceKind(row.kind), row.policy_version, row.head_sha, row.producer_execution_id, row.manifest_artifact_id, artifact.digest, artifact.media_type, artifact.byte_count, artifact.schema_version, row.validation_evidence_set_id, row.prior_review_evidence_set_id, None if row.review_finding_ids is None else tuple(row.review_finding_ids))

    @staticmethod
    def _matches(
        row: EvidenceSet,
        manifest: ValidationEvidenceManifest | ReviewEvidenceManifest,
        descriptor: ArtifactDescriptor,
    ) -> bool:
        if (
            row.manifest_artifact_id != descriptor.artifact_id
            or row.id != manifest.evidence_set_id
            or row.kind != manifest.kind
            or row.run_id != manifest.run_id
            or row.step_id != manifest.step_id
            or row.policy_version != manifest.policy_version
            or row.head_sha != manifest.head_sha
        ):
            return False
        if isinstance(manifest, ValidationEvidenceManifest):
            return (
                row.producer_execution_id is None
                and row.validation_evidence_set_id is None
                and row.prior_review_evidence_set_id == manifest.prior_review_evidence_set_id
                and row.review_finding_ids is None
            )
        return (
            row.producer_execution_id == manifest.producer_execution_id
            and row.validation_evidence_set_id == manifest.validation_evidence_set_id
            and row.prior_review_evidence_set_id is None
            and tuple(row.review_finding_ids or ())
            == tuple(sorted(finding.finding_id for finding in manifest.review.findings))
        )
