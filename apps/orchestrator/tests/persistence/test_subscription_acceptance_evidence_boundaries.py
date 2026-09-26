"""Acceptance evidence must retain exact sources and preserve transactional gates."""

import asyncio
from uuid import UUID, uuid4

import pytest
from forge.application.ports.evidence import (
    EvidenceCorruptLineage,
    SubscriptionAcceptanceEvidenceDraft,
)
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.persistence.models import AgentExecution, ArtifactLineage, EvidenceSet, Run, Step
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionOperationBinding, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_evidence_persistence import acceptance_evidence_case


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "wrong_phase",
        "pause",
        "epoch",
        "result",
        "selection",
        "callback",
        "receipt_proof",
        "validation_tree",
        "validation_failed",
        "validation_output_missing",
        "validation_output_other",
        "validation_agent",
        "validation_producer_kind",
        "validation_producer_id",
        "receipt_producer",
    ],
)
async def test_acceptance_evidence_rejects_changed_sources(session_factory, tmp_path, change):
    factory, manifest, artifact = await acceptance_evidence_case(
        session_factory, tmp_path, validation_tree="f" * 64 if change == "validation_tree" else None
    )
    async with factory() as work:
        if change == "wrong_phase":
            row = await work.session.get(Run, manifest.run_id)
            row.state = RunState.IMPLEMENTING.value
        elif change == "pause":
            row = await work.session.get(SubscriptionTask, manifest.producer_task_id)
            row.pause_requested = True
        elif change == "epoch":
            row = await work.session.get(SubscriptionSchedulerRun, manifest.run_id)
            row.candidate_epoch += 1
        elif change in ("result", "selection", "receipt_proof"):
            row = await work.session.get(
                SubscriptionAttemptResult,
                manifest.selection_attempt_id
                if change == "selection"
                else manifest.producer_attempt_id,
            )
            if change == "result":
                row.result_digest = "f" * 64
            elif change == "selection":
                row.application_digest = "f" * 64
            else:
                proof = dict(row.application_payload["receipt_verification"])
                proof["result_digest"] = "f" * 64
                row.application_payload = {**row.application_payload, "receipt_verification": proof}
                row.application_digest = canonical_digest(row.application_payload)
        elif change == "callback":
            row = await work.session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id
                    == UUID(manifest.acceptance.evidence_receipt_ids[0])
                )
            )
            row.receipt_payload = {**row.receipt_payload, "accepted": False}
        elif change == "validation_tree":
            pass  # This immutable validation was originally recorded for different contents.
        elif change == "validation_failed":
            row = await work.session.get(Step, manifest.step_id)
            row.status = "FAILED"
        elif change in ("validation_output_missing", "validation_output_other"):
            row = await work.session.get(Step, manifest.step_id)
            row.output_artifact_id = (
                None if change == "validation_output_missing" else artifact.descriptor.artifact_id
            )
        elif change == "validation_agent":
            work.session.add(
                AgentExecution(
                    id=uuid4(),
                    run_id=manifest.run_id,
                    step_id=manifest.step_id,
                    role="reviewer",
                    instruction_version="1",
                    provider="test",
                    model="test-reviewer",
                    status="PENDING",
                )
            )
        else:
            digest = manifest.receipt_evidence_digest
            if change in ("validation_producer_kind", "validation_producer_id"):
                validation = await work.evidence.get_by_id(
                    manifest.validation_evidence_set_id, run_id=manifest.run_id
                )
                digest = validation.manifest_digest
            proof = await work.artifacts.get_by_digest(digest, run_id=manifest.run_id)
            row = await work.session.scalar(
                select(ArtifactLineage).where(
                    ArtifactLineage.artifact_id == proof.artifact_id,
                    ArtifactLineage.run_id == manifest.run_id,
                )
            )
            if change == "validation_producer_kind":
                row.producer_kind = "unrelated_producer"
            else:
                row.producer_id = uuid4()
        await work.commit()
    async with factory() as work:
        with pytest.raises(EvidenceCorruptLineage):
            await work.evidence.record_set(SubscriptionAcceptanceEvidenceDraft(manifest), artifact)
        assert await work.session.get(EvidenceSet, manifest.evidence_set_id) is None
        assert (await work.runs.get(manifest.run_id)).pending_gate is None


@pytest.mark.integration
async def test_acceptance_evidence_replay_is_atomic_and_historical(session_factory, tmp_path):
    factory, manifest, artifact = await acceptance_evidence_case(session_factory, tmp_path)
    draft = SubscriptionAcceptanceEvidenceDraft(manifest)
    async with factory() as work:
        await work.evidence.record_set(draft, artifact)
        # Exiting without commit must retain neither an evidence set nor projections.
    async with factory() as work:
        assert await work.session.get(EvidenceSet, manifest.evidence_set_id) is None

    async def record():
        async with factory() as work:
            result = await work.evidence.record_set(draft, artifact)
            await work.commit()
            return result

    first, second = await asyncio.gather(record(), record())
    assert first == second
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, manifest.run_id)
        scheduler.candidate_epoch += 1
        scheduler.candidate_state = "open"
        await work.commit()
    async with factory() as work:
        # Replaying this exact immutable ID is historical evidence, not current authority.
        assert await work.evidence.record_set(draft, artifact) == first
        assert (await work.runs.get(manifest.run_id)).pending_gate is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "change", ["producer_result_digest", "selection_result_digest", "selection"]
)
async def test_canonical_acceptance_cannot_substitute_different_claims(
    session_factory, tmp_path, change
):
    from dataclasses import replace

    from forge.application.ports.evidence import CanonicalEvidenceArtifact
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.domain.evidence import encode_evidence_manifest

    factory, manifest, artifact = await acceptance_evidence_case(session_factory, tmp_path)
    value = (
        replace(manifest.selection, no_review_reason="Different selection rationale")
        if change == "selection"
        else "f" * 64
    )
    manifest = manifest.model_copy(update={change: value})
    wire = encode_evidence_manifest(manifest)
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    descriptor = await store.put_bytes(wire, media_type=artifact.descriptor.media_type)
    async with factory() as work:
        descriptor = await work.artifacts.record(
            descriptor,
            run_id=manifest.run_id,
            producer_type="evidence_set",
            producer_id=manifest.evidence_set_id,
            parent_digests=artifact.descriptor.parent_digests,
        )
        with pytest.raises(EvidenceCorruptLineage):
            await work.evidence.record_set(
                SubscriptionAcceptanceEvidenceDraft(manifest),
                CanonicalEvidenceArtifact(descriptor, manifest, wire),
            )


@pytest.mark.integration
async def test_acceptance_producer_is_constrained_to_actual_run_task_attempt(
    session_factory, tmp_path
):
    factory, manifest, artifact = await acceptance_evidence_case(session_factory, tmp_path)
    async with factory() as work:
        # Bypass repository proof to discriminate the database's composite FK.
        work.session.add(
            EvidenceSet(
                id=manifest.evidence_set_id,
                run_id=manifest.run_id,
                step_id=manifest.step_id,
                kind="acceptance",
                policy_version=manifest.policy_version,
                head_sha=manifest.head_sha,
                candidate_tree_digest=manifest.candidate_tree_digest,
                producer_task_id=uuid4(),
                producer_attempt_id=manifest.producer_attempt_id,
                manifest_artifact_id=artifact.descriptor.artifact_id,
                validation_evidence_set_id=manifest.validation_evidence_set_id,
                validation_parent_policy_version=manifest.policy_version,
                validation_parent_kind="validation",
                validation_parent_head_sha=manifest.head_sha,
            )
        )
        with pytest.raises(IntegrityError, match="fk_evidence_sets_subscription_producer"):
            await work.session.flush()


@pytest.mark.integration
@pytest.mark.parametrize("record_acceptance", [False, True])
async def test_downgrade_refuses_retained_candidate_evidence(
    session_factory, tmp_path, record_acceptance
):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    factory, manifest, artifact = await acceptance_evidence_case(session_factory, tmp_path)
    if record_acceptance:
        async with factory() as work:
            await work.evidence.record_set(SubscriptionAcceptanceEvidenceDraft(manifest), artifact)
            await work.commit()
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260911_0018_subscription_acceptance_evidence.py"
    )
    spec = importlib.util.spec_from_file_location("acceptance_evidence_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def downgrade(session):
        with Operations.context(MigrationContext.configure(session.connection())):
            migration.downgrade()

    async with factory() as work:
        with pytest.raises(RuntimeError, match="must not be discarded"):
            await work.session.run_sync(downgrade)
        parent = await work.session.get(EvidenceSet, manifest.validation_evidence_set_id)
        assert parent.candidate_tree_digest == manifest.candidate_tree_digest
        assert (
            await work.session.get(EvidenceSet, manifest.evidence_set_id) is not None
        ) == record_acceptance
