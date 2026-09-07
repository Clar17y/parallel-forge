"""Adversarial PostgreSQL contracts for immutable evidence persistence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceConflict,
    EvidenceCorruptLineage,
    EvidenceInputPurpose,
    EvidenceReadScope,
    ReviewEvidenceDraft,
    ValidationEvidenceDraft,
    ValidationProjectionMember,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.evidence import (
    EvidenceStatus,
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    ValidationEvidenceMember,
    encode_evidence_manifest,
)
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.persistence.models import (
    AgentExecution,
    EvidenceSet,
    Review,
    Run,
    Step,
    ValidationResult,
)
from forge.persistence.repositories.evidence import PostgresEvidenceRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

_HEAD = "a" * 40
_MEDIA_TYPE = "application/vnd.forge.evidence-manifest+json"
_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


async def _step(
    work: PostgresUnitOfWork, run_id: UUID, kind: str, *, status: str = "SUCCEEDED"
) -> UUID:
    step_id = uuid4()
    prior_attempt = await work.session.scalar(
        select(func.max(Step.attempt)).where(Step.run_id == run_id, Step.kind == kind)
    )
    work.session.add(
        Step(
            id=step_id,
            run_id=run_id,
            kind=kind,
            attempt=(prior_attempt or 0) + 1,
            status=status,
        )
    )
    await work.session.flush()
    return step_id


async def _reviewer(
    work: PostgresUnitOfWork,
    run_id: UUID,
    step_id: UUID,
    *,
    status: str = "SUCCEEDED",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> UUID:
    execution_id = uuid4()
    work.session.add(
        AgentExecution(
            id=execution_id,
            run_id=run_id,
            step_id=step_id,
            role="reviewer",
            instruction_version="1",
            provider="test",
            model="test-reviewer",
            status=status,
            started_at=started_at,
            completed_at=completed_at,
        )
    )
    await work.session.flush()
    return execution_id


async def _blob(work: PostgresUnitOfWork, store: FilesystemArtifactStore, run_id: UUID, data: bytes):
    stored = await store.put_bytes(data, media_type="text/plain")
    return await work.artifacts.record(stored, run_id=run_id, producer_type="test")


async def _validation(
    work: PostgresUnitOfWork,
    store: FilesystemArtifactStore,
    run_id: UUID,
    step_id: UUID,
    *,
    prior_review_id: UUID | None = None,
    prior_review_digest: str | None = None,
    member: ValidationEvidenceMember | None = None,
    output_artifact_id: UUID | None = None,
):
    manifest = ValidationEvidenceManifest(
        evidence_set_id=uuid4(),
        run_id=run_id,
        step_id=step_id,
        policy_version=1,
        head_sha=_HEAD,
        prior_review_evidence_set_id=prior_review_id,
        members=() if member is None else (member,),
    )
    wire = encode_evidence_manifest(manifest)
    stored = await store.put_bytes(wire, media_type=_MEDIA_TYPE)
    parents = {
        digest
        for item in manifest.members
        for digest in (
            item.command_result_digest,
            item.stdout_digest,
            item.stderr_digest,
        )
    }
    if prior_review_digest is not None:
        parents.add(prior_review_digest)
    descriptor = await work.artifacts.record(
        stored,
        run_id=run_id,
        producer_type="evidence_set",
        producer_id=manifest.evidence_set_id,
        parent_digests=tuple(sorted(parents)),
    )
    members = (
        ()
        if member is None
        else (ValidationProjectionMember(member, output_artifact_id or UUID(int=1)),)
    )
    return ValidationEvidenceDraft(manifest, members), CanonicalEvidenceArtifact(
        descriptor, manifest, wire
    )


async def _review(
    work: PostgresUnitOfWork,
    store: FilesystemArtifactStore,
    run_id: UUID,
    step_id: UUID,
    producer_id: UUID,
    validation_id: UUID,
    validation_digest: str,
    finding: ReviewFinding,
):
    manifest = ReviewEvidenceManifest(
        evidence_set_id=uuid4(),
        run_id=run_id,
        step_id=step_id,
        policy_version=1,
        head_sha=_HEAD,
        producer_execution_id=producer_id,
        validation_evidence_set_id=validation_id,
        review=ReviewOutput(
            decision=(
                ReviewDecision.APPROVE
                if finding.resolved_at is not None
                else ReviewDecision.REQUEST_CHANGES
            ),
            findings=(finding,),
            tested_claims=("validated",),
            missing_evidence=(),
            summary="reviewed",
        ),
    )
    wire = encode_evidence_manifest(manifest)
    stored = await store.put_bytes(wire, media_type=_MEDIA_TYPE)
    descriptor = await work.artifacts.record(
        stored,
        run_id=run_id,
        producer_type="evidence_set",
        producer_id=manifest.evidence_set_id,
        parent_digests=(validation_digest,),
    )
    return ReviewEvidenceDraft(manifest), CanonicalEvidenceArtifact(descriptor, manifest, wire)


def _finding(summary: str, *, resolved: bool = False) -> ReviewFinding:
    return ReviewFinding(
        finding_id="stable-finding",
        severity=FindingSeverity.MAJOR,
        path="src/module.py",
        start_line=7,
        summary=summary,
        evidence="reproducible evidence",
        proposed_resolution="repair it",
        resolved_at=_NOW if resolved else None,
    )


@pytest.mark.integration
async def test_record_validation_rejects_descriptor_parent_and_output_artifact_mismatch(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        step_id = await _step(work, persisted_run.id, "validation")
        output = await _blob(work, store, persisted_run.id, b"expected")
        other = await _blob(work, store, persisted_run.id, b"other")
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
            started_at=_NOW,
            completed_at=_NOW,
        )
        draft, artifact = await _validation(
            work,
            store,
            persisted_run.id,
            step_id,
            member=member,
            output_artifact_id=output.artifact_id,
        )

        with pytest.raises(EvidenceCorruptLineage):
            await work.evidence.record_set(
                draft,
                CanonicalEvidenceArtifact(
                    replace(artifact.descriptor, parent_digests=()),
                    artifact.manifest,
                    artifact.canonical_bytes,
                ),
            )
        with pytest.raises(EvidenceCorruptLineage):
            await work.evidence.record_set(
                replace(
                    draft,
                    members=(ValidationProjectionMember(member, other.artifact_id),),
                ),
                artifact,
            )
        assert await work.session.get(EvidenceSet, draft.manifest.evidence_set_id) is None


@pytest.mark.integration
async def test_sibling_review_cannot_overwrite_existing_finding_projection(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        validation_step = await _step(work, persisted_run.id, "validation")
        validation, validation_artifact = await _validation(
            work, store, persisted_run.id, validation_step
        )
        validation_set = await work.evidence.record_set(validation, validation_artifact)

        first_step = await _step(work, persisted_run.id, "review")
        first_producer = await _reviewer(work, persisted_run.id, first_step)
        first, first_artifact = await _review(
            work,
            store,
            persisted_run.id,
            first_step,
            first_producer,
            validation_set.evidence_set_id,
            validation_set.manifest_digest,
            _finding("original finding"),
        )
        await work.evidence.record_set(first, first_artifact)
        await work.commit()

    async with PostgresUnitOfWork(session_factory) as work:
        sibling_validation_step = await _step(work, persisted_run.id, "validation")
        sibling_validation, sibling_validation_artifact = await _validation(
            work, store, persisted_run.id, sibling_validation_step
        )
        sibling_set = await work.evidence.record_set(
            sibling_validation, sibling_validation_artifact
        )
        sibling_step = await _step(work, persisted_run.id, "review")
        sibling_producer = await _reviewer(work, persisted_run.id, sibling_step)
        sibling, sibling_artifact = await _review(
            work,
            store,
            persisted_run.id,
            sibling_step,
            sibling_producer,
            sibling_set.evidence_set_id,
            sibling_set.manifest_digest,
            _finding("sibling overwrite"),
        )
        with pytest.raises(EvidenceConflict):
            await work.evidence.record_set(sibling, sibling_artifact)

    async with session_factory() as session:
        row = (
            await session.execute(
                select(Review).where(
                    Review.run_id == persisted_run.id,
                    Review.finding_id == "stable-finding",
                )
            )
        ).scalar_one()
        assert row.summary == "original finding"
        assert row.reviewer_execution_id == first_producer
        assert await session.get(EvidenceSet, sibling.manifest.evidence_set_id) is None


@pytest.mark.integration
async def test_same_id_replay_after_newer_causal_review_does_not_reapply_old_projection(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        validation_step = await _step(work, persisted_run.id, "validation")
        validation1, validation_artifact1 = await _validation(
            work, store, persisted_run.id, validation_step
        )
        validation_set1 = await work.evidence.record_set(validation1, validation_artifact1)
        review_step1 = await _step(work, persisted_run.id, "review")
        producer1 = await _reviewer(work, persisted_run.id, review_step1)
        review1, review_artifact1 = await _review(
            work,
            store,
            persisted_run.id,
            review_step1,
            producer1,
            validation_set1.evidence_set_id,
            validation_set1.manifest_digest,
            _finding("open finding"),
        )
        review_set1 = await work.evidence.record_set(review1, review_artifact1)

        validation_step2 = await _step(work, persisted_run.id, "validation")
        validation2, validation_artifact2 = await _validation(
            work,
            store,
            persisted_run.id,
            validation_step2,
            prior_review_id=review_set1.evidence_set_id,
            prior_review_digest=review_set1.manifest_digest,
        )
        validation_set2 = await work.evidence.record_set(validation2, validation_artifact2)
        review_step2 = await _step(work, persisted_run.id, "review")
        producer2 = await _reviewer(work, persisted_run.id, review_step2)
        review2, review_artifact2 = await _review(
            work,
            store,
            persisted_run.id,
            review_step2,
            producer2,
            validation_set2.evidence_set_id,
            validation_set2.manifest_digest,
            _finding("resolved finding", resolved=True),
        )
        await work.evidence.record_set(review2, review_artifact2)

        replayed = await work.evidence.record_set(review1, review_artifact1)
        assert replayed.evidence_set_id == review_set1.evidence_set_id
        await work.commit()

    async with session_factory() as session:
        row = (
            await session.execute(
                select(Review).where(
                    Review.run_id == persisted_run.id,
                    Review.finding_id == "stable-finding",
                )
            )
        ).scalar_one()
        assert row.status == "RESOLVED"
        assert row.summary == "resolved finding"
        assert row.reviewer_execution_id == producer2
        assert await session.scalar(
            select(func.count())
            .select_from(EvidenceSet)
            .where(EvidenceSet.run_id == persisted_run.id)
        ) == 4


@pytest.mark.integration
async def test_failed_projection_rolls_back_evidence_set_and_partial_validation_rows(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    result_id = uuid4()
    evidence_set_id: UUID
    async with PostgresUnitOfWork(session_factory) as work:
        step_id = await _step(work, persisted_run.id, "validation")
        output = await _blob(work, store, persisted_run.id, b"result")
        work.session.add(
            ValidationResult(
                id=result_id,
                run_id=persisted_run.id,
                step_id=step_id,
                check_name="existing",
                command_name="existing",
                command_version=1,
                status="PASSED",
                exit_code=0,
                output_artifact_id=output.artifact_id,
                started_at=_NOW,
                completed_at=_NOW,
            )
        )
        await work.commit()

    async with PostgresUnitOfWork(session_factory) as work:
        step_id = await _step(work, persisted_run.id, "validation")
        output = await work.artifacts.get_by_digest(output.digest, run_id=persisted_run.id)
        member = ValidationEvidenceMember(
            result_id=result_id,
            check_name="collision",
            command_name="pytest",
            command_version=1,
            command_digest="2" * 64,
            command_result_digest=output.digest,
            stdout_digest=output.digest,
            stderr_digest=output.digest,
            status=EvidenceStatus.PASSED,
            exit_code=0,
            started_at=_NOW,
            completed_at=_NOW,
        )
        draft, artifact = await _validation(
            work,
            store,
            persisted_run.id,
            step_id,
            member=member,
            output_artifact_id=output.artifact_id,
        )
        evidence_set_id = draft.manifest.evidence_set_id
        with pytest.raises(EvidenceConflict):
            await work.evidence.record_set(draft, artifact)

    async with session_factory() as session:
        assert await session.get(EvidenceSet, evidence_set_id) is None
        assert await session.scalar(
            select(func.count())
            .select_from(ValidationResult)
            .where(ValidationResult.run_id == persisted_run.id)
        ) == 1


@pytest.mark.integration
async def test_reader_rejects_consumer_step_that_is_not_running(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        validation_step = await _step(work, persisted_run.id, "validation")
        draft, artifact = await _validation(
            work, store, persisted_run.id, validation_step
        )
        evidence = await work.evidence.record_set(draft, artifact)
        consumer_step = await _step(work, persisted_run.id, "review")
        consumer_id = await _reviewer(
            work, persisted_run.id, consumer_step, status="PENDING"
        )
        await work.evidence.bind_input(
            consumer_id,
            EvidenceInputPurpose.VALIDATION_RESULTS,
            evidence.evidence_set_id,
            run_id=persisted_run.id,
        )
        consumer = await work.session.get(AgentExecution, consumer_id)
        assert consumer is not None
        consumer.status = "RUNNING"
        consumer.started_at = _NOW
        await work.session.flush()

        with pytest.raises(EvidenceCorruptLineage):
            await work.evidence.input_for_execution(
                EvidenceInputPurpose.VALIDATION_RESULTS,
                EvidenceReadScope(
                    persisted_run.id, 1, consumer_id, consumer_step, _HEAD
                ),
            )


@pytest.mark.integration
async def test_reader_rejects_validation_whose_producer_step_is_not_succeeded(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        validation_step = await _step(work, persisted_run.id, "validation")
        draft, artifact = await _validation(
            work, store, persisted_run.id, validation_step
        )
        evidence = await work.evidence.record_set(draft, artifact)
        consumer_step = await _step(
            work, persisted_run.id, "review", status="RUNNING"
        )
        consumer_id = await _reviewer(
            work,
            persisted_run.id,
            consumer_step,
            status="PENDING",
            started_at=_NOW,
        )
        await work.evidence.bind_input(
            consumer_id,
            EvidenceInputPurpose.VALIDATION_RESULTS,
            evidence.evidence_set_id,
            run_id=persisted_run.id,
        )
        consumer = await work.session.get(AgentExecution, consumer_id)
        producer_step = await work.session.get(Step, validation_step)
        assert consumer is not None and producer_step is not None
        consumer.status = "RUNNING"
        producer_step.status = "RUNNING"
        await work.session.flush()

        with pytest.raises(EvidenceCorruptLineage):
            await work.evidence.input_for_execution(
                EvidenceInputPurpose.VALIDATION_RESULTS,
                EvidenceReadScope(
                    persisted_run.id, 1, consumer_id, consumer_step, _HEAD
                ),
            )


@pytest.mark.integration
async def test_reader_returns_exact_bound_descriptors_for_both_input_purposes(
    tmp_path, session_factory, persisted_run
) -> None:
    """Eligible running reviewers receive the immutable descriptor they bound."""

    store = FilesystemArtifactStore(tmp_path)
    consumer_started_at = _NOW + timedelta(hours=1)
    async with PostgresUnitOfWork(session_factory) as work:
        first_validation_step = await _step(work, persisted_run.id, "validation")
        first_validation, first_validation_artifact = await _validation(
            work, store, persisted_run.id, first_validation_step
        )
        first_validation_set = await work.evidence.record_set(
            first_validation, first_validation_artifact
        )
        review_step = await _step(work, persisted_run.id, "review")
        producer_id = await _reviewer(
            work,
            persisted_run.id,
            review_step,
            started_at=_NOW,
            completed_at=_NOW,
        )
        review, review_artifact = await _review(
            work,
            store,
            persisted_run.id,
            review_step,
            producer_id,
            first_validation_set.evidence_set_id,
            first_validation_set.manifest_digest,
            _finding("prior finding"),
        )
        review_set = await work.evidence.record_set(review, review_artifact)
        validation_step = await _step(work, persisted_run.id, "validation")
        validation, validation_artifact = await _validation(
            work,
            store,
            persisted_run.id,
            validation_step,
            prior_review_id=review_set.evidence_set_id,
            prior_review_digest=review_set.manifest_digest,
        )
        validation_set = await work.evidence.record_set(validation, validation_artifact)
        consumer_step = await _step(work, persisted_run.id, "review", status="RUNNING")
        consumer_id = await _reviewer(
            work,
            persisted_run.id,
            consumer_step,
            status="PENDING",
            started_at=consumer_started_at,
        )
        await work.evidence.bind_input(
            consumer_id,
            EvidenceInputPurpose.VALIDATION_RESULTS,
            validation_set.evidence_set_id,
            run_id=persisted_run.id,
        )
        await work.evidence.bind_input(
            consumer_id,
            EvidenceInputPurpose.PRIOR_REVIEW,
            review_set.evidence_set_id,
            run_id=persisted_run.id,
        )
        consumer = await work.session.get(AgentExecution, consumer_id)
        assert consumer is not None
        consumer.status = "RUNNING"
        await work.session.flush()
        scope = EvidenceReadScope(persisted_run.id, 1, consumer_id, consumer_step, _HEAD)

        validation_descriptor = await work.evidence.input_for_execution(
            EvidenceInputPurpose.VALIDATION_RESULTS, scope
        )
        prior_review_descriptor = await work.evidence.input_for_execution(
            EvidenceInputPurpose.PRIOR_REVIEW, scope
        )

        assert validation_descriptor == validation_set
        assert prior_review_descriptor == review_set


@pytest.mark.integration
async def test_evidence_retention_restricts_run_deletion(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        step_id = await _step(work, persisted_run.id, "validation")
        draft, artifact = await _validation(work, store, persisted_run.id, step_id)
        await work.evidence.record_set(draft, artifact)
        await work.commit()

    async with session_factory() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.delete(await session.get_one(Run, persisted_run.id))
            await session.flush()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("producer_status", "producer_completed_at", "scope_policy", "scope_head"),
    [
        pytest.param("FAILED", _NOW, 1, _HEAD, id="producer-not-succeeded"),
        pytest.param("SUCCEEDED", None, 1, _HEAD, id="producer-missing-completion"),
        pytest.param(
            "SUCCEEDED",
            _NOW + timedelta(hours=2),
            1,
            _HEAD,
            id="producer-completed-after-consumer-start",
        ),
        pytest.param("SUCCEEDED", _NOW, 1, "b" * 40, id="validation-head-mismatch"),
        pytest.param("SUCCEEDED", _NOW, 2, _HEAD, id="validation-policy-mismatch"),
    ],
)
async def test_prior_review_reader_rechecks_causal_validation_and_producer_eligibility(
    tmp_path,
    session_factory,
    persisted_run,
    producer_status,
    producer_completed_at,
    scope_policy,
    scope_head,
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    consumer_started_at = _NOW + timedelta(hours=1)
    async with PostgresUnitOfWork(session_factory) as work:
        validation_step1 = await _step(work, persisted_run.id, "validation")
        validation1, validation_artifact1 = await _validation(
            work, store, persisted_run.id, validation_step1
        )
        validation_set1 = await work.evidence.record_set(
            validation1, validation_artifact1
        )
        review_step = await _step(work, persisted_run.id, "review")
        producer_id = await _reviewer(
            work,
            persisted_run.id,
            review_step,
            status=producer_status,
            started_at=_NOW - timedelta(minutes=5),
            completed_at=producer_completed_at,
        )
        review, review_artifact = await _review(
            work,
            store,
            persisted_run.id,
            review_step,
            producer_id,
            validation_set1.evidence_set_id,
            validation_set1.manifest_digest,
            _finding("prior finding"),
        )
        review_set = await work.evidence.record_set(review, review_artifact)
        validation_step2 = await _step(work, persisted_run.id, "validation")
        validation2, validation_artifact2 = await _validation(
            work,
            store,
            persisted_run.id,
            validation_step2,
            prior_review_id=review_set.evidence_set_id,
            prior_review_digest=review_set.manifest_digest,
        )
        validation_set2 = await work.evidence.record_set(
            validation2, validation_artifact2
        )
        consumer_step = await _step(
            work, persisted_run.id, "review", status="RUNNING"
        )
        consumer_id = await _reviewer(
            work,
            persisted_run.id,
            consumer_step,
            status="PENDING",
            started_at=consumer_started_at,
        )
        await work.evidence.bind_input(
            consumer_id,
            EvidenceInputPurpose.VALIDATION_RESULTS,
            validation_set2.evidence_set_id,
            run_id=persisted_run.id,
        )
        await work.evidence.bind_input(
            consumer_id,
            EvidenceInputPurpose.PRIOR_REVIEW,
            review_set.evidence_set_id,
            run_id=persisted_run.id,
        )
        consumer = await work.session.get(AgentExecution, consumer_id)
        assert consumer is not None
        consumer.status = "RUNNING"
        await work.session.flush()

        with pytest.raises(EvidenceCorruptLineage):
            await work.evidence.input_for_execution(
                EvidenceInputPurpose.PRIOR_REVIEW,
                EvidenceReadScope(
                    persisted_run.id,
                    scope_policy,
                    consumer_id,
                    consumer_step,
                    scope_head,
                ),
            )


class _BlockingArtifactRepository:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_by_digest(self, digest: str, *, run_id: UUID):
        self.entered.set()
        await self.release.wait()
        return await self.delegate.get_by_digest(digest, run_id=run_id)


@pytest.mark.integration
async def test_initial_insert_projects_snapshot_taken_before_caller_mutates_draft_collection(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        step_id = await _step(work, persisted_run.id, "validation")
        output = await _blob(work, store, persisted_run.id, b"snapshot")
        original = ValidationEvidenceMember(
            result_id=uuid4(),
            check_name="original",
            command_name="pytest",
            command_version=1,
            command_digest="3" * 64,
            command_result_digest=output.digest,
            stdout_digest=output.digest,
            stderr_digest=output.digest,
            status=EvidenceStatus.PASSED,
            exit_code=0,
            started_at=_NOW,
            completed_at=_NOW,
        )
        replacement = original.model_copy(
            update={"result_id": uuid4(), "check_name": "replacement"}
        )
        manifest = ValidationEvidenceManifest(
            evidence_set_id=uuid4(),
            run_id=persisted_run.id,
            step_id=step_id,
            policy_version=1,
            head_sha=_HEAD,
            members=(original,),
        )
        wire = encode_evidence_manifest(manifest)
        stored = await store.put_bytes(wire, media_type=_MEDIA_TYPE)
        descriptor = await work.artifacts.record(
            stored,
            run_id=persisted_run.id,
            producer_type="evidence_set",
            producer_id=manifest.evidence_set_id,
            parent_digests=(output.digest,),
        )
        caller_members = [ValidationProjectionMember(original, output.artifact_id)]
        draft = ValidationEvidenceDraft(manifest, caller_members)  # type: ignore[arg-type]
        artifact = CanonicalEvidenceArtifact(descriptor, manifest, wire)
        blocking = _BlockingArtifactRepository(work.artifacts)
        repository = PostgresEvidenceRepository(work.session, artifacts=blocking)

        recording = asyncio.create_task(repository.record_set(draft, artifact))
        await asyncio.wait_for(blocking.entered.wait(), timeout=2)
        caller_members[:] = [ValidationProjectionMember(replacement, output.artifact_id)]
        blocking.release.set()
        await asyncio.wait_for(recording, timeout=2)
        await work.commit()

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ValidationResult).where(
                    ValidationResult.run_id == persisted_run.id
                )
            )
        ).scalars().all()
        assert [(row.id, row.check_name) for row in rows] == [
            (original.result_id, "original")
        ]
