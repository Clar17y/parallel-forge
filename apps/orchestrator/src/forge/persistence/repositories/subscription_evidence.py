"""Source proof for immutable subscription acceptance evidence."""

import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.artifacts import ArtifactRepository
from forge.application.ports.evidence import EvidenceCorruptLineage
from forge.application.ports.subscription_acceptance_receipts import VerifiedAcceptanceReceipts
from forge.domain.evidence import SubscriptionAcceptanceEvidenceManifest, encode_evidence_manifest
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.subscription_acceptance import acceptance_evidence_proposal
from forge.persistence.repositories.subscription_acceptance_receipts import (
    _receipt_sources,
    verify_acceptance_receipt_evidence,
)


async def verify_acceptance_evidence_source(
    session: AsyncSession,
    artifacts: ArtifactRepository,
    manifest: SubscriptionAcceptanceEvidenceManifest,
) -> None:
    """Require current stopped authority and the exact persisted source records.

    The proposal holds the run lock. Reuse the receipt verifier so callback,
    stopped-launch, terminal-intent and artifact-lineage checks remain identical
    to those applied when the receipt proof was first recorded.
    """
    proposal = await acceptance_evidence_proposal(session, manifest.producer_attempt_id)
    review, candidate = proposal.review, proposal.review.candidate
    expected = manifest.model_copy(
        update={
            "run_id": proposal.decision.run_id,
            "policy_version": proposal.policy.version,
            "head_sha": candidate.head_sha,
            "base_sha": candidate.base_sha,
            "candidate_tree_digest": candidate.tree_digest,
            "candidate_manifest_digest": candidate.manifest_digest,
            "candidate_epoch": review.candidate_epoch,
            "producer_task_id": proposal.decision.task_id,
            "producer_attempt_id": proposal.attempt_id,
            "producer_result_digest": proposal.result_digest,
            "acceptance": proposal.decision,
            "selection_attempt_id": review.selection_attempt_id,
            "selection_result_digest": review.selection_result_digest,
            "selection_application_digest": review.selection_application_digest,
            "selection": review.selection,
            "review_handoff": review.review_handoff,
            "review_result_digest": review.review_result_digest,
            "review_application_digest": review.review_application_digest,
        }
    )
    # Canonical encoding revalidates both the outer model and nested domain data.
    if (
        encode_evidence_manifest(expected) != encode_evidence_manifest(manifest)
        or proposal.inspection != candidate
    ):
        raise EvidenceCorruptLineage("acceptance evidence differs from its current source")
    result = await session.get(SubscriptionAttemptResult, proposal.attempt_id)
    assert result is not None and result.application_payload is not None
    proof = VerifiedAcceptanceReceipts.from_payload(
        result.application_payload.get("receipt_verification")
    )
    sources = await _receipt_sources(session, proposal)
    await verify_acceptance_receipt_evidence(session, proposal, proof, sources)
    wire = json.dumps(
        proof.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if hashlib.sha256(wire).hexdigest() != manifest.receipt_evidence_digest:
        raise EvidenceCorruptLineage("acceptance receipt artifact differs from its proof")
    descriptor = await artifacts.get_by_digest(
        manifest.receipt_evidence_digest, run_id=manifest.run_id
    )
    if (
        descriptor.run_id != manifest.run_id
        or descriptor.producer_type != "subscription_acceptance_receipts"
        or descriptor.producer_id != manifest.producer_attempt_id
        or descriptor.media_type != "application/json"
        or descriptor.schema_version != 1
        or descriptor.byte_count != len(wire)
        or descriptor.truncated
        or descriptor.parent_digests != tuple(sorted(digest for digest, _ in proof.artifact_proofs))
    ):
        raise EvidenceCorruptLineage("acceptance receipt artifact provenance differs")
