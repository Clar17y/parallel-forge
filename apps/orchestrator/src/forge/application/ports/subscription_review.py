"""Retained candidate review sources, not acceptance or human approval."""

from dataclasses import dataclass
from uuid import UUID

from forge.application.ports.subscription_candidate import CandidateInspection
from forge.domain.subscription import (
    ReviewedTaskHandoff,
    ReviewSelection,
    encode_subscription_record,
)


@dataclass(frozen=True, slots=True)
class CandidateReviewEvidence:
    """Source identities proven under the caller's run transaction.

    Callers still own current Git/validation verification, acceptance and human
    gates. Absence of a reviewer is an explicit selection, never an approve report.
    """

    run_id: UUID
    primary_task_id: UUID
    candidate_epoch: int
    candidate: CandidateInspection
    selection: ReviewSelection
    selection_attempt_id: UUID
    selection_result_digest: str
    selection_application_digest: str
    review_attempt_id: UUID | None = None
    review_handoff: ReviewedTaskHandoff | None = None
    review_result_digest: str | None = None
    review_application_digest: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "primary_task_id": str(self.primary_task_id),
            "candidate_epoch": self.candidate_epoch,
            "candidate": self.candidate.payload(),
            "selection": encode_subscription_record(self.selection),
            "selection_attempt_id": str(self.selection_attempt_id),
            "selection_result_digest": self.selection_result_digest,
            "selection_application_digest": self.selection_application_digest,
            "review_attempt_id": None
            if self.review_attempt_id is None
            else str(self.review_attempt_id),
            "review_handoff": None
            if self.review_handoff is None
            else encode_subscription_record(self.review_handoff),
            "review_result_digest": self.review_result_digest,
            "review_application_digest": self.review_application_digest,
        }
