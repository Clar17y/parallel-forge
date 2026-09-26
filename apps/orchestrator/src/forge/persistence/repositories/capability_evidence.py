"""Artifact-verified, current capability evidence selected by PostgreSQL."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.capability_evidence import (
    CapabilityEvidenceConflict,
    CapabilityEvidenceInvalid,
    CapabilityEvidenceMissing,
    CapabilityEvidenceSourceError,
    CapabilityEvidenceUnavailable,
)
from forge.artifacts._errors import ArtifactIntegrityError, ArtifactStoreError
from forge.domain.artifact import canonical_storage_pointer
from forge.domain.capability_evidence import (
    CapabilityEvidenceError,
    CapabilityEvidenceIdentity,
    CapabilityEvidenceManifest,
    ResolvedCapabilityEvidence,
    decode_capability_evidence,
    encode_capability_evidence,
)
from forge.domain.capability_proof import CapabilityProofError, validate_proof
from forge.persistence.models.capability_evidence import CapabilityEvidence
from forge.persistence.models.execution import Artifact

CAPABILITY_EVIDENCE_MEDIA_TYPE = "application/vnd.forge.client-capability+json"


class PostgresCapabilityEvidenceSource:
    """Persist immutable evidence and resolve only one current identity revision."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        artifacts: ArtifactStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(session_factory) or not all(
            callable(getattr(artifacts, name, None))
            for name in ("put_bytes", "open_bytes", "verify")
        ):
            raise TypeError("capability evidence requires PostgreSQL and artifact boundaries")
        self._factory = session_factory
        self._artifacts = artifacts
        self._clock = clock

    async def publish(self, manifest: CapabilityEvidenceManifest) -> ResolvedCapabilityEvidence:
        wire = _wire(manifest)
        wire_digest = hashlib.sha256(wire).hexdigest()
        # Replay identity is immutable.  Check it before proof artifacts so a
        # malformed replacement cannot disguise the conflict as unavailability.
        try:
            async with self._factory() as session:
                existing = await _record_by_id(session, manifest.evidence_id)
            if existing is not None:
                _record, artifact = existing
                if artifact.digest != wire_digest:
                    raise CapabilityEvidenceConflict("capability evidence replay differs")
        except CapabilityEvidenceConflict:
            raise
        except SQLAlchemyError:
            raise CapabilityEvidenceSourceError(
                "capability evidence could not be persisted"
            ) from None
        for proof in manifest.proofs:
            try:
                proof_wire = await self._artifacts.open_bytes(
                    proof.artifact_digest, max_bytes=16 * 1024
                )
                validate_proof(proof_wire, proof.kind, manifest)
                if not await self._artifacts.verify(proof.artifact_digest):
                    raise CapabilityEvidenceUnavailable("capability proof artifact is unavailable")
            except (
                ArtifactIntegrityError,
                ArtifactStoreError,
                CapabilityProofError,
                OSError,
                AttributeError,
                TypeError,
            ):
                raise CapabilityEvidenceUnavailable(
                    "capability proof artifact is unavailable"
                ) from None
        try:
            descriptor = await self._artifacts.put_bytes(
                wire, media_type=CAPABILITY_EVIDENCE_MEDIA_TYPE
            )
        except ArtifactIntegrityError, ArtifactStoreError, OSError:
            raise CapabilityEvidenceSourceError(
                "capability evidence artifact could not be stored"
            ) from None
        try:
            descriptor_is_valid = (
                descriptor.digest == hashlib.sha256(wire).hexdigest()
                and descriptor.media_type == CAPABILITY_EVIDENCE_MEDIA_TYPE
                and descriptor.byte_count == len(wire)
                and not descriptor.truncated
                and await self._artifacts.verify(descriptor.digest)
            )
        except ArtifactIntegrityError, ArtifactStoreError, OSError, AttributeError, TypeError:
            raise CapabilityEvidenceSourceError(
                "capability evidence artifact is unavailable"
            ) from None
        if not descriptor_is_valid:
            raise CapabilityEvidenceSourceError("capability evidence artifact descriptor differs")

        try:
            async with self._factory() as session, session.begin():
                now = await self._now(session)
                if not manifest.observed_at <= now < manifest.expires_at:
                    raise CapabilityEvidenceUnavailable(
                        "capability evidence is not currently valid"
                    )
                await _identity_lock(session, manifest.identity.digest)
                existing = await _record_by_id(session, manifest.evidence_id)
                if existing is not None:
                    record, artifact = existing
                    if not _same_record(record, artifact, manifest, descriptor.digest, len(wire)):
                        raise CapabilityEvidenceConflict("capability evidence replay differs")
                    if record.invalidated_at is not None:
                        raise CapabilityEvidenceUnavailable("capability evidence was replaced")
                    return ResolvedCapabilityEvidence(
                        manifest=manifest,
                        artifact_digest=artifact.digest,
                        revision=record.revision,
                    )

                artifact = await _artifact_row(
                    session, manifest.identity.digest, descriptor.digest, len(wire)
                )
                current = await session.scalar(
                    select(CapabilityEvidence)
                    .where(
                        CapabilityEvidence.identity_digest == manifest.identity.digest,
                        CapabilityEvidence.invalidated_at.is_(None),
                    )
                    .with_for_update()
                )
                if current is not None:
                    if manifest.observed_at <= current.observed_at:
                        raise CapabilityEvidenceConflict(
                            "replacement capability evidence is not newer"
                        )
                    current.invalidated_at = now
                    await session.flush()
                latest = await session.scalar(
                    select(func.max(CapabilityEvidence.revision)).where(
                        CapabilityEvidence.identity_digest == manifest.identity.digest
                    )
                )
                revision = int(latest or 0) + 1
                session.add(
                    CapabilityEvidence(
                        id=manifest.evidence_id,
                        identity_digest=manifest.identity.digest,
                        revision=revision,
                        artifact_id=artifact.id,
                        observed_at=manifest.observed_at,
                        expires_at=manifest.expires_at,
                    )
                )
                await session.flush()
                return ResolvedCapabilityEvidence(
                    manifest=manifest,
                    artifact_digest=descriptor.digest,
                    revision=revision,
                )
        except CapabilityEvidenceConflict, CapabilityEvidenceUnavailable:
            raise
        except IntegrityError:
            raise CapabilityEvidenceConflict(
                "capability evidence violated an immutable identity"
            ) from None
        except SQLAlchemyError:
            raise CapabilityEvidenceSourceError(
                "capability evidence could not be persisted"
            ) from None

    async def resolve(
        self,
        identity: CapabilityEvidenceIdentity,
        *,
        evidence_id: UUID | None = None,
    ) -> ResolvedCapabilityEvidence:
        if not isinstance(identity, CapabilityEvidenceIdentity):
            raise TypeError("capability evidence identity is required")
        if evidence_id is not None and (not isinstance(evidence_id, UUID) or evidence_id.int == 0):
            raise ValueError("capability evidence id must be a non-nil UUID")
        try:
            async with self._factory() as session, session.begin():
                now = await self._now(session)
                row = (
                    await session.execute(
                        select(CapabilityEvidence, Artifact)
                        .join(Artifact, Artifact.id == CapabilityEvidence.artifact_id)
                        .where(
                            CapabilityEvidence.identity_digest == identity.digest,
                            CapabilityEvidence.invalidated_at.is_(None),
                        )
                        .with_for_update(read=True)
                    )
                ).one_or_none()
                if row is None:
                    raise CapabilityEvidenceMissing("capability evidence is missing")
                record, artifact = row
                if evidence_id is not None and record.id != evidence_id:
                    raise CapabilityEvidenceInvalid("capability evidence was replaced")
                if not record.observed_at <= now < record.expires_at:
                    raise CapabilityEvidenceInvalid("capability evidence is stale")
                try:
                    wire = await self._artifacts.open_bytes(artifact.digest, max_bytes=64 * 1024)
                    manifest = decode_capability_evidence(wire)
                except (
                    ArtifactIntegrityError,
                    ArtifactStoreError,
                    CapabilityEvidenceError,
                    OSError,
                ):
                    raise CapabilityEvidenceUnavailable(
                        "capability evidence artifact is unavailable"
                    ) from None
                if (
                    not _same_record(record, artifact, manifest, artifact.digest, len(wire))
                    or manifest.identity != identity
                ):
                    raise CapabilityEvidenceInvalid("capability evidence identity differs")
                for proof in manifest.proofs:
                    try:
                        proof_wire = await self._artifacts.open_bytes(
                            proof.artifact_digest, max_bytes=16 * 1024
                        )
                        validate_proof(proof_wire, proof.kind, manifest)
                        if not await self._artifacts.verify(proof.artifact_digest):
                            raise CapabilityEvidenceUnavailable(
                                "capability proof artifact is unavailable"
                            )
                    except (
                        ArtifactIntegrityError,
                        ArtifactStoreError,
                        CapabilityProofError,
                        OSError,
                        AttributeError,
                        TypeError,
                    ):
                        raise CapabilityEvidenceUnavailable(
                            "capability proof artifact is unavailable"
                        ) from None
                return ResolvedCapabilityEvidence(
                    manifest=manifest,
                    artifact_digest=artifact.digest,
                    revision=record.revision,
                )
        except CapabilityEvidenceUnavailable:
            raise
        except SQLAlchemyError, TypeError, ValueError:
            raise CapabilityEvidenceInvalid("capability evidence could not be verified") from None

    async def invalidate(
        self,
        identity: CapabilityEvidenceIdentity,
        *,
        evidence_id: UUID | None = None,
    ) -> bool:
        if not isinstance(identity, CapabilityEvidenceIdentity):
            raise TypeError("capability evidence identity is required")
        if evidence_id is not None and (not isinstance(evidence_id, UUID) or evidence_id.int == 0):
            raise ValueError("capability evidence id must be a non-nil UUID")
        async with self._factory() as session, session.begin():
            now = await self._now(session)
            await _identity_lock(session, identity.digest)
            current = await session.scalar(
                select(CapabilityEvidence)
                .where(
                    CapabilityEvidence.identity_digest == identity.digest,
                    CapabilityEvidence.invalidated_at.is_(None),
                )
                .with_for_update()
            )
            if current is None or evidence_id is not None and current.id != evidence_id:
                return False
            current.invalidated_at = max(now, current.observed_at)
            await session.flush()
            return True

    async def _now(self, session: AsyncSession) -> datetime:
        value = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("capability evidence clock must be timezone-aware")
        return value.astimezone(UTC)


def _wire(manifest: CapabilityEvidenceManifest) -> bytes:
    if not isinstance(manifest, CapabilityEvidenceManifest):
        raise TypeError("capability evidence manifest is required")
    try:
        return encode_capability_evidence(manifest)
    except CapabilityEvidenceError:
        raise CapabilityEvidenceConflict("capability evidence manifest is invalid") from None


async def _identity_lock(session: AsyncSession, identity_digest: str) -> None:
    value = int(identity_digest[:16], 16)
    if value >= 2**63:
        value -= 2**64
    await session.execute(select(func.pg_advisory_xact_lock(value)))


async def _record_by_id(
    session: AsyncSession, evidence_id: UUID
) -> tuple[CapabilityEvidence, Artifact] | None:
    row = (
        await session.execute(
            select(CapabilityEvidence, Artifact)
            .join(Artifact, Artifact.id == CapabilityEvidence.artifact_id)
            .where(CapabilityEvidence.id == evidence_id)
            .with_for_update()
        )
    ).one_or_none()
    return cast(tuple[CapabilityEvidence, Artifact] | None, row)


async def _artifact_row(
    session: AsyncSession, identity_digest: str, digest: str, byte_count: int
) -> Artifact:
    metadata = {"kind": "client_capability", "identity_digest": identity_digest}
    candidate_id = uuid4()
    inserted = await session.scalar(
        insert(Artifact)
        .values(
            id=candidate_id,
            digest=digest,
            media_type=CAPABILITY_EVIDENCE_MEDIA_TYPE,
            storage_pointer=canonical_storage_pointer(digest),
            size_bytes=byte_count,
            metadata_schema_version=1,
            artifact_metadata=metadata,
        )
        .on_conflict_do_nothing(index_elements=[Artifact.digest])
        .returning(Artifact.id)
    )
    artifact = await session.get(Artifact, inserted or candidate_id)
    if artifact is None:
        artifact = await session.scalar(
            select(Artifact).where(Artifact.digest == digest).with_for_update()
        )
    if artifact is None or (
        artifact.media_type != CAPABILITY_EVIDENCE_MEDIA_TYPE
        or artifact.storage_pointer != canonical_storage_pointer(digest)
        or artifact.size_bytes != byte_count
        or artifact.metadata_schema_version != 1
        or artifact.artifact_metadata != metadata
    ):
        raise CapabilityEvidenceConflict("capability artifact metadata differs")
    return artifact


def _same_record(
    record: CapabilityEvidence,
    artifact: Artifact,
    manifest: CapabilityEvidenceManifest,
    artifact_digest: str,
    byte_count: int,
) -> bool:
    return (
        record.id == manifest.evidence_id
        and record.identity_digest == manifest.identity.digest
        and record.artifact_id == artifact.id
        and record.observed_at == manifest.observed_at
        and record.expires_at == manifest.expires_at
        and artifact.digest == artifact_digest
        and artifact.media_type == CAPABILITY_EVIDENCE_MEDIA_TYPE
        and artifact.storage_pointer == canonical_storage_pointer(artifact_digest)
        and artifact.size_bytes == byte_count
        and artifact.metadata_schema_version == 1
        and artifact.artifact_metadata
        == {"kind": "client_capability", "identity_digest": manifest.identity.digest}
    )


__all__ = [
    "CAPABILITY_EVIDENCE_MEDIA_TYPE",
    "CapabilityEvidenceConflict",
    "CapabilityEvidenceSourceError",
    "CapabilityEvidenceUnavailable",
    "PostgresCapabilityEvidenceSource",
]
