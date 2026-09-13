"""Acceptance evidence re-proves actual prepared sources during final validation."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceKind,
    SubscriptionAcceptanceEvidenceDraft,
    ValidationEvidenceDraft,
)
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.evidence import (
    SubscriptionAcceptanceEvidenceManifest,
    ValidationEvidenceManifest,
    encode_evidence_manifest,
)
from forge.domain.run import RunState
from forge.persistence.models import AgentExecution, Run
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_receipt_sources import receipt_case


async def acceptance_evidence_case(
    session_factory, tmp_path, *, validation_tree=None, reviewed=False
):
    from test_subscription_acceptance_preparation import acceptance_case

    factory, proposal, _, _, data = await receipt_case(
        session_factory,
        tmp_path,
        acceptance_factory=reviewed_acceptance_case if reviewed else acceptance_case,
    )
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    await store.put_bytes(data, media_type="application/json")

    async def snapshot(current):
        return GitWorkingTreeSnapshot(
            head_sha=current.review.candidate.head_sha,
            base_sha=current.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    await SubscriptionAcceptanceInspection(factory, snapshot).inspect(proposal.attempt_id)
    proof = await SubscriptionAcceptanceReceiptVerification(
        factory, store, SimpleNamespace()
    ).verify(proposal.attempt_id)
    assert proof is not None
    proof_wire = json.dumps(
        proof.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    proof_descriptor = await store.put_bytes(proof_wire, media_type="application/json")
    async with factory() as work:
        proposal = await work.subscription_decisions.acceptance_proposal(proposal.attempt_id)
        proof_descriptor = await work.artifacts.record(
            proof_descriptor,
            run_id=proposal.decision.run_id,
            producer_type="subscription_acceptance_receipts",
            producer_id=proposal.attempt_id,
            parent_digests=tuple(sorted(digest for digest, _ in proof.artifact_proofs)),
        )
        run = await work.session.get(Run, proposal.decision.run_id)
        run.state = RunState.VALIDATING.value
        run.version += 1
        step_id = uuid4()
        await work.controller_steps.admit(run.id, step_id, "validate", 1)
        validation = ValidationEvidenceManifest(
            schema_version=2,
            evidence_set_id=uuid4(),
            run_id=run.id,
            step_id=step_id,
            policy_version=proposal.policy.version,
            head_sha=proposal.review.candidate.head_sha,
            candidate_tree_digest=validation_tree or proposal.review.candidate.tree_digest,
        )
        validation_wire = encode_evidence_manifest(validation)
        validation_descriptor = await store.put_bytes(
            validation_wire, media_type="application/vnd.forge.evidence-manifest+json"
        )
        validation_descriptor = await work.artifacts.record(
            replace(validation_descriptor, schema_version=2),
            run_id=run.id,
            producer_type="evidence_set",
            producer_id=validation.evidence_set_id,
        )
        await work.evidence.record_set(
            ValidationEvidenceDraft(validation, ()),
            CanonicalEvidenceArtifact(validation_descriptor, validation, validation_wire),
        )
        await work.controller_steps.finalize(
            run.id,
            step_id,
            ExecutionStatus.SUCCEEDED,
            output_artifact_id=validation_descriptor.artifact_id,
            outcome="Validation evidence recorded",
        )
        await work.commit()
    manifest = SubscriptionAcceptanceEvidenceManifest(
        evidence_set_id=uuid4(),
        run_id=proposal.decision.run_id,
        step_id=step_id,
        policy_version=proposal.policy.version,
        head_sha=proposal.review.candidate.head_sha,
        base_sha=proposal.review.candidate.base_sha,
        candidate_tree_digest=proposal.review.candidate.tree_digest,
        candidate_manifest_digest=proposal.review.candidate.manifest_digest,
        candidate_epoch=proposal.review.candidate_epoch,
        producer_task_id=proposal.decision.task_id,
        producer_attempt_id=proposal.attempt_id,
        producer_result_digest=proposal.result_digest,
        acceptance=proposal.decision,
        selection_attempt_id=proposal.review.selection_attempt_id,
        selection_result_digest=proposal.review.selection_result_digest,
        selection_application_digest=proposal.review.selection_application_digest,
        selection=proposal.review.selection,
        review_handoff=proposal.review.review_handoff,
        review_result_digest=proposal.review.review_result_digest,
        review_application_digest=proposal.review.review_application_digest,
        receipt_evidence_digest=proof_descriptor.digest,
        validation_evidence_set_id=validation.evidence_set_id,
    )
    wire = encode_evidence_manifest(manifest)
    descriptor = await store.put_bytes(
        wire, media_type="application/vnd.forge.evidence-manifest+json"
    )
    async with factory() as work:
        descriptor = await work.artifacts.record(
            descriptor,
            run_id=manifest.run_id,
            producer_type="evidence_set",
            producer_id=manifest.evidence_set_id,
            parent_digests=tuple(sorted((proof_descriptor.digest, validation_descriptor.digest))),
        )
        await work.commit()
    return factory, manifest, CanonicalEvidenceArtifact(descriptor, manifest, wire)


async def reviewed_acceptance_case(session_factory, tmp_path):
    """Exercise the real handoff verifier and persistence before primary acceptance."""
    from uuid import UUID

    from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.application.services.subscription_handoff import SubscriptionHandoffVerifier
    from forge.application.services.subscription_handoff_application import (
        SubscriptionHandoffApplication,
    )
    from forge.domain.subscription import AcceptDecision, TaskBudget
    from subscription_launch_fixture import record_stopped_launch
    from test_subscription_acceptance_receipt_sources import record_snapshot_receipt
    from test_subscription_review_handoff import review_handoff_case
    from test_subscription_review_report import reviewed
    from test_subscription_usage import _known, _reservation

    factory, child, handoff = await review_handoff_case(
        session_factory,
        tmp_path,
        mutate=reviewed,
        primary_budget=TaskBudget(max_provider_attempts=8),
    )
    application = SubscriptionDecisionApplication(factory)
    proposal = await application.handoff_proposal(child.attempt.attempt_id)
    snapshot = GitWorkingTreeSnapshot(
        head_sha=handoff.candidate_commit,
        base_sha=proposal.worktree.base_sha,
        files=(),
        changed_paths=(),
    )
    _, _, data = await record_snapshot_receipt(
        factory,
        child,
        UUID(handoff.evidence_receipt_ids[0]),
        snapshot,
        proposal.worktree,
        proposal.policy.version,
    )
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    await store.put_bytes(data, media_type="application/json")

    async def capture(_):
        return snapshot

    completed = await SubscriptionHandoffApplication(
        factory, SubscriptionHandoffVerifier(factory, store, SimpleNamespace()), capture
    ).apply(child.attempt.attempt_id)
    assert completed.accepted and completed.disposition == "handoff_completed"
    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("primary-accepts-review", _reservation())
    decision = AcceptDecision(
        run_id=primary.task.run_id,
        task_id=primary.task.task_id,
        candidate_commit=handoff.candidate_commit,
        candidate_tree_digest=handoff.candidate_tree_digest,
        evidence_receipt_ids=(str(uuid4()),),
        rationale="Accept the verified reviewed candidate",
    )
    launch = await record_stopped_launch(session_factory, primary)
    result = await executor.settle(
        primary,
        SubscriptionInvocationResult(
            attempt=primary.attempt,
            decision=decision,
            telemetry=_known(),
            launch_proof=launch,
        ),
    )
    assert result.disposition == "decision_pending"
    return factory, primary, decision


@pytest.mark.integration
async def test_acceptance_evidence_preserves_subscription_producer_without_legacy_review(
    session_factory, tmp_path
):
    factory, manifest, artifact = await acceptance_evidence_case(session_factory, tmp_path)
    async with factory() as work:
        before = await work.session.scalar(select(func.count()).select_from(AgentExecution))
        draft = SubscriptionAcceptanceEvidenceDraft(manifest)
        result = await work.evidence.record_set(draft, artifact)
        assert result.kind is EvidenceKind.ACCEPTANCE
        assert result.producer_execution_id is None
        assert result.producer_task_id == manifest.producer_task_id
        assert result.producer_attempt_id == manifest.producer_attempt_id
        assert result.candidate_tree_digest == manifest.candidate_tree_digest
        assert result.review_finding_ids is None
        assert await work.evidence.record_set(draft, artifact) == result
        assert await work.session.scalar(select(func.count()).select_from(AgentExecution)) == before
        await work.commit()
    async with factory() as work:
        assert (
            await work.evidence.get_by_id(result.evidence_set_id, run_id=manifest.run_id) == result
        )
        run = await work.runs.get(manifest.run_id)
        assert run.state is RunState.VALIDATING and run.pending_gate is None


@pytest.mark.integration
@pytest.mark.parametrize("corrupt_review", [None, "result", "application"])
async def test_acceptance_evidence_retains_verified_selected_reviewer(
    session_factory, tmp_path, corrupt_review
):
    from forge.application.ports.evidence import EvidenceCorruptLineage
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    factory, manifest, artifact = await acceptance_evidence_case(
        session_factory, tmp_path, reviewed=True
    )
    assert manifest.review_handoff is not None
    assert manifest.selection.review_required
    if corrupt_review is not None:
        async with factory() as work:
            source = await work.session.get(
                SubscriptionAttemptResult, manifest.review_handoff.attempt_id
            )
            if corrupt_review == "result":
                source.result_digest = "f" * 64
            else:
                source.application_digest = "f" * 64
            await work.commit()
        async with factory() as work:
            with pytest.raises(EvidenceCorruptLineage):
                await work.evidence.record_set(
                    SubscriptionAcceptanceEvidenceDraft(manifest), artifact
                )
        return
    async with factory() as work:
        result = await work.evidence.record_set(
            SubscriptionAcceptanceEvidenceDraft(manifest), artifact
        )
        assert result.producer_attempt_id == manifest.producer_attempt_id
        assert result.producer_execution_id is None
        assert result.review_finding_ids == ()
        assert manifest.review_handoff.attempt_id != manifest.producer_attempt_id
        assert (
            await work.evidence.record_set(SubscriptionAcceptanceEvidenceDraft(manifest), artifact)
            == result
        )
        await work.commit()
