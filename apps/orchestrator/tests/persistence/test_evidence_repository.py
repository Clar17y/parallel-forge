"""Real PostgreSQL contracts for evidence-set projection persistence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceCorruptLineage,
    ValidationEvidenceDraft,
    ValidationProjectionMember,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.evidence import (
    EvidenceStatus,
    ValidationEvidenceManifest,
    ValidationEvidenceMember,
    encode_evidence_manifest,
)
from forge.persistence.models import Step, ValidationResult
from sqlalchemy import func, select


@pytest.mark.integration
async def test_record_validation_set_projects_each_manifest_member(
    tmp_path, session_factory, persisted_run
) -> None:
    """A new immutable set must append its exact validation attempt projection."""

    from forge.persistence.unit_of_work import PostgresUnitOfWork

    step_id = uuid4()
    now = datetime.now(UTC)
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        work.session.add(
            Step(
                id=step_id,
                run_id=persisted_run.id,
                kind="validation",
                attempt=1,
                status="SUCCEEDED",
            )
        )
        output = await store.put_bytes(b"result", media_type="text/plain")
        output = await work.artifacts.record(output, run_id=persisted_run.id, producer_type="test")
        member = ValidationEvidenceMember(
            result_id=uuid4(),
            check_name="unit",
            command_name="pytest",
            command_version=1,
            command_digest="1" * 64,
            command_result_digest=output.digest,
            stdout_digest=output.digest,
            stderr_digest=output.digest,
            status=EvidenceStatus.PASSED,
            exit_code=0,
            started_at=now,
            completed_at=now,
        )
        manifest = ValidationEvidenceManifest(
            evidence_set_id=uuid4(),
            run_id=persisted_run.id,
            step_id=step_id,
            policy_version=1,
            head_sha="a" * 40,
            members=(member,),
        )
        wire = encode_evidence_manifest(manifest)
        descriptor = await store.put_bytes(
            wire, media_type="application/vnd.forge.evidence-manifest+json"
        )
        descriptor = await work.artifacts.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="evidence_set",
            producer_id=manifest.evidence_set_id,
            parent_digests=(output.digest,),
        )
        draft = ValidationEvidenceDraft(
            manifest, (ValidationProjectionMember(member, output.artifact_id),)
        )
        artifact = CanonicalEvidenceArtifact(descriptor, manifest, wire)
        with pytest.raises(EvidenceCorruptLineage):
            await work.evidence.record_set(
                draft,
                CanonicalEvidenceArtifact(replace(descriptor, parent_digests=()), manifest, wire),
            )
        await work.evidence.record_set(draft, artifact)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_lock = work.evidence._locked_run

        async def barrier(run_id):
            entered.set()
            await release.wait()
            return await original_lock(run_id)

        work.evidence._locked_run = barrier
        pending = asyncio.create_task(work.evidence.record_set(draft, artifact))
        await entered.wait()
        object.__setattr__(draft, "members", ())
        release.set()
        await pending
        assert await work.session.get(ValidationResult, member.result_id) is not None
        assert (
            await work.session.execute(select(func.count()).select_from(ValidationResult))
        ).scalar_one() == 1
        await work.commit()
