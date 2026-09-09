"""PostgreSQL contract for atomically binding Reviewer inputs before start."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceInputPurpose,
    EvidenceReadScope,
    ReviewEvidenceDraft,
    ValidationEvidenceDraft,
)
from forge.application.ports.executions import ReviewerEvidenceBinding
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.evidence import (
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    encode_evidence_manifest,
)
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.domain.run import RunSnapshot
from forge.persistence.models import AgentExecution, AgentExecutionEvidenceInput, RunEvent, Step
from forge.persistence.repositories.executions import ExecutionConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, select

_HEAD = "a" * 40
_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
_MEDIA_TYPE = "application/vnd.forge.evidence-manifest+json"


async def _step(work: PostgresUnitOfWork, run_id: UUID, kind: str) -> UUID:
    step_id = uuid4()
    attempt = await work.session.scalar(
        select(func.max(Step.attempt)).where(Step.run_id == run_id, Step.kind == kind)
    )
    work.session.add(
        Step(id=step_id, run_id=run_id, kind=kind, attempt=(attempt or 0) + 1, status="SUCCEEDED")
    )
    await work.session.flush()
    return step_id


async def _validation(
    work: PostgresUnitOfWork,
    store: FilesystemArtifactStore,
    run_id: UUID,
    step_id: UUID,
    prior_id: UUID | None = None,
    prior_digest: str | None = None,
):
    manifest = ValidationEvidenceManifest(
        evidence_set_id=uuid4(),
        run_id=run_id,
        step_id=step_id,
        policy_version=1,
        head_sha=_HEAD,
        prior_review_evidence_set_id=prior_id,
        members=(),
    )
    wire = encode_evidence_manifest(manifest)
    stored = await store.put_bytes(wire, media_type=_MEDIA_TYPE)
    descriptor = await work.artifacts.record(
        stored,
        run_id=run_id,
        producer_type="evidence_set",
        producer_id=manifest.evidence_set_id,
        parent_digests=() if prior_digest is None else (prior_digest,),
    )
    return await work.evidence.record_set(
        ValidationEvidenceDraft(manifest, ()), CanonicalEvidenceArtifact(descriptor, manifest, wire)
    )


async def _review(
    work: PostgresUnitOfWork,
    store: FilesystemArtifactStore,
    run_id: UUID,
    validation_id: UUID,
    validation_digest: str,
) -> object:
    step_id = await _step(work, run_id, "review")
    producer_id = uuid4()
    work.session.add(
        AgentExecution(
            id=producer_id,
            run_id=run_id,
            step_id=step_id,
            role="reviewer",
            instruction_version="1",
            provider="test",
            model="test",
            status="SUCCEEDED",
            started_at=_NOW,
            completed_at=_NOW,
        )
    )
    finding = ReviewFinding(
        finding_id="finding",
        severity=FindingSeverity.MAJOR,
        path="src/module.py",
        start_line=1,
        summary="finding",
        evidence="evidence",
        proposed_resolution="fix",
    )
    manifest = ReviewEvidenceManifest(
        evidence_set_id=uuid4(),
        run_id=run_id,
        step_id=step_id,
        policy_version=1,
        head_sha=_HEAD,
        producer_execution_id=producer_id,
        validation_evidence_set_id=validation_id,
        review=ReviewOutput(
            decision=ReviewDecision.REQUEST_CHANGES,
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
    return await work.evidence.record_set(
        ReviewEvidenceDraft(manifest), CanonicalEvidenceArtifact(descriptor, manifest, wire)
    )


async def _causal_sets(work: PostgresUnitOfWork, store: FilesystemArtifactStore, run_id: UUID):
    first_validation = await _validation(
        work, store, run_id, await _step(work, run_id, "validation")
    )
    prior_review = await _review(
        work,
        store,
        run_id,
        first_validation.evidence_set_id,
        first_validation.manifest_digest,
    )
    validation = await _validation(
        work,
        store,
        run_id,
        await _step(work, run_id, "validation"),
        prior_review.evidence_set_id,
        prior_review.manifest_digest,
    )
    return first_validation, prior_review, validation


@pytest.mark.integration
async def test_reviewer_admission_binds_causal_inputs_before_running(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        first_validation, prior_review, validation = await _causal_sets(
            work, store, persisted_run.id
        )
        step_id, execution_id = uuid4(), uuid4()
        binding = ReviewerEvidenceBinding(
            validation_evidence_set_id=validation.evidence_set_id,
            prior_review_evidence_set_id=prior_review.evidence_set_id,
            policy_version=1,
            head_sha=_HEAD,
        )
        admitted = await work.executions.admit(
            persisted_run.id,
            step_id,
            execution_id,
            "review",
            2,
            AgentRole.REVIEWER,
            "1",
            "test",
            "reviewer",
            admitted_at=_NOW,
            reviewer_input=binding,
        )
        assert admitted.is_new and admitted.status.value == "RUNNING"
        scope = EvidenceReadScope(persisted_run.id, 1, execution_id, step_id, _HEAD)
        assert (
            await work.evidence.input_for_execution(EvidenceInputPurpose.VALIDATION_RESULTS, scope)
            == validation
        )
        assert (
            await work.evidence.input_for_execution(EvidenceInputPurpose.PRIOR_REVIEW, scope)
            == prior_review
        )
        replay = await work.executions.admit(
            persisted_run.id,
            step_id,
            execution_id,
            "review",
            2,
            AgentRole.REVIEWER,
            "1",
            "test",
            "reviewer",
            admitted_at=_NOW,
            reviewer_input=binding,
        )
        assert not replay.is_new
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(
                persisted_run.id,
                step_id,
                execution_id,
                "review",
                2,
                AgentRole.REVIEWER,
                "1",
                "test",
                "reviewer",
                admitted_at=_NOW,
                reviewer_input=ReviewerEvidenceBinding(
                    validation_evidence_set_id=first_validation.evidence_set_id,
                    prior_review_evidence_set_id=None,
                    policy_version=1,
                    head_sha=_HEAD,
                ),
            )
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == persisted_run.id,
                    RunEvent.event_type == "agent_execution.admitted",
                )
            )
            == 1
        )
        await work.commit()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("policy_version", "head_sha", "prior_review_id"),
    [
        pytest.param(2, _HEAD, "bound", id="policy"),
        pytest.param(1, "b" * 40, "bound", id="head"),
        pytest.param(1, _HEAD, None, id="causal-parent"),
    ],
)
async def test_invalid_reviewer_binding_never_inserts_pending_rows(
    tmp_path, session_factory, persisted_run, policy_version, head_sha, prior_review_id
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    step_id, execution_id = uuid4(), uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        _first, prior_review, validation = await _causal_sets(work, store, persisted_run.id)
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(
                persisted_run.id,
                step_id,
                execution_id,
                "review",
                2,
                AgentRole.REVIEWER,
                "1",
                "test",
                "reviewer",
                reviewer_input=ReviewerEvidenceBinding(
                    validation_evidence_set_id=validation.evidence_set_id,
                    prior_review_evidence_set_id=(
                        prior_review.evidence_set_id if prior_review_id == "bound" else None
                    ),
                    policy_version=policy_version,
                    head_sha=head_sha,
                ),
            )
        assert await work.session.get(Step, step_id) is None
        assert await work.session.get(AgentExecution, execution_id) is None


@pytest.mark.integration
async def test_cross_run_reviewer_evidence_is_rejected_before_admission(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    other_run = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=1,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(other_run)
        await work.commit()
    step_id, execution_id = uuid4(), uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        _first, prior_review, validation = await _causal_sets(work, store, persisted_run.id)
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(
                other_run.id,
                step_id,
                execution_id,
                "review",
                1,
                AgentRole.REVIEWER,
                "1",
                "test",
                "reviewer",
                reviewer_input=ReviewerEvidenceBinding(
                    validation_evidence_set_id=validation.evidence_set_id,
                    prior_review_evidence_set_id=prior_review.evidence_set_id,
                    policy_version=1,
                    head_sha=_HEAD,
                ),
            )
        assert await work.session.get(Step, step_id) is None
        assert await work.session.get(AgentExecution, execution_id) is None


@pytest.mark.integration
async def test_reviewer_input_is_rejected_for_other_roles_and_legacy_admission_stays_running(
    session_factory, persisted_run
) -> None:
    binding = ReviewerEvidenceBinding(
        validation_evidence_set_id=uuid4(),
        prior_review_evidence_set_id=None,
        policy_version=1,
        head_sha=_HEAD,
    )
    rejected_step, rejected_execution = uuid4(), uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(ExecutionConflict):
            await work.executions.admit(
                persisted_run.id,
                rejected_step,
                rejected_execution,
                "plan",
                1,
                AgentRole.PLANNER,
                "1",
                "test",
                "planner",
                reviewer_input=binding,
            )
        admitted = await work.executions.admit(
            persisted_run.id,
            uuid4(),
            uuid4(),
            "plan",
            1,
            AgentRole.PLANNER,
            "1",
            "test",
            "planner",
        )
        assert admitted.is_new and admitted.status.value == "RUNNING"
        legacy_reviewer = await work.executions.admit(
            persisted_run.id,
            uuid4(),
            uuid4(),
            "review",
            1,
            AgentRole.REVIEWER,
            "1",
            "test",
            "reviewer",
        )
        assert legacy_reviewer.is_new and legacy_reviewer.status.value == "RUNNING"
        assert await work.session.get(Step, rejected_step) is None
        assert await work.session.get(AgentExecution, rejected_execution) is None


@pytest.mark.integration
async def test_concurrent_reviewer_admission_replays_one_bound_start(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        _first, prior_review, validation = await _causal_sets(work, store, persisted_run.id)
        await work.commit()
    binding = ReviewerEvidenceBinding(
        validation_evidence_set_id=validation.evidence_set_id,
        prior_review_evidence_set_id=prior_review.evidence_set_id,
        policy_version=1,
        head_sha=_HEAD,
    )
    step_id, execution_id = uuid4(), uuid4()

    async def admit_once():
        async with PostgresUnitOfWork(session_factory) as work:
            admission = await work.executions.admit(
                persisted_run.id,
                step_id,
                execution_id,
                "review",
                2,
                AgentRole.REVIEWER,
                "1",
                "test",
                "reviewer",
                admitted_at=_NOW,
                reviewer_input=binding,
            )
            await work.commit()
            return admission

    first, second = await asyncio.gather(admit_once(), admit_once())
    assert {first.is_new, second.is_new} == {False, True}
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == persisted_run.id,
                    RunEvent.event_type == "agent_execution.admitted",
                )
            )
            == 1
        )
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(AgentExecutionEvidenceInput)
                .where(AgentExecutionEvidenceInput.consumer_execution_id == execution_id)
            )
            == 2
        )
