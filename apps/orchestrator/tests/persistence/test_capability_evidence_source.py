"""PostgreSQL is authoritative for current capability evidence selection."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.artifact import canonical_storage_pointer
from forge.domain.capability_evidence import (
    CapabilityEvidenceIdentity,
    CapabilityEvidenceManifest,
    CapabilityProof,
    CapabilityProofKind,
)
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    SpecialistPurpose,
)
from forge.domain.tool import ToolName
from forge.persistence.repositories.capability_evidence import (
    CapabilityEvidenceConflict,
    CapabilityEvidenceUnavailable,
    PostgresCapabilityEvidenceSource,
)
from sqlalchemy import text


@pytest_asyncio.fixture(autouse=True)
async def _remove_disposable_capability_evidence(session_factory):
    yield
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM capability_evidence"))


def _identity(*, model: str = "gpt-6-astra") -> CapabilityEvidenceIdentity:
    return CapabilityEvidenceIdentity(
        provider="openai",
        client="codex_app_server",
        client_version="0.153.4",
        executable_digest="1" * 64,
        client_home_digest="2" * 64,
        account="personal-chatgpt",
        model=model,
        effort=ReasoningEffort.LOW,
        role=SpecialistPurpose.PRIMARY,
        tool_surface=(ToolName.REPOSITORY_READ_FILE,),
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


async def _manifest(
    now: datetime,
    *,
    artifacts: FilesystemArtifactStore,
    identity: CapabilityEvidenceIdentity | None = None,
    evidence_id=None,
) -> CapabilityEvidenceManifest:
    proofs = []
    for kind in CapabilityProofKind:
        descriptor = await artifacts.put_bytes(
            f"sanitized {kind.value} proof".encode(),
            media_type="application/vnd.forge.capability-proof+json",
        )
        proofs.append(CapabilityProof(kind=kind, artifact_digest=descriptor.digest))
    return CapabilityEvidenceManifest(
        evidence_id=evidence_id or uuid4(),
        identity=identity or _identity(),
        verifier_id="codex-conformance",
        verifier_version="1",
        observed_at=now,
        expires_at=now + timedelta(hours=1),
        proofs=tuple(proofs),
    )


@pytest.mark.integration
async def test_publish_replay_and_restart_resolve_the_same_artifact(
    session_factory, tmp_path
) -> None:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    writer = PostgresCapabilityEvidenceSource(session_factory, artifacts, clock=lambda: now)
    manifest = await _manifest(now, artifacts=artifacts)

    published = await writer.publish(manifest)
    assert await writer.publish(manifest) == published

    restarted = PostgresCapabilityEvidenceSource(session_factory, artifacts, clock=lambda: now)
    assert await restarted.resolve(manifest.identity) == published


@pytest.mark.integration
async def test_missing_stale_mismatched_and_changed_replay_evidence_is_rejected(
    session_factory, tmp_path
) -> None:
    clock = [datetime(2026, 9, 13, 12, tzinfo=UTC)]
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    source = PostgresCapabilityEvidenceSource(session_factory, artifacts, clock=lambda: clock[0])
    manifest = await _manifest(clock[0], artifacts=artifacts)

    with pytest.raises(CapabilityEvidenceUnavailable, match="missing"):
        await source.resolve(manifest.identity)
    await source.publish(manifest)
    with pytest.raises(CapabilityEvidenceUnavailable, match="missing"):
        await source.resolve(manifest.identity.model_copy(update={"model": "gpt-5.6-sol"}))
    with pytest.raises(CapabilityEvidenceConflict, match="replay"):
        await source.publish(manifest.model_copy(update={"verifier_version": "2"}))
    older = await _manifest(clock[0] - timedelta(minutes=1), artifacts=artifacts)
    with pytest.raises(CapabilityEvidenceConflict, match="not newer"):
        await source.publish(older)
    assert (await source.resolve(manifest.identity)).manifest.evidence_id == manifest.evidence_id

    clock[0] = manifest.expires_at
    with pytest.raises(CapabilityEvidenceUnavailable, match="stale"):
        await source.resolve(manifest.identity)


@pytest.mark.integration
async def test_missing_or_corrupt_artifact_bytes_reject_current_evidence(
    session_factory, tmp_path
) -> None:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    root = tmp_path / "artifacts"
    artifacts = FilesystemArtifactStore(root)
    source = PostgresCapabilityEvidenceSource(session_factory, artifacts, clock=lambda: now)
    manifest = await _manifest(now, artifacts=artifacts)
    published = await source.publish(manifest)

    proof_path = root / canonical_storage_pointer(manifest.proofs[0].artifact_digest)
    proof_path.unlink()
    with pytest.raises(CapabilityEvidenceUnavailable, match="proof artifact"):
        await source.resolve(manifest.identity)

    await artifacts.put_bytes(
        f"sanitized {manifest.proofs[0].kind.value} proof".encode(),
        media_type="application/vnd.forge.capability-proof+json",
    )
    manifest_path = root / canonical_storage_pointer(published.artifact_digest)
    manifest_path.write_bytes(b"corrupt capability evidence")
    with pytest.raises(CapabilityEvidenceUnavailable, match="evidence artifact"):
        await source.resolve(manifest.identity)


@pytest.mark.integration
async def test_replacement_and_stale_invalidation_are_atomic_across_sources(
    session_factory, tmp_path
) -> None:
    clock = [datetime(2026, 9, 13, 12, tzinfo=UTC)]
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    publisher = PostgresCapabilityEvidenceSource(session_factory, artifacts, clock=lambda: clock[0])
    invalidator = PostgresCapabilityEvidenceSource(
        session_factory, artifacts, clock=lambda: clock[0]
    )
    original = await _manifest(clock[0], artifacts=artifacts)
    first = await publisher.publish(original)
    clock[0] += timedelta(minutes=1)
    replacement = await _manifest(clock[0], artifacts=artifacts)

    published, _ = await asyncio.gather(
        publisher.publish(replacement),
        invalidator.invalidate(original.identity, evidence_id=original.evidence_id),
    )

    assert published.revision == first.revision + 1
    assert await invalidator.resolve(original.identity) == published
    with pytest.raises(CapabilityEvidenceUnavailable, match="replaced"):
        await publisher.resolve(original.identity, evidence_id=original.evidence_id)
    assert not await invalidator.invalidate(original.identity, evidence_id=original.evidence_id)
    assert await invalidator.invalidate(replacement.identity, evidence_id=replacement.evidence_id)
    with pytest.raises(CapabilityEvidenceUnavailable, match="missing"):
        await publisher.resolve(replacement.identity)
