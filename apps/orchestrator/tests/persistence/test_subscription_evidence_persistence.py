"""PostgreSQL stores content-bound validation and actual subscription evidence identities."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.evidence import CanonicalEvidenceArtifact, ValidationEvidenceDraft
from forge.application.ports.executions import ExecutionStatus
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.evidence import ValidationEvidenceManifest, encode_evidence_manifest
from forge.persistence.models import EvidenceSet, Step
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import text


@pytest.fixture(autouse=True)
async def _remove_disposable_content_evidence(session_factory):
    # This module uses the disposable PostgreSQL fixture; downgrade must never
    # discard retained evidence in an operator database.
    yield
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE evidence_sets CASCADE"))
        await session.commit()


@pytest.mark.integration
async def test_record_validation_v2_preserves_candidate_contents(
    session_factory, persisted_run, tmp_path
):
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        step_id = uuid4()
        work.session.add(
            Step(
                id=step_id,
                run_id=persisted_run.id,
                kind="validation",
                attempt=1,
                status="SUCCEEDED",
            )
        )
        manifest = ValidationEvidenceManifest(
            schema_version=2,
            evidence_set_id=uuid4(),
            run_id=persisted_run.id,
            step_id=step_id,
            policy_version=1,
            head_sha="a" * 40,
            candidate_tree_digest="c" * 64,
        )
        wire = encode_evidence_manifest(manifest)
        descriptor = await store.put_bytes(
            wire, media_type="application/vnd.forge.evidence-manifest+json"
        )
        descriptor = await work.artifacts.record(
            replace(descriptor, schema_version=2),
            run_id=persisted_run.id,
            producer_type="evidence_set",
            producer_id=manifest.evidence_set_id,
        )
        draft, artifact = (
            ValidationEvidenceDraft(manifest, ()),
            CanonicalEvidenceArtifact(descriptor, manifest, wire),
        )
        result = await work.evidence.record_set(draft, artifact)
        assert result.manifest_schema_version == 2
        assert result.candidate_tree_digest == manifest.candidate_tree_digest
        assert await work.evidence.record_set(draft, artifact) == result
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        retained = await work.evidence.get_by_id(result.evidence_set_id, run_id=persisted_run.id)
        assert retained == result
        row = await work.session.get(EvidenceSet, result.evidence_set_id)
        assert row.candidate_tree_digest == manifest.candidate_tree_digest


@pytest.mark.integration
async def test_migration_preserves_existing_v1_evidence(
    session_factory, persisted_run, tmp_path, migrated_database_url, alembic_config_factory
):
    import asyncio

    from alembic import command

    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        step_id = uuid4()
        await work.controller_steps.admit(persisted_run.id, step_id, "validate", 1)
        manifest = ValidationEvidenceManifest(
            evidence_set_id=uuid4(),
            run_id=persisted_run.id,
            step_id=step_id,
            policy_version=1,
            head_sha="a" * 40,
        )
        wire = encode_evidence_manifest(manifest)
        artifact = await store.put_bytes(
            wire, media_type="application/vnd.forge.evidence-manifest+json"
        )
        artifact = await work.artifacts.record(
            artifact,
            run_id=persisted_run.id,
            producer_type="evidence_set",
            producer_id=manifest.evidence_set_id,
        )
        before = await work.evidence.record_set(
            ValidationEvidenceDraft(manifest, ()),
            CanonicalEvidenceArtifact(artifact, manifest, wire),
        )
        await work.controller_steps.finalize(
            persisted_run.id,
            step_id,
            ExecutionStatus.SUCCEEDED,
            output_artifact_id=artifact.artifact_id,
        )
        await work.commit()

    config = alembic_config_factory(migrated_database_url)
    await asyncio.to_thread(command.downgrade, config, "20260911_0017")
    async with session_factory() as session:
        retained = (
            await session.execute(
                text("SELECT id, kind, head_sha FROM evidence_sets WHERE id = :identity"),
                {"identity": manifest.evidence_set_id},
            )
        ).one()
        assert retained == (manifest.evidence_set_id, "validation", manifest.head_sha)
    await asyncio.to_thread(command.upgrade, config, "head")
    async with PostgresUnitOfWork(session_factory) as work:
        after = await work.evidence.get_by_id(manifest.evidence_set_id, run_id=persisted_run.id)
        assert before == after
        assert (
            after.candidate_tree_digest
            is after.producer_task_id
            is after.producer_attempt_id
            is None
        )
        assert await store.open_bytes(after.manifest_digest) == wire
