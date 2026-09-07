"""Verified reader for immutable evidence artifacts bound to an execution."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.evidence import (
    EvidenceCorruptLineage,
    EvidenceError,
    EvidenceInputPurpose,
    EvidenceKind,
    EvidenceReadScope,
    EvidenceSetDescriptor,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.evidence import (
    EvidenceManifest,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
)

_MANIFEST_MEDIA_TYPE = "application/vnd.forge.evidence-manifest+json"
_MAX_MANIFEST_BYTES = 256 * 1024
_CORRUPT_MESSAGE = "evidence lineage is corrupt"


class EvidenceReader:
    """Read only a repository-bound, canonical evidence manifest.

    The supplied scope is capability-like input from a controlled service.  This
    class does not derive a scope from model input or from a repository search.
    """

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        artifact_store: ArtifactStore,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._artifact_store = artifact_store

    async def read(
        self, purpose: EvidenceInputPurpose, scope: EvidenceReadScope
    ) -> EvidenceManifest | None:
        try:
            async with self._unit_of_work_factory() as work:
                bound = await work.evidence.input_for_execution(purpose, scope)
                if bound is None:
                    return None
                descriptor = await work.artifacts.get_by_digest(
                    bound.manifest_digest, run_id=scope.run_id
                )
                self._validate_descriptor(bound, descriptor, purpose, scope)
                manifest = await self._load_manifest(descriptor)
                self._validate_manifest(bound, manifest, purpose, scope)
                await self._validate_parents(work, descriptor, manifest, scope)
                return manifest
        except EvidenceError:
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE) from None
        except LookupError, OSError, TypeError, ValueError, UnicodeError:
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE) from None

    async def _load_manifest(self, descriptor: ArtifactDescriptor) -> EvidenceManifest:
        if not await self._artifact_store.verify(descriptor.digest):
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)
        data = await self._artifact_store.open_bytes(descriptor.digest)
        if not isinstance(data, bytes) or len(data) != descriptor.byte_count:
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)
        if hashlib.sha256(data).hexdigest() != descriptor.digest:
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)
        return decode_evidence_manifest(data)

    @staticmethod
    def _validate_descriptor(
        bound: EvidenceSetDescriptor,
        descriptor: ArtifactDescriptor,
        purpose: EvidenceInputPurpose,
        scope: EvidenceReadScope,
    ) -> None:
        expected_kind = (
            EvidenceKind.VALIDATION
            if purpose is EvidenceInputPurpose.VALIDATION_RESULTS
            else EvidenceKind.REVIEW
        )
        if (
            bound.kind is not expected_kind
            or bound.run_id != scope.run_id
            or bound.policy_version != scope.policy_version
            or descriptor.artifact_id != bound.manifest_artifact_id
            or descriptor.digest != bound.manifest_digest
            or descriptor.run_id != bound.run_id
            or descriptor.media_type != bound.manifest_media_type
            or descriptor.byte_count != bound.manifest_byte_count
            or descriptor.schema_version != bound.manifest_schema_version
            or descriptor.media_type != _MANIFEST_MEDIA_TYPE
            or descriptor.schema_version != 1
            or descriptor.byte_count > _MAX_MANIFEST_BYTES
            or descriptor.truncated
            or descriptor.original_byte_count != descriptor.byte_count
            or descriptor.truncation_policy != "none"
            or descriptor.producer_type != "evidence_set"
            or descriptor.producer_id != bound.evidence_set_id
        ):
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)

    @staticmethod
    def _validate_manifest(
        bound: EvidenceSetDescriptor,
        manifest: EvidenceManifest,
        purpose: EvidenceInputPurpose,
        scope: EvidenceReadScope,
    ) -> None:
        if (
            manifest.evidence_set_id != bound.evidence_set_id
            or manifest.run_id != bound.run_id
            or manifest.step_id != bound.step_id
            or manifest.kind != bound.kind.value
            or manifest.policy_version != bound.policy_version
            or manifest.head_sha != bound.head_sha
        ):
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)
        if isinstance(manifest, ValidationEvidenceManifest):
            if (
                purpose is not EvidenceInputPurpose.VALIDATION_RESULTS
                or manifest.head_sha != scope.head_sha
                or bound.producer_execution_id is not None
                or bound.validation_evidence_set_id is not None
                or bound.prior_review_evidence_set_id != manifest.prior_review_evidence_set_id
                or bound.review_finding_ids is not None
            ):
                raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)
            return
        if (
            purpose is not EvidenceInputPurpose.PRIOR_REVIEW
            or bound.producer_execution_id != manifest.producer_execution_id
            or bound.validation_evidence_set_id != manifest.validation_evidence_set_id
            or bound.prior_review_evidence_set_id is not None
            or bound.review_finding_ids
            != tuple(sorted(f.finding_id for f in manifest.review.findings))
        ):
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)

    async def _validate_parents(
        self,
        work: UnitOfWork,
        descriptor: ArtifactDescriptor,
        manifest: EvidenceManifest,
        scope: EvidenceReadScope,
    ) -> None:
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
                prior = await work.evidence.get_by_id(
                    manifest.prior_review_evidence_set_id, run_id=scope.run_id
                )
                if (
                    prior.kind is not EvidenceKind.REVIEW
                    or prior.run_id != scope.run_id
                    or prior.policy_version != scope.policy_version
                ):
                    raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)
                parents.add(prior.manifest_digest)
            if descriptor.parent_digests != tuple(sorted(parents)):
                raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)
            return

        validation = await work.evidence.get_by_id(
            manifest.validation_evidence_set_id, run_id=scope.run_id
        )
        if (
            validation.kind is not EvidenceKind.VALIDATION
            or validation.run_id != scope.run_id
            or validation.policy_version != manifest.policy_version
            or validation.head_sha != manifest.head_sha
            or descriptor.parent_digests != (validation.manifest_digest,)
        ):
            raise EvidenceCorruptLineage(_CORRUPT_MESSAGE)


__all__ = ["EvidenceReader"]
