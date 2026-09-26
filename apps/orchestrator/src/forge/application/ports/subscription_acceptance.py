"""Intrinsic acceptance-intent checks; external receipt/Git verification is separate."""

from dataclasses import dataclass
from uuid import UUID

from forge.application.ports.subscription_acceptance_receipts import VerifiedAcceptanceReceipts
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_review import CandidateReviewEvidence
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.agent import ReviewDecision
from forge.domain.policy import ProjectPolicy
from forge.domain.subscription import AcceptDecision


@dataclass(frozen=True, slots=True)
class PreparedSubscriptionAcceptance:
    attempt_id: UUID
    decision: AcceptDecision
    result_digest: str
    review: CandidateReviewEvidence
    task_version: int
    run_version: int
    policy: ProjectPolicy
    worktree: ManagedWorktree
    inspection: CandidateInspection | None = None


@dataclass(frozen=True, slots=True)
class RetainedSubscriptionAcceptance:
    """Verified historical sources; this type grants no current effect authority."""

    attempt_id: UUID
    decision: AcceptDecision
    result_digest: str
    review: CandidateReviewEvidence
    policy: ProjectPolicy
    worktree: ManagedWorktree
    receipts: VerifiedAcceptanceReceipts


def acceptance_intent_payload(
    decision: AcceptDecision,
    source: CandidateReviewEvidence | None,
    result_digest: str,
) -> dict[str, object] | None:
    if source is None or (decision.run_id, decision.task_id) != (
        source.run_id,
        source.primary_task_id,
    ):
        return None
    if decision.candidate_tree_digest != source.candidate.tree_digest or (
        decision.candidate_commit is not None
        and decision.candidate_commit != source.candidate.head_sha
    ):
        return None
    if source.selection.review_required and (
        source.review_handoff is None
        or source.review_handoff.review_output.decision is not ReviewDecision.APPROVE
    ):
        return None
    try:
        identities = tuple(UUID(value) for value in decision.evidence_receipt_ids)
    except ValueError:
        return None
    if (
        not 1 <= len(identities) <= 128
        or len(set(identities)) != len(identities)
        or any(
            identity.int == 0 or str(identity) != value
            for identity, value in zip(identities, decision.evidence_receipt_ids, strict=True)
        )
    ):
        return None
    return {
        "schema_version": 1,
        "kind": "acceptance_intent",
        "result_digest": result_digest,
        "candidate_epoch": source.candidate_epoch,
        "review_sources": source.payload(),
        "receipt_claims": list(decision.evidence_receipt_ids),
    }
