"""PostgreSQL is authoritative for current capability evidence selection."""

import asyncio
import json
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
from forge.domain.capability_proof import encode_proof
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
    selected_identity = identity or _identity()
    selected_id = evidence_id or uuid4()
    placeholder = "0" * 64
    draft = CapabilityEvidenceManifest(
        evidence_id=selected_id,
        identity=selected_identity,
        verifier_id="codex-conformance",
        verifier_version="1",
        observed_at=now,
        expires_at=now + timedelta(hours=1),
        proofs=tuple(
            CapabilityProof(kind=kind, artifact_digest=placeholder) for kind in CapabilityProofKind
        ),
    )
    proofs = []
    for kind in CapabilityProofKind:
        payload = {
            CapabilityProofKind.CLIENT_IDENTITY: {
                "client": selected_identity.client,
                "client_version": selected_identity.client_version,
                "executable_digest": selected_identity.executable_digest,
                "client_home_digest": selected_identity.client_home_digest,
                "executable_unchanged": True,
                "reported_client_version": selected_identity.client_version,
            },
            CapabilityProofKind.ACCOUNT_AUTHENTICATION: {
                "account": selected_identity.account,
                "auth_mode": "subscription",
                "authenticated": True,
                "account_kind": "chatgpt",
            },
            CapabilityProofKind.ROUTE_IDENTITY: {
                "model": selected_identity.model,
                "effort": selected_identity.effort.value,
                "catalog_supported": True,
                "turn_completed": True,
                "turn_observation_digest": "3" * 64,
            },
            CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING: {
                "auth_mode": "subscription",
                "billing_mode": "allowance_only",
                "paid_credential_names_scrubbed": True,
                "fallback_disabled": True,
                "subscription_route_observed": True,
            },
            CapabilityProofKind.TOOL_ISOLATION: {
                "tool_surface": [tool.value for tool in selected_identity.tool_surface],
                "isolated": True,
                "forbidden_tool_calls": 0,
                "side_effect_canaries_clear": True,
                "advertised_tool_surface_digest": "4" * 64,
            },
        }[kind]
        descriptor = await artifacts.put_bytes(
            encode_proof(kind, payload, draft),
            media_type="application/vnd.forge.client-capability-proof+json",
        )
        proofs.append(CapabilityProof(kind=kind, artifact_digest=descriptor.digest))
    return CapabilityEvidenceManifest(
        evidence_id=selected_id,
        identity=selected_identity,
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
async def test_antigravity_warning_survives_postgres_restart_and_paid_policy_is_rejected(
    session_factory, tmp_path
):
    from forge.application.services.subscription_capability_evidence import (
        SubscriptionCapabilityEvidenceService,
    )

    from apps.orchestrator.tests.application.test_antigravity_capability_publication import (
        identity,
        observations,
    )

    now = datetime.now(UTC)
    artifacts = FilesystemArtifactStore(tmp_path / "antigravity-artifacts")
    source = PostgresCapabilityEvidenceSource(session_factory, artifacts, clock=lambda: now)
    service = SubscriptionCapabilityEvidenceService(artifacts, source, clock=lambda: now)
    published = await service.publish(
        identity=identity(), observations=observations(), observed_at=now
    )
    restarted = PostgresCapabilityEvidenceSource(session_factory, artifacts, clock=lambda: now)
    assert await restarted.resolve(identity()) == published
    tool_proof = next(
        p for p in published.manifest.proofs if p.kind is CapabilityProofKind.TOOL_ISOLATION
    )
    wire = await artifacts.open_bytes(tool_proof.artifact_digest, max_bytes=16 * 1024)
    assert json.loads(wire)["payload"]["approved_tools_unproved"] is True

    # Even recomputed artifact/manifest digests cannot launder a paid setting.
    binding = next(
        p
        for p in published.manifest.proofs
        if p.kind is CapabilityProofKind.SUBSCRIPTION_ROUTE_BINDING
    )
    payload = json.loads(await artifacts.open_bytes(binding.artifact_digest, max_bytes=16 * 1024))
    payload["payload"]["effective_use_g1_credits"] = True
    changed = await artifacts.put_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        media_type="application/vnd.forge.client-capability-proof+json",
    )
    manifest = published.manifest.model_copy(
        update={
            "evidence_id": uuid4(),
            "proofs": tuple(
                p.model_copy(update={"artifact_digest": changed.digest}) if p == binding else p
                for p in published.manifest.proofs
            ),
        }
    )
    with pytest.raises(CapabilityEvidenceUnavailable):
        await source.publish(manifest)
    assert await restarted.resolve(identity()) == published


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
async def test_changed_replay_conflicts_when_persisted_manifest_artifact_is_missing(
    session_factory, tmp_path
) -> None:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    root = tmp_path / "artifacts"
    artifacts = FilesystemArtifactStore(root)
    source = PostgresCapabilityEvidenceSource(session_factory, artifacts, clock=lambda: now)
    manifest = await _manifest(now, artifacts=artifacts)
    published = await source.publish(manifest)

    manifest_path = root / canonical_storage_pointer(published.artifact_digest)
    manifest_path.unlink()

    changed = manifest.model_copy(update={"verifier_version": "2"})
    with pytest.raises(CapabilityEvidenceConflict, match="replay"):
        await source.publish(changed)


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
