"""Apply only decisions whose durable source still has execution authority."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_acceptance_receipts import (
    AcceptanceReceiptSource,
    VerifiedAcceptanceReceipts,
)
from forge.application.ports.subscription_base_update import BaseUpdateReservation
from forge.application.ports.subscription_candidate import (
    CandidateInspection,
    PreparedReviewSelection,
)
from forge.application.ports.subscription_candidate_revision import AcceptanceRevision
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_handoff import (
    HandoffObservation,
    RejectedSubscriptionHandoff,
    SettledSubscriptionHandoff,
    VerifiedSubscriptionHandoff,
)
from forge.application.ports.subscription_review import CandidateReviewEvidence
from forge.application.ports.subscription_validation import (
    AcceptanceValidationBinding,
    AcceptanceValidationRepair,
)
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.domain.agent import UntrustedContent


class SubscriptionDecisionError(ValueError):
    pass


class PendingDecisionKind(StrEnum):
    PLAN = "plan"
    DELEGATE = "delegate"
    WAIT = "wait"
    REASSIGN = "reassign"
    HANDOFF = "handoff"
    SCOPE_REQUEST = "scope_request"
    SCOPE_RESPONSE = "scope_response"
    TASK_ACCEPTANCE = "task_acceptance"
    FINAL_ACCEPTANCE = "final_acceptance"
    REVIEW_SELECTION = "review_selection"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class PendingSubscriptionDecision:
    attempt_id: UUID
    kind: PendingDecisionKind


class SubscriptionDecisionRepository(Protocol):
    async def reserve_acceptance_base(
        self, proposal: PreparedSubscriptionAcceptance
    ) -> BaseUpdateReservation | None: ...

    async def verify_base_reservation(
        self,
        source: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance,
        reservation: BaseUpdateReservation,
        *,
        live: bool = False,
    ) -> None: ...

    async def reopen_acceptance_base(
        self,
        proposal: PreparedSubscriptionAcceptance,
        command_id: UUID,
        pr_digest: str,
        target: str,
        update_id: UUID,
        adoption_id: UUID,
        reservation: BaseUpdateReservation,
    ) -> AcceptanceValidationRepair: ...

    async def verify_acceptance_base(
        self,
        source: RetainedSubscriptionAcceptance,
        command_id: UUID,
        pr_digest: str,
        target: str,
        update_id: UUID,
        adoption_id: UUID,
        reservation: BaseUpdateReservation,
        receipt: AcceptanceValidationRepair,
    ) -> None: ...

    async def acceptance_remote_source(
        self, attempt_id: UUID, *, allow_paused: bool = False
    ) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]: ...

    async def reopen_acceptance_remote(
        self,
        proposal: PreparedSubscriptionAcceptance,
        command_id: UUID,
        pr_digest: str,
        feedback: UntrustedContent,
    ) -> AcceptanceValidationRepair: ...

    async def verify_acceptance_remote(
        self,
        source: RetainedSubscriptionAcceptance,
        command_id: UUID,
        pr_digest: str,
        feedback: UntrustedContent,
        receipt: AcceptanceValidationRepair,
    ) -> None: ...

    async def acceptance_revision_source(
        self, attempt_id: UUID
    ) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]: ...

    async def reopen_acceptance_revision(
        self,
        proposal: PreparedSubscriptionAcceptance,
        command_id: UUID,
        revision: AcceptanceRevision,
        repair_limit: int,
    ) -> AcceptanceValidationRepair: ...

    async def verify_acceptance_revision(
        self,
        source: RetainedSubscriptionAcceptance,
        command_id: UUID,
        revision: AcceptanceRevision,
        receipt: AcceptanceValidationRepair,
    ) -> None: ...

    async def reject_acceptance_validation(
        self,
        proposal: PreparedSubscriptionAcceptance,
        command_id: UUID,
        validation_digest: str,
        failed_checks: tuple[str, ...],
        repair_limit: int,
    ) -> AcceptanceValidationRepair: ...

    async def verify_acceptance_validation_rejection(
        self,
        source: RetainedSubscriptionAcceptance,
        command_id: UUID,
        validation_digest: str,
        failed_checks: tuple[str, ...],
        receipt: AcceptanceValidationRepair,
    ) -> None: ...

    async def retained_acceptance_source(
        self, attempt_id: UUID
    ) -> RetainedSubscriptionAcceptance: ...

    async def acceptance_validation_binding(
        self, attempt_id: UUID
    ) -> AcceptanceValidationBinding | None: ...

    async def acceptance_validation_source(
        self, attempt_id: UUID
    ) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]: ...

    async def reject_acceptance_receipt_claims(
        self, attempt_id: UUID
    ) -> SubscriptionSettlement: ...

    async def record_acceptance_receipts(
        self, proposal: PreparedSubscriptionAcceptance, proof: VerifiedAcceptanceReceipts
    ) -> None: ...

    async def acceptance_receipt_sources(
        self, proposal: PreparedSubscriptionAcceptance
    ) -> tuple[AcceptanceReceiptSource, ...]: ...

    async def acceptance_proposal(self, attempt_id: UUID) -> PreparedSubscriptionAcceptance: ...

    async def record_acceptance_inspection(
        self, proposal: PreparedSubscriptionAcceptance, snapshot: GitWorkingTreeSnapshot
    ) -> CandidateInspection: ...

    async def reject_acceptance_mismatch(
        self,
        proposal: PreparedSubscriptionAcceptance,
        snapshot: GitWorkingTreeSnapshot | None = None,
    ) -> SubscriptionSettlement: ...

    async def prepare_acceptance(self, attempt_id: UUID) -> SubscriptionSettlement: ...

    async def candidate_review_evidence(
        self, run_id: UUID, primary_task_id: UUID
    ) -> CandidateReviewEvidence: ...

    async def review_selection_context(
        self, run_id: UUID, task_id: UUID
    ) -> dict[str, object] | None: ...
    async def finalize_review_selection(self, attempt_id: UUID) -> SubscriptionSettlement: ...
    async def reject_candidate_mismatch(self, attempt_id: UUID) -> SubscriptionSettlement: ...
    async def review_selection_proposal(self, attempt_id: UUID) -> PreparedReviewSelection: ...
    async def record_candidate_inspection(
        self, proposal: PreparedReviewSelection, snapshot: GitWorkingTreeSnapshot
    ) -> CandidateInspection: ...

    async def prepare_review_selection(self, attempt_id: UUID) -> SubscriptionSettlement: ...
    async def apply_scope_response(self, attempt_id: UUID) -> SubscriptionSettlement: ...
    async def apply_scope_request(self, attempt_id: UUID) -> SubscriptionSettlement: ...
    async def reject_handoff_claim(
        self, observation: HandoffObservation
    ) -> SubscriptionSettlement: ...

    async def reject_handoff(
        self, observation: HandoffObservation, proof: RejectedSubscriptionHandoff
    ) -> SubscriptionSettlement: ...

    async def handoff_replay(self, attempt_id: UUID) -> SubscriptionSettlement | None: ...

    async def apply_handoff(
        self, observation: HandoffObservation, proof: VerifiedSubscriptionHandoff
    ) -> SubscriptionSettlement: ...

    async def begin_handoff_observation(
        self, attempt_id: UUID, token: UUID
    ) -> HandoffObservation: ...
    async def release_handoff_observation(self, observation: HandoffObservation) -> bool: ...

    async def handoff_proposal(self, attempt_id: UUID) -> SettledSubscriptionHandoff: ...

    async def pending_applications(
        self, after_id: UUID | None, limit: int
    ) -> tuple[PendingSubscriptionDecision, ...]: ...
    async def apply_delegation(self, attempt_id: UUID) -> SubscriptionSettlement: ...
    async def apply_wait(self, attempt_id: UUID) -> SubscriptionSettlement: ...

    async def apply_reassignment(self, attempt_id: UUID) -> SubscriptionSettlement: ...
