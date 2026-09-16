"""Atomic settled decision application and completed-handoff proof rechecks."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import String, and_, case, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_acceptance_receipts import (
    AcceptanceReceiptClaimError,
    AcceptanceReceiptSource,
    VerifiedAcceptanceReceipts,
)
from forge.application.ports.subscription_base_update import BaseUpdateReservation
from forge.application.ports.subscription_candidate import (
    CandidateInspection,
    PreparedReviewSelection,
)
from forge.application.ports.subscription_candidate_revision import AcceptanceRevision
from forge.application.ports.subscription_decisions import (
    PendingDecisionKind,
    PendingSubscriptionDecision,
    SubscriptionDecisionError,
)
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_handoff import (
    HandoffObservation,
    RejectedSubscriptionHandoff,
    SettledSubscriptionHandoff,
    VerifiedSubscriptionHandoff,
    handoff_claim_error,
    verified_handoff_digest,
)
from forge.application.ports.subscription_review import CandidateReviewEvidence
from forge.application.ports.subscription_validation import (
    AcceptanceValidationBinding,
    AcceptanceValidationRepair,
)
from forge.application.ports.worktrees import GitWorkingTreeSnapshot, ManagedWorktree
from forge.domain.agent import UntrustedContent
from forge.domain.artifact import validate_artifact_digest
from forge.domain.operation import canonical_digest
from forge.domain.paths import policy_path_key
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.scheduling import ScheduleTask
from forge.domain.subscription import (
    AcceptanceCriterion,
    AcceptDecision,
    AttemptIdentity,
    AttemptTelemetry,
    BoundReassignDecision,
    BoundScopeResponseDecision,
    DelegateDecision,
    ExecutionEnvelope,
    ForwardFeedbackDecision,
    HandoffStatus,
    LogicalTaskContract,
    ReviewedTaskHandoff,
    ReviewSelection,
    ScopeRequestDecision,
    SpecialistPurpose,
    TaskHandoff,
    WaitDecision,
    decode_subscription_record,
    encode_subscription_record,
    is_read_only,
    validate_task_dag,
)
from forge.domain.subscription_delegation import validate_child_authority
from forge.domain.subscription_execution import SUBSCRIPTION_WORK_STATES
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.persistence.models import RunEvent
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.projects import PostgresProjectRepository
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_feedback import (
    PostgresSubscriptionFeedbackRepository,
)
from forge.persistence.repositories.subscription_handoff_evidence import (
    PostgresSubscriptionHandoffEvidence,
)
from forge.persistence.repositories.subscription_handoff_fence import (
    PostgresSubscriptionHandoffFence,
)
from forge.persistence.repositories.subscription_launch import launches_confirmed
from forge.persistence.repositories.subscription_reassignment import (
    reassignment_proof,
    requeue_reassigned_child,
)
from forge.persistence.repositories.subscription_task_stop_receipts import (
    pending_decision_task_version,
)


def _decode(value: object) -> object:
    if not isinstance(value, Mapping):
        raise TypeError("subscription record must be an object")
    return decode_subscription_record(value)


def _selected_review_task(
    parent: LogicalTaskContract,
    selection: ReviewSelection,
    envelope: ExecutionEnvelope,
    attempt_id: UUID,
) -> LogicalTaskContract | None:
    if not selection.review_required:
        return None
    route = envelope.route_for(SpecialistPurpose.INDEPENDENT_REVIEW)
    if selection.reviewer_route != route.effective:
        raise SubscriptionDecisionError("review selection route differs from approved route")
    child = LogicalTaskContract(
        run_id=parent.run_id,
        task_id=selection.review_task_id or uuid5(NAMESPACE_URL, f"forge:review:{attempt_id}"),
        parent_task_id=parent.task_id,
        purpose=SpecialistPurpose.INDEPENDENT_REVIEW,
        route=route,
        budget=replace(parent.budget, max_repairs=0),
        max_repairs=0,
        typed_acceptance=(
            AcceptanceCriterion(
                criterion_id="independent_review",
                description="Independently review the selected candidate and report evidence and unresolved findings.",
            ),
        ),
        untrusted_context_refs=(f"candidate-intent:{attempt_id}",),
    )
    validate_child_authority(parent, child, envelope)
    return child


def _selection_receipt(
    selection: ReviewSelection, child: LogicalTaskContract | None
) -> dict[str, object]:
    return {
        "review_required": selection.review_required,
        "review_task": None if child is None else encode_subscription_record(child),
    }


def _rejection_handoff(
    attempt: SubscriptionAttempt,
    *,
    repair: bool,
    handoff: bool = False,
    claim_error: str | None = None,
    candidate: bool = False,
    acceptance: bool = False,
    receipt_issue: AcceptanceReceiptClaimError | None = None,
) -> TaskHandoff:
    return TaskHandoff(
        run_id=attempt.run_id,
        task_id=attempt.task_row_id,
        attempt_id=attempt.id,
        status=HandoffStatus.FAILED if repair else HandoffStatus.REPAIRS_EXHAUSTED,
        summary=(
            f"Acceptance intent rejected: {receipt_issue}"
            if receipt_issue is not None
            else "Acceptance intent rejected: candidate, review or receipt claims differ"
            if acceptance
            else "Candidate proposal rejected: observed commit or tree differs"
            if candidate
            else f"Completed handoff rejected: {claim_error.replace('_', ' ')}"
            if claim_error is not None
            else "Completed handoff rejected: outputs, claimed commit or check evidence differs"
            if handoff
            else "Decision rejected: targets or children exceed the task authority"
        ),
    )


def _claim_rejection_reason(receipt: Mapping[str, object] | None) -> str | None:
    if receipt is None or receipt.get("kind") != "rejected_handoff_claim":
        return None
    reason = receipt.get("claim_error")
    if not isinstance(reason, str) or reason not in {
        "invalid_receipt_claims",
        "invalid_check_claims",
        "candidate_claim_differs",
    }:
        raise SubscriptionDecisionError("handoff claim rejection reason differs")
    return reason


class PostgresSubscriptionDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acceptance_proposal(self, attempt_id: UUID) -> PreparedSubscriptionAcceptance:
        from forge.persistence.repositories.subscription_acceptance import acceptance_proposal

        return await acceptance_proposal(self._session, attempt_id)

    async def acceptance_receipt_sources(
        self, proposal: PreparedSubscriptionAcceptance
    ) -> tuple[AcceptanceReceiptSource, ...]:
        from forge.persistence.repositories.subscription_acceptance_receipts import (
            acceptance_receipt_sources,
        )

        return await acceptance_receipt_sources(self._session, proposal)

    async def record_acceptance_receipts(
        self, proposal: PreparedSubscriptionAcceptance, proof: VerifiedAcceptanceReceipts
    ) -> None:
        from forge.persistence.repositories.subscription_acceptance_receipts import (
            record_acceptance_receipts,
        )

        await record_acceptance_receipts(self._session, proposal, proof)

    async def reject_acceptance_receipt_claims(self, attempt_id: UUID) -> SubscriptionSettlement:
        from forge.persistence.repositories.subscription_acceptance import (
            reject_acceptance_receipt_claims,
        )

        return await reject_acceptance_receipt_claims(self._session, attempt_id)

    async def record_acceptance_inspection(
        self, proposal: PreparedSubscriptionAcceptance, snapshot: GitWorkingTreeSnapshot
    ) -> CandidateInspection:
        from forge.persistence.repositories.subscription_acceptance import (
            record_acceptance_inspection,
        )

        return await record_acceptance_inspection(self._session, proposal, snapshot)

    async def reject_acceptance_mismatch(
        self,
        proposal: PreparedSubscriptionAcceptance,
        snapshot: GitWorkingTreeSnapshot | None = None,
    ) -> SubscriptionSettlement:
        from forge.persistence.repositories.subscription_acceptance import (
            reject_acceptance_mismatch,
        )

        return await reject_acceptance_mismatch(self._session, proposal, snapshot)

    async def prepare_acceptance(self, attempt_id: UUID) -> SubscriptionSettlement:
        result = await self._apply(attempt_id, decision_type=AcceptDecision)
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def acceptance_validation_binding(
        self, attempt_id: UUID
    ) -> AcceptanceValidationBinding | None:
        from forge.persistence.repositories.subscription_validation import (
            acceptance_validation_binding,
        )

        return await acceptance_validation_binding(self._session, attempt_id)

    async def retained_acceptance_source(self, attempt_id: UUID) -> RetainedSubscriptionAcceptance:
        from forge.persistence.repositories.subscription_validation import (
            retained_acceptance_source,
        )

        return await retained_acceptance_source(self._session, attempt_id)

    async def acceptance_validation_source(
        self, attempt_id: UUID
    ) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]:
        from forge.persistence.repositories.subscription_validation import (
            acceptance_validation_source,
        )

        return await acceptance_validation_source(self._session, attempt_id)

    async def acceptance_remote_source(
        self, attempt_id: UUID, *, allow_paused: bool = False
    ) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]:
        from forge.persistence.repositories.subscription_validation import acceptance_remote_source

        return await acceptance_remote_source(self._session, attempt_id, allow_paused=allow_paused)

    async def reserve_acceptance_base(
        self, proposal: PreparedSubscriptionAcceptance
    ) -> BaseUpdateReservation | None:
        from forge.persistence.repositories.subscription_base_update import reserve_acceptance_base

        return await reserve_acceptance_base(self._session, proposal)

    async def verify_base_reservation(
        self,
        source: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance,
        reservation: BaseUpdateReservation,
        *,
        live: bool = False,
    ) -> None:
        from forge.persistence.repositories.subscription_base_update import verify_base_reservation

        await verify_base_reservation(self._session, source, reservation, live=live)

    async def reopen_acceptance_base(
        self,
        proposal: PreparedSubscriptionAcceptance,
        command_id: UUID,
        pr_digest: str,
        target: str,
        update_id: UUID,
        adoption_id: UUID,
        reservation: BaseUpdateReservation,
    ) -> AcceptanceValidationRepair:
        from forge.persistence.repositories.subscription_base_update import reopen_acceptance_base

        return await reopen_acceptance_base(
            self._session,
            proposal,
            command_id,
            pr_digest,
            target,
            update_id,
            adoption_id,
            reservation,
        )

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
    ) -> None:
        from forge.persistence.repositories.subscription_base_update import verify_acceptance_base

        await verify_acceptance_base(
            self._session,
            source,
            command_id,
            pr_digest,
            target,
            update_id,
            adoption_id,
            reservation,
            receipt,
        )

    async def reopen_acceptance_remote(
        self,
        proposal: PreparedSubscriptionAcceptance,
        command_id: UUID,
        pr_digest: str,
        feedback: UntrustedContent,
    ) -> AcceptanceValidationRepair:
        from forge.persistence.repositories.subscription_remote_remediation import (
            reopen_acceptance_remote,
        )

        return await reopen_acceptance_remote(
            self._session, proposal, command_id, pr_digest, feedback
        )

    async def verify_acceptance_remote(
        self,
        source: RetainedSubscriptionAcceptance,
        command_id: UUID,
        pr_digest: str,
        feedback: UntrustedContent,
        receipt: AcceptanceValidationRepair,
    ) -> None:
        from forge.persistence.repositories.subscription_remote_remediation import (
            verify_acceptance_remote,
        )

        await verify_acceptance_remote(
            self._session, source, command_id, pr_digest, feedback, receipt
        )

    async def acceptance_revision_source(
        self, attempt_id: UUID
    ) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]:
        from forge.persistence.repositories.subscription_validation import (
            acceptance_revision_source,
        )

        return await acceptance_revision_source(self._session, attempt_id)

    async def reopen_acceptance_revision(
        self,
        proposal: PreparedSubscriptionAcceptance,
        command_id: UUID,
        revision: AcceptanceRevision,
        repair_limit: int,
    ) -> AcceptanceValidationRepair:
        from forge.persistence.repositories.subscription_candidate_revision import (
            reopen_acceptance_revision,
        )

        return await reopen_acceptance_revision(
            self._session, proposal, command_id, revision, repair_limit
        )

    async def verify_acceptance_revision(
        self,
        source: RetainedSubscriptionAcceptance,
        command_id: UUID,
        revision: AcceptanceRevision,
        receipt: AcceptanceValidationRepair,
    ) -> None:
        from forge.persistence.repositories.subscription_candidate_revision import (
            verify_acceptance_revision,
        )

        await verify_acceptance_revision(self._session, source, command_id, revision, receipt)

    async def reject_acceptance_validation(
        self,
        proposal: PreparedSubscriptionAcceptance,
        command_id: UUID,
        validation_digest: str,
        failed_checks: tuple[str, ...],
        repair_limit: int,
    ) -> AcceptanceValidationRepair:
        from forge.persistence.repositories.subscription_validation_repair import (
            reject_acceptance_validation,
        )

        return await reject_acceptance_validation(
            self._session, proposal, command_id, validation_digest, failed_checks, repair_limit
        )

    async def verify_acceptance_validation_rejection(
        self,
        source: RetainedSubscriptionAcceptance,
        command_id: UUID,
        validation_digest: str,
        failed_checks: tuple[str, ...],
        receipt: AcceptanceValidationRepair,
    ) -> None:
        from forge.persistence.repositories.subscription_validation_repair import (
            verify_acceptance_validation_rejection,
        )

        await verify_acceptance_validation_rejection(
            self._session, source, command_id, validation_digest, failed_checks, receipt
        )

    async def candidate_review_evidence(
        self, run_id: UUID, primary_task_id: UUID
    ) -> CandidateReviewEvidence:
        from forge.persistence.repositories.subscription_review import candidate_review_evidence

        return await candidate_review_evidence(self._session, run_id, primary_task_id)

    async def begin_handoff_observation(self, attempt_id: UUID, token: UUID) -> HandoffObservation:
        proposal = await self.handoff_proposal(attempt_id)
        return await PostgresSubscriptionHandoffFence(self._session).begin(proposal, token)

    async def release_handoff_observation(self, observation: HandoffObservation) -> bool:
        return await PostgresSubscriptionHandoffFence(self._session).release(observation)

    async def pending_applications(
        self, after_id: UUID | None, limit: int
    ) -> tuple[PendingSubscriptionDecision, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("pending decision page must contain 1 to 100 items")
        if after_id is not None and not isinstance(after_id, UUID):
            raise TypeError("pending decision cursor must be a UUID")
        # Read only bounded dispatch metadata. Each application service loads
        # and validates its complete source under its own authority checks.
        plan_type = SubscriptionAttemptResult.result_payload["decision"]["type"].astext
        record_type = SubscriptionAttemptResult.result_payload["decision"]["record"][
            "$record"
        ].astext
        target = SubscriptionAttemptResult.result_payload["decision"]["record"]["fields"][1][1][
            "$uuid"
        ].astext
        kind = case(
            (plan_type == "PlanOutput", "plan"),
            (record_type == "DelegateDecision", "delegate"),
            (record_type == "WaitDecision", "wait"),
            (record_type == "BoundReassignDecision", "reassign"),
            (record_type == "ForwardFeedbackDecision", "forward_feedback"),
            (record_type.in_(("TaskHandoff", "ReviewedTaskHandoff")), "handoff"),
            (record_type == "ScopeRequestDecision", "scope_request"),
            (record_type == "BoundScopeResponseDecision", "scope_response"),
            (record_type == "ReviewSelection", "review_selection"),
            (
                and_(
                    record_type == "AcceptDecision",
                    target != cast(SubscriptionAttempt.task_row_id, String),
                ),
                "task_acceptance",
            ),
            (
                and_(
                    record_type == "AcceptDecision",
                    target == cast(SubscriptionAttempt.task_row_id, String),
                ),
                "final_acceptance",
            ),
            else_="unsupported",
        )
        query = (
            select(SubscriptionAttemptResult.attempt_id, kind)
            .join(
                SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionAttemptResult.attempt_id
            )
            .where(
                or_(
                    and_(
                        SubscriptionAttemptResult.disposition == "decision_pending",
                        SubscriptionAttemptResult.accepted.is_(False),
                    ),
                    and_(
                        record_type == "ReviewSelection",
                        SubscriptionAttemptResult.disposition == "candidate_prepared",
                        SubscriptionAttemptResult.accepted.is_(True),
                    ),
                    and_(
                        record_type == "AcceptDecision",
                        SubscriptionAttemptResult.disposition == "acceptance_prepared",
                        SubscriptionAttemptResult.accepted.is_(True),
                        ~select(RunEvent.id)
                        .where(
                            RunEvent.run_id == SubscriptionAttempt.run_id,
                            RunEvent.event_type == "run.subscription_validation_requested",
                            RunEvent.payload["binding"]["source_attempt_id"].astext
                            == cast(SubscriptionAttempt.id, String),
                        )
                        .exists(),
                    ),
                ),
            )
        )
        if after_id is not None:
            query = query.where(SubscriptionAttemptResult.attempt_id > after_id)
        rows = (
            await self._session.execute(
                query.order_by(SubscriptionAttemptResult.attempt_id).limit(limit)
            )
        ).all()
        return tuple(
            PendingSubscriptionDecision(attempt_id, PendingDecisionKind(value))
            for attempt_id, value in rows
        )

    async def apply_delegation(self, attempt_id: UUID) -> SubscriptionSettlement:
        result = await self._apply(attempt_id, decision_type=DelegateDecision)
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def apply_wait(self, attempt_id: UUID) -> SubscriptionSettlement:
        result = await self._apply(attempt_id, decision_type=WaitDecision)
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def apply_reassignment(self, attempt_id: UUID) -> SubscriptionSettlement:
        result = await self._apply(attempt_id, decision_type=BoundReassignDecision)
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def apply_feedback(self, attempt_id: UUID) -> SubscriptionSettlement:
        result = await self._apply(attempt_id, decision_type=ForwardFeedbackDecision)
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def review_selection_context(
        self, run_id: UUID, task_id: UUID
    ) -> dict[str, object] | None:
        current = await self._session.get(SubscriptionSchedulerRun, run_id, populate_existing=True)
        if current is None or current.candidate_state != "closed":
            return None
        candidate_epoch = current.candidate_epoch
        attempt_id = await self._session.scalar(
            select(SubscriptionAttemptResult.attempt_id)
            .join(
                SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionAttemptResult.attempt_id
            )
            .where(
                SubscriptionAttempt.run_id == run_id,
                SubscriptionAttemptResult.disposition == "review_selected",
                SubscriptionAttempt.candidate_epoch == candidate_epoch - 1,
            )
            .order_by(
                SubscriptionAttempt.candidate_epoch.desc(),
                SubscriptionAttempt.attempt_number.desc(),
            )
            .limit(1)
        )
        if attempt_id is None:
            return None
        await self.prepare_review_selection(attempt_id)
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        result = await self._session.get(SubscriptionAttemptResult, attempt_id)
        scheduler = await self._session.get(
            SubscriptionSchedulerRun, run_id, populate_existing=True
        )
        assert attempt is not None and result is not None and result.application_payload is not None
        receipt = result.application_payload
        if (
            scheduler is None
            or scheduler.candidate_state != "closed"
            or scheduler.candidate_epoch != receipt["candidate_epoch"]
        ):
            return None
        selection = receipt["selection"]
        assert isinstance(selection, dict)
        child = selection["review_task"]
        if child is not None:
            child = _decode(child)
            assert isinstance(child, LogicalTaskContract)
        if task_id != attempt.task_row_id and (child is None or task_id != child.task_id):
            return None
        return {
            "decision": result.result_payload["decision"],
            "observation": receipt["observation"],
            "candidate_epoch": scheduler.candidate_epoch,
        }

    async def _delegation_reopening_receipt(
        self,
        primary: LogicalTaskContract,
        attempt: SubscriptionAttempt,
    ) -> dict[str, object] | None:
        if attempt.candidate_epoch is None:
            raise SubscriptionDecisionError("delegation source epoch is absent")
        sources = (
            await self._session.scalars(
                select(SubscriptionAttemptResult.attempt_id)
                .join(
                    SubscriptionAttempt,
                    SubscriptionAttempt.id == SubscriptionAttemptResult.attempt_id,
                )
                .where(
                    SubscriptionAttempt.run_id == primary.run_id,
                    SubscriptionAttempt.task_row_id == primary.task_id,
                    SubscriptionAttempt.candidate_epoch == attempt.candidate_epoch - 1,
                    SubscriptionAttemptResult.disposition == "review_selected",
                )
                .limit(2)
            )
        ).all()
        if not sources:
            return None
        if len(sources) != 1:
            raise SubscriptionDecisionError("delegation selection source is ambiguous")
        await self.prepare_review_selection(sources[0])
        selected = await self._session.get(SubscriptionAttemptResult, sources[0])
        assert selected is not None and selected.application_digest is not None
        return {
            "schema_version": 1,
            "kind": "candidate_reopened",
            "selection_attempt_id": str(sources[0]),
            "selection_application_digest": selected.application_digest,
            "source_epoch": attempt.candidate_epoch,
            "reopened_epoch": attempt.candidate_epoch + 1,
        }

    async def _handoff_selected_candidate(
        self,
        task: LogicalTaskContract,
        attempt: SubscriptionAttempt,
    ) -> CandidateInspection | None:
        """Re-prove the historical selection for this review's invocation epoch."""
        if (
            task.purpose is not SpecialistPurpose.INDEPENDENT_REVIEW
            or attempt.candidate_epoch is None
        ):
            return None
        sources = (
            await self._session.scalars(
                select(SubscriptionAttemptResult.attempt_id)
                .join(
                    SubscriptionAttempt,
                    SubscriptionAttempt.id == SubscriptionAttemptResult.attempt_id,
                )
                .where(
                    SubscriptionAttempt.run_id == task.run_id,
                    SubscriptionAttempt.task_row_id == task.parent_task_id,
                    SubscriptionAttempt.candidate_epoch == attempt.candidate_epoch - 1,
                    SubscriptionAttemptResult.disposition == "review_selected",
                )
                .limit(2)
            )
        ).all()
        if not sources:
            return None
        if len(sources) != 1:
            raise SubscriptionDecisionError("review selection source is ambiguous")
        await self.prepare_review_selection(sources[0])
        source = await self._session.get(SubscriptionAttemptResult, sources[0])
        assert source is not None and source.application_payload is not None
        receipt = source.application_payload
        selection = receipt["selection"]
        assert isinstance(selection, dict)
        child = selection["review_task"]
        if child is None or _decode(child) != task:
            return None
        if receipt["candidate_epoch"] != attempt.candidate_epoch:
            raise SubscriptionDecisionError("review selection epoch differs")
        return CandidateInspection.from_payload(receipt["observation"])

    async def _wake_selected_review_parent(
        self,
        task: SubscriptionTask,
        selected: CandidateInspection | None,
    ) -> None:
        # The caller has re-proven the exact selection, stopped handoff and run
        # lock. Review rejection also returns control to the primary, not approval.
        if selected is None or task.state != "terminal":
            return
        parent = await self._session.get(
            SubscriptionTask,
            task.parent_task_id,
            with_for_update=True,
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask,
            task.parent_task_id,
            with_for_update=True,
        )
        if (
            parent is None
            or scheduled is None
            or parent.run_id != task.run_id
            or scheduled.run_id != task.run_id
        ):
            raise SubscriptionDecisionError("selected review parent differs")
        if (parent.state, scheduled.state) != ("blocked", "blocked"):
            return
        parent.state = scheduled.state = "queued"
        parent.version += 1
        await self._session.flush()

    async def finalize_review_selection(self, attempt_id: UUID) -> SubscriptionSettlement:
        prior = await self.prepare_review_selection(attempt_id)
        if prior.disposition == "review_selected":
            return prior
        if prior.disposition != "candidate_prepared":
            raise SubscriptionDecisionError("candidate selection is already rejected")
        proposal = await self.review_selection_proposal(attempt_id)
        observed, selection = proposal.inspection, proposal.selection
        if (
            observed is None
            or observed.tree_digest != selection.candidate_tree_digest
            or (
                selection.candidate_commit is not None
                and observed.head_sha != selection.candidate_commit
            )
        ):
            raise SubscriptionDecisionError("candidate selection has no matching observation")
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        assert attempt is not None
        task = await self._session.get(SubscriptionTask, attempt.task_row_id)
        scheduled = await self._session.get(SubscriptionScheduledTask, attempt.task_row_id)
        result = await self._session.get(SubscriptionAttemptResult, attempt_id)
        assert task is not None and scheduled is not None and result is not None
        parent = decode_subscription_record(task.payload)
        assert isinstance(parent, LogicalTaskContract)
        subscription = PostgresSubscriptionRepository(self._session)
        envelope = await subscription.envelope_for_run(attempt.run_id)
        assert envelope is not None
        child = _selected_review_task(parent, selection, envelope, attempt_id)
        if child is not None:
            if await self._session.get(SubscriptionTask, child.task_id) is not None:
                raise SubscriptionDecisionError("selected review task identity already exists")
            await subscription.create_task(child, idempotency_key=f"selected-review:{attempt_id}")
            await PostgresSchedulingRepository(self._session).enqueue(
                ScheduleTask(
                    run_id=child.run_id,
                    task_id=child.task_id,
                    parent_task_id=parent.task_id,
                    worktree_id=scheduled.worktree_id,
                    read_only=True,
                    max_repairs=child.max_repairs,
                )
            )
        task.state = scheduled.state = "blocked" if child is not None else "queued"
        task.version += 1
        assert result.application_payload is not None
        result.application_payload = {
            **result.application_payload,
            "selection": _selection_receipt(selection, child),
        }
        result.application_digest = canonical_digest(result.application_payload)
        result.disposition = "review_selected"
        await self._session.flush()
        return SubscriptionSettlement(True, result.disposition)

    async def reject_candidate_mismatch(self, attempt_id: UUID) -> SubscriptionSettlement:
        prior = await self.prepare_review_selection(attempt_id)
        if prior.disposition in {"candidate_repair_queued", "candidate_rejected"}:
            return prior
        proposal = await self.review_selection_proposal(attempt_id)
        observed, selected = proposal.inspection, proposal.selection
        if observed is None or (
            observed.tree_digest == selected.candidate_tree_digest
            and (
                selected.candidate_commit is None or observed.head_sha == selected.candidate_commit
            )
        ):
            raise SubscriptionDecisionError("candidate has no observed identity mismatch")
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        assert attempt is not None
        task = await self._session.get(SubscriptionTask, attempt.task_row_id)
        scheduled = await self._session.get(SubscriptionScheduledTask, attempt.task_row_id)
        scheduler = await self._session.get(SubscriptionSchedulerRun, attempt.run_id)
        result = await self._session.get(SubscriptionAttemptResult, attempt_id)
        assert task is not None and scheduled is not None and scheduler is not None
        assert result is not None and result.application_payload is not None
        repair = (
            scheduled.repairs < scheduled.max_repairs
            and await PostgresSubscriptionBudgetRepository(self._session).try_debit_repair(
                task.run_id, task.id, attempt.id
            )
        )
        await PostgresSubscriptionRepository(self._session).record_decision(
            _rejection_handoff(attempt, repair=repair, candidate=True),
            idempotency_key=f"decision-rejection:{attempt_id}",
        )
        if repair:
            scheduled.repairs += 1
        task.state = scheduled.state = "queued" if repair else "terminal"
        task.version += 1
        scheduler.candidate_state = "open"
        scheduler.candidate_epoch += 1
        result.accepted = False
        result.disposition = "candidate_repair_queued" if repair else "candidate_rejected"
        result.application_payload = {
            **result.application_payload,
            "rejection": {
                "reason": "candidate_identity_differs",
                "repair": repair,
                "reopened_epoch": scheduler.candidate_epoch,
            },
        }
        result.application_digest = canonical_digest(result.application_payload)
        await self._session.flush()
        return SubscriptionSettlement(False, result.disposition)

    async def review_selection_proposal(self, attempt_id: UUID) -> PreparedReviewSelection:
        retained = await self._session.get(
            SubscriptionAttemptResult, attempt_id, populate_existing=True
        )
        if retained is None or retained.disposition != "candidate_prepared":
            raise SubscriptionDecisionError("candidate intent is not prepared")
        # Reuse the immutable stopped-producer proof before checking current state.
        await self.prepare_review_selection(attempt_id)
        attempt = await self._session.get(SubscriptionAttempt, attempt_id, populate_existing=True)
        assert attempt is not None
        run = await PostgresRunRepository(self._session).get_for_update(attempt.run_id)
        task = await self._session.get(
            SubscriptionTask, attempt.task_row_id, populate_existing=True
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask, attempt.task_row_id, populate_existing=True
        )
        scheduler = await self._session.get(
            SubscriptionSchedulerRun, attempt.run_id, populate_existing=True
        )
        result = await self._session.get(
            SubscriptionAttemptResult, attempt_id, populate_existing=True
        )
        assert result is not None
        if (
            run.state not in SUBSCRIPTION_WORK_STATES
            or run.pending_gate is not None
            or not run.worktree_path
            or not run.base_sha
            or run.policy_version is None
            or task is None
            or scheduled is None
            or scheduler is None
            or task.state != "blocked"
            or scheduled.state != "blocked"
            or task.pause_requested
            or task.cancel_requested
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or scheduled.lease_owner is not None
            or scheduled.lease_expires_at is not None
            or attempt.task_version is None
            or task.version != attempt.task_version + 2
            or canonical_digest(task.payload) != attempt.task_digest
            or not scheduler.admitted
            or scheduler.candidate_state != "closed"
            or attempt.candidate_epoch is None
            or scheduler.candidate_epoch != attempt.candidate_epoch + 1
            or result.disposition != "candidate_prepared"
            or not await PostgresSchedulingRepository(self._session)._is_coordinator(scheduled)
            or await PostgresCommandRepository(
                session=self._session
            ).has_pending_current_control_stop(run_id=run.id, expected_run_version=run.version)
        ):
            raise SubscriptionDecisionError("candidate inspection source is no longer current")
        pending = await self._session.scalar(
            select(SubscriptionTask.id)
            .where(
                SubscriptionTask.run_id == run.id,
                SubscriptionTask.id != task.id,
                SubscriptionTask.state != "terminal",
            )
            .limit(1)
        )
        effect = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == run.id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        if pending is not None or effect is not None:
            raise SubscriptionDecisionError("candidate inspection has unfinished work")
        projects = PostgresProjectRepository(self._session)
        project = await projects.get(run.project_id)
        record = await projects.get_policy(run.project_id, run.policy_version)
        policy = ProjectPolicy.model_validate(record.document)
        envelope = await PostgresSubscriptionRepository(self._session).envelope_for_run(run.id)
        if (
            envelope is None
            or envelope.safety_policy_version != run.policy_version
            or record.document_schema_version != 1
            or project.current_policy_version != run.policy_version
            or (policy.id, policy.version) != (run.project_id, run.policy_version)
            or record.policy_digest != canonical_digest(record.document)
        ):
            raise SubscriptionDecisionError("candidate inspection policy differs")
        worktree = ManagedWorktree(
            identity=WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name or "", policy.database.enabled
            ),
            path=Path(run.worktree_path),
            base_sha=run.base_sha,
        )
        if scheduled.worktree_id != worktree.identity.worktree_name:
            raise SubscriptionDecisionError("candidate inspection worktree differs")
        decision = _decode(result.result_payload["decision"])
        assert isinstance(decision, ReviewSelection)
        receipt = result.application_payload
        assert receipt is not None
        inspection = (
            CandidateInspection.from_payload(receipt["observation"])
            if "observation" in receipt
            else None
        )
        if inspection is not None and inspection.base_sha != worktree.base_sha:
            raise SubscriptionDecisionError("candidate observation base differs")
        return PreparedReviewSelection(
            attempt_id,
            decision,
            result.result_digest,
            scheduler.candidate_epoch,
            task.version,
            run.version,
            policy,
            worktree,
            inspection,
        )

    async def record_candidate_inspection(
        self, proposal: PreparedReviewSelection, snapshot: GitWorkingTreeSnapshot
    ) -> CandidateInspection:
        current = await self.review_selection_proposal(proposal.attempt_id)
        if replace(current, inspection=None) != replace(proposal, inspection=None):
            raise SubscriptionDecisionError("candidate changed during inspection")
        observed = CandidateInspection.from_snapshot(snapshot)
        if observed.base_sha != current.worktree.base_sha:
            raise SubscriptionDecisionError("candidate observation base differs")
        if current.inspection is not None:
            if observed != current.inspection:
                raise SubscriptionDecisionError("candidate observation replay differs")
            return current.inspection
        result = await self._session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert result is not None and result.application_payload is not None
        result.application_payload = {
            **result.application_payload,
            "observation": observed.payload(),
        }
        result.application_digest = canonical_digest(result.application_payload)
        await self._session.flush()
        return observed

    async def prepare_review_selection(self, attempt_id: UUID) -> SubscriptionSettlement:
        result = await self._apply(attempt_id, decision_type=ReviewSelection)
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def apply_scope_request(self, attempt_id: UUID) -> SubscriptionSettlement:
        result = await self._apply(attempt_id, decision_type=ScopeRequestDecision)
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def apply_scope_response(self, attempt_id: UUID) -> SubscriptionSettlement:
        result = await self._apply(attempt_id, decision_type=BoundScopeResponseDecision)
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def handoff_proposal(self, attempt_id: UUID) -> SettledSubscriptionHandoff:
        result = await self._apply(attempt_id, decision_type=TaskHandoff)
        assert isinstance(result, SettledSubscriptionHandoff)
        return result

    async def handoff_replay(self, attempt_id: UUID) -> SubscriptionSettlement | None:
        result = await self._apply(attempt_id, decision_type=TaskHandoff, historical_handoff=True)
        assert result is None or isinstance(result, SubscriptionSettlement)
        return result

    async def reject_handoff(
        self, observation: HandoffObservation, proof: RejectedSubscriptionHandoff
    ) -> SubscriptionSettlement:
        if proof.reason(observation.proposal.handoff) is None:
            raise SubscriptionDecisionError("handoff rejection has no verified mismatch")
        result = await self._apply(
            observation.proposal.handoff.attempt_id,
            decision_type=TaskHandoff,
            observation=observation,
            verified=proof.verified,
            rejection=proof,
        )
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def reject_handoff_claim(self, observation: HandoffObservation) -> SubscriptionSettlement:
        result = await self._apply(
            observation.proposal.handoff.attempt_id,
            decision_type=TaskHandoff,
            observation=observation,
            claim_rejection=True,
        )
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def apply_handoff(
        self, observation: HandoffObservation, proof: VerifiedSubscriptionHandoff
    ) -> SubscriptionSettlement:
        result = await self._apply(
            observation.proposal.handoff.attempt_id,
            decision_type=TaskHandoff,
            observation=observation,
            verified=proof,
        )
        assert isinstance(result, SubscriptionSettlement)
        return result

    async def _apply(
        self,
        attempt_id: UUID,
        *,
        decision_type: type[
            AcceptDecision
            | DelegateDecision
            | WaitDecision
            | TaskHandoff
            | ScopeRequestDecision
            | BoundScopeResponseDecision
            | BoundReassignDecision
            | ForwardFeedbackDecision
            | ReviewSelection
        ],
        observation: HandoffObservation | None = None,
        verified: VerifiedSubscriptionHandoff | None = None,
        historical_handoff: bool = False,
        rejection: RejectedSubscriptionHandoff | None = None,
        claim_rejection: bool = False,
    ) -> SubscriptionSettlement | SettledSubscriptionHandoff | None:
        # Handoffs without observation/proof only load a proposal. Application
        # shares these stopped-source guards and rechecks both under the locks.
        handoff_proposal = decision_type is TaskHandoff
        worker_decision = handoff_proposal or decision_type is ScopeRequestDecision
        disposition = "waiting" if decision_type is WaitDecision else "delegated"
        prefix = "wait" if decision_type is WaitDecision else "delegation"
        if handoff_proposal:
            prefix = "handoff"
            disposition = "handoff_completed"
        elif decision_type is ScopeRequestDecision:
            prefix, disposition = "scope-request", "scope_requested"
        elif decision_type is BoundScopeResponseDecision:
            prefix, disposition = "scope-response", "scope_responded"
        elif decision_type is BoundReassignDecision:
            prefix, disposition = "reassignment", "reassigned"
        elif decision_type is ForwardFeedbackDecision:
            prefix, disposition = "feedback-forward", "feedback_forwarded"
        elif decision_type is ReviewSelection:
            prefix, disposition = "review-selection", "candidate_prepared"
        elif decision_type is AcceptDecision:
            prefix, disposition = "acceptance", "acceptance_prepared"
        receipt = None
        receipt_issue: AcceptanceReceiptClaimError | None = None
        if observation is not None and verified is not None:
            receipt = {
                "schema_version": 1,
                "kind": "completed_handoff",
                "result_digest": observation.proposal.result_digest,
                "verified_evidence_digest": verified_handoff_digest(verified),
                "current_tree_digest": verified.current_tree_digest,
                "output_digest": verified.output_digest,
                "manifest_digest": verified.manifest_digest,
                "observation_id": str(observation.token),
                "observation_expires_at": observation.expires_at.isoformat(),
                "candidate_epoch": observation.proposal.candidate_epoch,
                "task_version": observation.proposal.task_version,
            }
            if rejection is not None:
                reason = rejection.reason(observation.proposal.handoff)
                if reason is None:
                    raise SubscriptionDecisionError("handoff rejection has no verified mismatch")
                receipt.update(
                    {
                        "kind": "rejected_handoff",
                        "verified_evidence_digest": verified_handoff_digest(rejection),
                        "current_tree_digest": rejection.current_tree_digest,
                        "current_output_digest": rejection.current_output_digest,
                        "current_head_sha": rejection.current_head_sha,
                        "rejection_reason": reason.value,
                    }
                )
        if claim_rejection:
            assert observation is not None and verified is None and rejection is None
            claim_error = handoff_claim_error(
                observation.proposal.handoff,
                observation.proposal.task,
                observation.proposal.selected_candidate,
            )
            if claim_error is None:
                raise SubscriptionDecisionError("handoff claim has no intrinsic contradiction")
            receipt = {
                "schema_version": 1,
                "kind": "rejected_handoff_claim",
                "result_digest": observation.proposal.result_digest,
                "claim_error": claim_error,
                "observation_id": str(observation.token),
                "observation_expires_at": observation.expires_at.isoformat(),
                "candidate_epoch": observation.proposal.candidate_epoch,
                "task_version": observation.proposal.task_version,
            }
        run_id = await self._session.scalar(
            select(SubscriptionAttempt.run_id).where(SubscriptionAttempt.id == attempt_id)
        )
        if run_id is None:
            raise SubscriptionDecisionError("delegation source is absent")
        run = await PostgresRunRepository(self._session).get_for_update(run_id)
        attempt = await self._session.get(
            SubscriptionAttempt, attempt_id, with_for_update=True, populate_existing=True
        )
        result = await self._session.get(
            SubscriptionAttemptResult, attempt_id, with_for_update=True, populate_existing=True
        )
        if attempt is None or result is None:
            raise SubscriptionDecisionError("delegation source is absent")
        if historical_handoff:
            if result.disposition not in {
                "handoff_completed",
                "handoff_repair_queued",
                "handoff_rejected",
            }:
                return None
            receipt = result.application_payload
            if (
                not isinstance(receipt, dict)
                or type(receipt.get("schema_version")) is not int
                or receipt["schema_version"] != 1
                or receipt.get("kind")
                not in (
                    {"completed_handoff"}
                    if result.disposition == "handoff_completed"
                    else {"rejected_handoff", "rejected_handoff_claim"}
                )
            ):
                raise SubscriptionDecisionError("handoff application receipt differs")
        subscription = PostgresSubscriptionRepository(self._session)
        current_envelope = await subscription.envelope_for_run(run_id)
        try:
            payload = result.result_payload
            context = payload["proposal_context"]
            if not isinstance(context, dict):
                raise TypeError
            parent = decode_subscription_record(context["task"])
            envelope = decode_subscription_record(context["envelope"])
            decision = _decode(payload["decision"])
            identity = _decode(payload["attempt"])
            telemetry = _decode(payload["telemetry"])
            proof = SubscriptionLaunchTerminalProof.model_validate(payload.get("launch_proof"))
            if (
                type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 4
                or canonical_digest(payload) != result.result_digest
                or payload.get("effective_failure") is not None
                or payload.get("failure") is not None
                or not isinstance(parent, LogicalTaskContract)
                or (
                    (parent.purpose is SpecialistPurpose.PRIMARY or parent.parent_task_id is None)
                    if worker_decision
                    else (
                        parent.purpose is not SpecialistPurpose.PRIMARY
                        or parent.parent_task_id is not None
                    )
                )
                or parent.run_id != run_id
                or parent.task_id != attempt.task_row_id
                or not isinstance(envelope, ExecutionEnvelope)
                or envelope != current_envelope
                or envelope.run_id != run_id
                or not envelope.permits_route(parent.purpose, parent.route)
                or not isinstance(
                    decision,
                    (
                        DelegateDecision,
                        AcceptDecision,
                        WaitDecision,
                        TaskHandoff,
                        ScopeRequestDecision,
                        BoundScopeResponseDecision,
                        BoundReassignDecision,
                        ForwardFeedbackDecision,
                        ReviewSelection,
                    ),
                )
                or (
                    type(decision) is not decision_type
                    and not (handoff_proposal and type(decision) is ReviewedTaskHandoff)
                )
                or decision.run_id != run_id
                or (
                    not isinstance(
                        decision,
                        (
                            AcceptDecision,
                            BoundScopeResponseDecision,
                            BoundReassignDecision,
                            ForwardFeedbackDecision,
                            ReviewSelection,
                        ),
                    )
                    and (
                        decision.parent_task_id
                        if isinstance(decision, DelegateDecision)
                        else decision.task_id
                    )
                    != parent.task_id
                )
                or (
                    isinstance(decision, TaskHandoff)
                    and (
                        decision.attempt_id != attempt_id
                        or decision.status is not HandoffStatus.COMPLETED
                    )
                )
                or not isinstance(identity, AttemptIdentity)
                or identity.attempt_id != attempt_id
                or identity.run_id != run_id
                or identity.task_id != parent.task_id
                or identity.attempt_number != attempt.attempt_number
                or not isinstance(telemetry, AttemptTelemetry)
                or encode_subscription_record(telemetry) != attempt.telemetry_payload
                or canonical_digest(context["task"]) != attempt.task_digest
                or canonical_digest(context["envelope"]) != attempt.envelope_digest
                or context["route"] != encode_subscription_record(parent.route)
                or context["route"] != attempt.route_payload
                or context["budget"] != encode_subscription_record(parent.budget)
                or context["candidate_epoch"] != attempt.candidate_epoch
                or context["task_version"] != attempt.task_version
            ):
                raise ValueError
        except KeyError, TypeError, ValueError:
            raise SubscriptionDecisionError("delegation source proof differs") from None
        consumption = await self._session.get(SubscriptionAttemptConsumption, attempt_id)
        if consumption is None or consumption.telemetry_payload != attempt.telemetry_payload:
            raise SubscriptionDecisionError("delegation usage settlement is absent or differs")
        launches = (
            await self._session.scalars(
                select(SubscriptionClientLaunch)
                .where(SubscriptionClientLaunch.attempt_id == attempt_id)
                .with_for_update()
            )
        ).all()
        if not launches_confirmed(
            launches, proof, require_decision=True, worker_identity=attempt.lease_owner
        ):
            raise SubscriptionDecisionError("delegation launch proof differs")
        if (
            handoff_proposal
            and receipt is None
            and (result.disposition != "decision_pending" or result.accepted)
        ):
            raise SubscriptionDecisionError("handoff proposal is no longer pending")
        selected_candidate = (
            await self._handoff_selected_candidate(parent, attempt) if handoff_proposal else None
        )
        if handoff_proposal and receipt is not None:
            selected_payload = None if selected_candidate is None else selected_candidate.payload()
            if historical_handoff:
                if receipt.get("selected_candidate") != selected_payload:
                    raise SubscriptionDecisionError("handoff selected candidate replay differs")
            elif selected_payload is not None:
                receipt["selected_candidate"] = selected_payload
        key = f"{prefix}:{attempt_id}"
        record = await self._session.scalar(
            select(SubscriptionDecisionRecord)
            .where(
                SubscriptionDecisionRecord.run_id == run_id,
                SubscriptionDecisionRecord.idempotency_key == key,
            )
            .with_for_update()
        )
        reopening_receipt = (
            await self._delegation_reopening_receipt(parent, attempt)
            if isinstance(decision, DelegateDecision)
            else None
        )
        acceptance_receipt = None
        task_acceptance = (
            isinstance(decision, AcceptDecision) and decision.task_id != parent.task_id
        )
        if task_acceptance:
            from forge.persistence.repositories.subscription_task_acceptance import (
                task_acceptance_payload,
            )

            assert isinstance(decision, AcceptDecision)
            disposition = "task_accepted"
            acceptance_receipt = await task_acceptance_payload(
                self._session, parent, decision, result
            )
        elif isinstance(decision, AcceptDecision):
            from forge.application.ports.subscription_acceptance import acceptance_intent_payload
            from forge.persistence.repositories.subscription_review import (
                historical_candidate_review_evidence,
            )

            assert attempt.candidate_epoch is not None
            review_source = await historical_candidate_review_evidence(
                self._session,
                run_id,
                parent.task_id,
                attempt.candidate_epoch,
                allow_missing=True,
            )
            acceptance_receipt = acceptance_intent_payload(
                decision, review_source, result.result_digest
            )
        candidate_receipt = None
        if isinstance(decision, ReviewSelection):
            if attempt.candidate_epoch is None:
                raise SubscriptionDecisionError("candidate intent source epoch is absent")
            candidate_receipt = {
                "schema_version": 1,
                "kind": "candidate_intent",
                "result_digest": result.result_digest,
                "candidate_epoch": attempt.candidate_epoch + 1,
                "proposed_commit": decision.candidate_commit,
                "proposed_tree_digest": decision.candidate_tree_digest,
            }
        rejected = result.disposition in {
            "decision_repair_queued",
            "decision_rejected",
            "handoff_repair_queued",
            "handoff_rejected",
        }
        candidate_rejected = isinstance(decision, ReviewSelection) and result.disposition in {
            "candidate_repair_queued",
            "candidate_rejected",
        }
        candidate_selected = (
            isinstance(decision, ReviewSelection) and result.disposition == "review_selected"
        )
        acceptance_rejected = (
            isinstance(decision, AcceptDecision)
            and not task_acceptance
            and result.disposition in {"acceptance_repair_queued", "acceptance_rejected"}
        )
        if (
            result.disposition == disposition
            or rejected
            or candidate_rejected
            or candidate_selected
            or acceptance_rejected
        ):
            if isinstance(decision, AcceptDecision):
                expected_acceptance = None if rejected else acceptance_receipt
                if (
                    expected_acceptance is not None
                    and not task_acceptance
                    and result.application_payload is not None
                    and "observation" in result.application_payload
                ):
                    try:
                        acceptance_observed = CandidateInspection.from_payload(
                            result.application_payload["observation"]
                        )
                        assert review_source is not None
                        if acceptance_observed.base_sha != review_source.candidate.base_sha:
                            raise ValueError
                    except KeyError, TypeError, ValueError:
                        raise SubscriptionDecisionError(
                            "acceptance observation replay differs"
                        ) from None
                    expected_acceptance = {
                        **expected_acceptance,
                        "observation": acceptance_observed.payload(),
                    }
                if (
                    expected_acceptance is not None
                    and not task_acceptance
                    and result.application_payload is not None
                    and "receipt_verification" in result.application_payload
                ):
                    try:
                        acceptance_receipts = VerifiedAcceptanceReceipts.from_payload(
                            result.application_payload["receipt_verification"]
                        )
                        assert review_source is not None
                        if (
                            acceptance_receipts.result_digest != result.result_digest
                            or acceptance_receipts.review_digest
                            != canonical_digest(review_source.payload())
                            or tuple(
                                str(item.call.call_id) for item in acceptance_receipts.receipts
                            )
                            != decision.evidence_receipt_ids
                        ):
                            raise ValueError
                    except KeyError, TypeError, ValueError:
                        raise SubscriptionDecisionError(
                            "acceptance receipt proof replay differs"
                        ) from None
                    expected_acceptance = {
                        **expected_acceptance,
                        "receipt_verification": acceptance_receipts.payload(),
                    }
                if acceptance_rejected:
                    try:
                        assert review_source is not None and result.application_payload is not None
                        rejected_value = result.application_payload["rejection"]
                        if not isinstance(rejected_value, Mapping):
                            raise TypeError
                        if rejected_value.get("reason") == "receipt_claim_invalid":
                            receipt_issue = AcceptanceReceiptClaimError.from_payload(
                                {
                                    "receipt_id": rejected_value["receipt_id"],
                                    "claim_error": rejected_value["claim_error"],
                                }
                            )
                            if str(receipt_issue.receipt_id) not in decision.evidence_receipt_ids:
                                raise ValueError
                            expected_rejection = {
                                "reason": "receipt_claim_invalid",
                                "repair": result.disposition == "acceptance_repair_queued",
                                "candidate_epoch": review_source.candidate_epoch,
                                **receipt_issue.payload(),
                            }
                        else:
                            rejected_observation = CandidateInspection.from_payload(
                                rejected_value["observation"]
                            )
                            if (
                                rejected_observation.base_sha != review_source.candidate.base_sha
                                or rejected_observation == review_source.candidate
                            ):
                                raise ValueError
                            expected_rejection = {
                                "reason": "candidate_identity_differs",
                                "repair": result.disposition == "acceptance_repair_queued",
                                "reopened_epoch": review_source.candidate_epoch + 1,
                                "observation": rejected_observation.payload(),
                            }
                        if type(rejected_value.get("repair")) is not bool or canonical_digest(
                            rejected_value
                        ) != canonical_digest(expected_rejection):
                            raise ValueError
                    except AssertionError, KeyError, TypeError, ValueError:
                        raise SubscriptionDecisionError(
                            "acceptance rejection replay differs"
                        ) from None
                    assert expected_acceptance is not None
                    acceptance_debit = await self._session.get(SubscriptionRepairDebit, attempt_id)
                    if (acceptance_debit is not None) != (
                        result.disposition == "acceptance_repair_queued"
                    ):
                        raise SubscriptionDecisionError("acceptance repair debit replay differs")
                    if (
                        acceptance_debit is not None
                        and acceptance_debit.next_attempt_id is not None
                    ):
                        acceptance_next_attempt = await self._session.get(
                            SubscriptionAttempt, acceptance_debit.next_attempt_id
                        )
                        if (
                            acceptance_next_attempt is None
                            or acceptance_next_attempt.run_id != run_id
                            or acceptance_next_attempt.task_row_id != parent.task_id
                            or acceptance_next_attempt.attempt_number != attempt.attempt_number + 1
                        ):
                            raise SubscriptionDecisionError(
                                "acceptance repair transfer replay differs"
                            )
                    expected_acceptance = {**expected_acceptance, "rejection": dict(rejected_value)}
                expected_digest = (
                    None if expected_acceptance is None else canonical_digest(expected_acceptance)
                )
                if (
                    not rejected
                    and expected_acceptance is None
                    or result.application_payload != expected_acceptance
                    or result.application_digest != expected_digest
                ):
                    raise SubscriptionDecisionError("acceptance intent replay differs")
            if isinstance(decision, DelegateDecision):
                expected_reopening = None if rejected else reopening_receipt
                expected_digest = (
                    None if expected_reopening is None else canonical_digest(expected_reopening)
                )
                if (
                    result.application_payload != expected_reopening
                    or result.application_digest != expected_digest
                ):
                    raise SubscriptionDecisionError("delegation candidate reopening replay differs")
            if isinstance(decision, ReviewSelection) and not rejected:
                try:
                    stored_intent = dict(result.application_payload or {})
                    observed = None
                    if "observation" in stored_intent:
                        observed = CandidateInspection.from_payload(
                            stored_intent.pop("observation")
                        )
                    if candidate_selected:
                        child = _selected_review_task(parent, decision, envelope, attempt_id)
                        selected_receipt = stored_intent.pop("selection")
                        if (
                            not isinstance(selected_receipt, dict)
                            or type(selected_receipt.get("review_required")) is not bool
                            or observed is None
                            or observed.tree_digest != decision.candidate_tree_digest
                            or (
                                decision.candidate_commit is not None
                                and observed.head_sha != decision.candidate_commit
                            )
                            or canonical_digest(selected_receipt)
                            != canonical_digest(_selection_receipt(decision, child))
                        ):
                            raise ValueError
                        if child is not None:
                            created_review = await self._session.get(
                                SubscriptionTask, child.task_id, populate_existing=True
                            )
                            if (
                                created_review is None
                                or created_review.run_id != parent.run_id
                                or canonical_digest(created_review.payload)
                                != canonical_digest(encode_subscription_record(child))
                            ):
                                raise ValueError
                    if candidate_rejected:
                        rejection_receipt = stored_intent.pop("rejection")
                        if (
                            observed is None
                            or not isinstance(rejection_receipt, dict)
                            or type(rejection_receipt.get("repair")) is not bool
                            or attempt.candidate_epoch is None
                            or canonical_digest(rejection_receipt)
                            != canonical_digest(
                                {
                                    "reason": "candidate_identity_differs",
                                    "repair": result.disposition == "candidate_repair_queued",
                                    "reopened_epoch": attempt.candidate_epoch + 2,
                                }
                            )
                            or (
                                observed.tree_digest == decision.candidate_tree_digest
                                and (
                                    decision.candidate_commit is None
                                    or observed.head_sha == decision.candidate_commit
                                )
                            )
                        ):
                            raise ValueError
                    if (
                        candidate_receipt is None
                        or canonical_digest(stored_intent) != canonical_digest(candidate_receipt)
                        or result.application_payload is None
                        or result.application_digest != canonical_digest(result.application_payload)
                    ):
                        raise ValueError
                except KeyError, TypeError, ValueError:
                    raise SubscriptionDecisionError("candidate intent replay differs") from None
            if isinstance(decision, BoundScopeResponseDecision) and not rejected:
                prepared = await self._scope_response_proof(
                    parent,
                    envelope,
                    decision,
                    result.result_digest,
                    replay_receipt=result.application_payload,
                )
                if (
                    prepared is None
                    or result.application_payload != prepared[1]
                    or result.application_digest != canonical_digest(prepared[1])
                ):
                    raise SubscriptionDecisionError("scope response replay differs")
            if isinstance(decision, BoundReassignDecision) and not rejected:
                reassignment = await reassignment_proof(
                    self._session, parent, envelope, decision, result.result_digest
                )
                if (
                    reassignment is None
                    or result.application_payload != reassignment[2]
                    or result.application_digest != canonical_digest(reassignment[2])
                ):
                    raise SubscriptionDecisionError("reassignment replay differs")
            if isinstance(decision, ForwardFeedbackDecision) and not rejected:
                try:
                    await PostgresSubscriptionFeedbackRepository(
                        self._session
                    ).verify_forward_replay(
                        attempt_id=attempt_id,
                        decision=decision,
                        application=result.application_payload,
                    )
                except Exception as error:
                    raise SubscriptionDecisionError("feedback forwarding replay differs") from error
            if (
                handoff_proposal
                and receipt is not None
                and receipt.get("kind") == "rejected_handoff_claim"
            ):
                assert isinstance(decision, TaskHandoff)
                claim_error = handoff_claim_error(decision, parent, selected_candidate)
                if claim_error is None or receipt.get("claim_error") != claim_error:
                    raise SubscriptionDecisionError("handoff claim rejection replay differs")
            if handoff_proposal and (
                receipt is None
                or result.application_payload != receipt
                or result.application_digest != canonical_digest(receipt)
                or result.result_digest != receipt["result_digest"]
            ):
                raise SubscriptionDecisionError("handoff application replay differs")
            if (
                result.accepted == (rejected or candidate_rejected or acceptance_rejected)
                or attempt.status != "terminal"
                or record is None
                or record.task_row_id != parent.task_id
                or record.attempt_id != attempt_id
                or record.record_type != type(decision).__name__
                or record.payload != encode_subscription_record(decision)
            ):
                raise SubscriptionDecisionError("delegation replay differs")
            if rejected or candidate_rejected or acceptance_rejected:
                failure_record = await self._session.scalar(
                    select(SubscriptionDecisionRecord)
                    .where(
                        SubscriptionDecisionRecord.run_id == run_id,
                        SubscriptionDecisionRecord.idempotency_key
                        == f"decision-rejection:{attempt_id}",
                    )
                    .with_for_update()
                )
                if (
                    failure_record is None
                    or failure_record.task_row_id != parent.task_id
                    or failure_record.attempt_id != attempt_id
                    or failure_record.record_type != "TaskHandoff"
                    or failure_record.payload
                    != encode_subscription_record(
                        _rejection_handoff(
                            attempt,
                            repair=result.disposition
                            in {
                                "decision_repair_queued",
                                "handoff_repair_queued",
                                "candidate_repair_queued",
                                "acceptance_repair_queued",
                            },
                            handoff=handoff_proposal,
                            claim_error=_claim_rejection_reason(receipt),
                            candidate=candidate_rejected,
                            acceptance=isinstance(decision, AcceptDecision),
                            receipt_issue=receipt_issue,
                        )
                    )
                ):
                    raise SubscriptionDecisionError("decision rejection receipt differs")
            return SubscriptionSettlement(result.accepted, result.disposition, True)
        task = await self._session.get(
            SubscriptionTask, parent.task_id, with_for_update=True, populate_existing=True
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask, parent.task_id, with_for_update=True, populate_existing=True
        )
        scheduler = await self._session.get(
            SubscriptionSchedulerRun, run_id, with_for_update=True, populate_existing=True
        )
        effect = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == run_id,
                SubscriptionScheduledEffect.task_id == parent.task_id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        closed_delegation = (
            reopening_receipt is not None
            and scheduler is not None
            and scheduler.candidate_state == "closed"
            and scheduled is not None
            and await PostgresSchedulingRepository(self._session)._is_coordinator(scheduled)
        )
        closed_acceptance = (
            isinstance(decision, AcceptDecision)
            and scheduler is not None
            and scheduler.candidate_state == "closed"
            and scheduled is not None
            and await PostgresSchedulingRepository(self._session)._is_coordinator(scheduled)
        )
        if (
            result.disposition != "decision_pending"
            or result.application_payload is not None
            or result.application_digest is not None
            or result.accepted
            or record is not None
            or run.state not in SUBSCRIPTION_WORK_STATES
            or run.pending_gate is not None
            or await PostgresCommandRepository(
                session=self._session
            ).has_pending_current_control_stop(run_id=run.id, expected_run_version=run.version)
            or run.policy_version != envelope.safety_policy_version
            or not run.worktree_path
            or task is None
            or scheduled is None
            or scheduler is None
            or not scheduler.admitted
            or effect is not None
            or task.run_id != run_id
            or scheduled.run_id != run_id
            or task.state != "reconciling"
            or scheduled.state != "reconciling"
            or attempt.status != "reconciling"
            or task.pause_requested
            or task.cancel_requested
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or task.parent_task_id != parent.parent_task_id
            or (
                worker_decision
                and (
                    scheduled.parent_task_id != parent.parent_task_id
                    or tuple(scheduled.owned_paths)
                    != tuple(policy_path_key(path) for path in parent.owned_paths)
                    or scheduled.read_only != is_read_only(parent.purpose)
                )
            )
            or canonical_digest(task.payload) != attempt.task_digest
            or attempt.task_version is None
            or task.version != await pending_decision_task_version(self._session, attempt, result)
            or not (
                (scheduler.candidate_state == "open" and reopening_receipt is None)
                or closed_delegation
                or closed_acceptance
                or (
                    scheduler.candidate_state == "closed"
                    and handoff_proposal
                    and selected_candidate is not None
                )
            )
            or scheduler.candidate_epoch != attempt.candidate_epoch
            or scheduled.lease_owner != attempt.lease_owner
            or scheduled.lease_generation != attempt.lease_generation
        ):
            raise SubscriptionDecisionError("delegation source is no longer current")
        policy_record = await PostgresProjectRepository(self._session).get_policy(
            run.project_id, run.policy_version
        )
        policy = ProjectPolicy.model_validate(policy_record.document)
        if (policy.id, policy.version) != (run.project_id, run.policy_version) or canonical_digest(
            policy_record.document
        ) != policy_record.policy_digest:
            raise SubscriptionDecisionError("delegation project policy binding differs")
        try:
            resource = WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name or "", policy.database.enabled
            ).worktree_name
        except TypeError, ValueError:
            raise SubscriptionDecisionError("delegation worktree identity differs") from None
        if scheduled.worktree_id != resource:
            raise SubscriptionDecisionError("delegation worktree identity differs")
        if task_acceptance:
            assert isinstance(decision, AcceptDecision)
            if acceptance_receipt is None:
                return await self._reject(task, scheduled, attempt, result, decision, key)
            outcome = await self._finish(
                task, scheduled, attempt, result, decision, key, "queued", disposition
            )
            result.application_payload = acceptance_receipt
            result.application_digest = canonical_digest(acceptance_receipt)
            await self._session.flush()
            return outcome
        if isinstance(decision, AcceptDecision):
            if not closed_acceptance or acceptance_receipt is None:
                return await self._reject(task, scheduled, attempt, result, decision, key)
            pending_task = await self._session.scalar(
                select(SubscriptionTask.id)
                .where(
                    SubscriptionTask.run_id == run_id,
                    SubscriptionTask.id != parent.task_id,
                    SubscriptionTask.state != "terminal",
                )
                .limit(1)
            )
            pending_schedule = await self._session.scalar(
                select(SubscriptionScheduledTask.task_id)
                .where(
                    SubscriptionScheduledTask.run_id == run_id,
                    SubscriptionScheduledTask.task_id != parent.task_id,
                    SubscriptionScheduledTask.state != "terminal",
                )
                .limit(1)
            )
            pending_effect = await self._session.scalar(
                select(SubscriptionScheduledEffect.id)
                .where(
                    SubscriptionScheduledEffect.run_id == run_id,
                    SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                )
                .limit(1)
            )
            if (
                pending_task is not None
                or pending_schedule is not None
                or pending_effect is not None
                or await PostgresSchedulingRepository(self._session)._has_exclusive_effect_barrier(
                    scheduled
                )
            ):
                raise SubscriptionDecisionError("acceptance candidate is not quiescent")
            outcome = await self._finish(
                task, scheduled, attempt, result, decision, key, "blocked", disposition
            )
            result.application_payload = acceptance_receipt
            result.application_digest = canonical_digest(acceptance_receipt)
            await self._session.flush()
            return outcome
        if isinstance(decision, ReviewSelection):
            # Reject an invalid frozen route/child contract before closing the
            # candidate. Finalization rechecks the same contract before creation.
            try:
                _selected_review_task(parent, decision, envelope, attempt_id)
            except KeyError, TypeError, ValueError:
                return await self._reject(task, scheduled, attempt, result, decision, key)
            pending_task = await self._session.scalar(
                select(SubscriptionTask.id)
                .where(
                    SubscriptionTask.run_id == run_id,
                    SubscriptionTask.id != parent.task_id,
                    SubscriptionTask.state != "terminal",
                )
                .limit(1)
            )
            if pending_task is not None:
                raise SubscriptionDecisionError("candidate intent has unfinished tasks")
            if result.application_payload is not None or result.application_digest is not None:
                raise SubscriptionDecisionError("candidate intent receipt already exists")
            assert candidate_receipt is not None
            # Commit this intent and the closed barrier together. It is not a
            # verified candidate or review approval; the primary stays blocked
            # until external observation and application have completed.
            outcome = await self._finish(
                task, scheduled, attempt, result, decision, key, "blocked", disposition
            )
            scheduling = PostgresSchedulingRepository(self._session)
            epoch = await scheduling.begin_candidate(run_id)
            await scheduling.close_candidate(run_id, epoch)
            result.application_payload = candidate_receipt
            result.application_digest = canonical_digest(candidate_receipt)
            await self._session.flush()
            return outcome
        if isinstance(decision, BoundReassignDecision):
            reassignment = await reassignment_proof(
                self._session, parent, envelope, decision, result.result_digest
            )
            if reassignment is None:
                return await self._reject(task, scheduled, attempt, result, decision, key)
            stopped_attempt, updated, reassignment_receipt = reassignment
            await requeue_reassigned_child(
                self._session,
                scheduled,
                decision,
                stopped_attempt,
                updated,
                scheduler.candidate_epoch,
            )
            result.application_payload = reassignment_receipt
            result.application_digest = canonical_digest(reassignment_receipt)
            return await self._finish(
                task, scheduled, attempt, result, decision, key, "blocked", disposition
            )
        if isinstance(decision, ForwardFeedbackDecision):
            try:
                feedback_receipt, primary_state = await PostgresSubscriptionFeedbackRepository(
                    self._session
                ).apply_forward(
                    attempt_id=attempt_id,
                    primary=parent,
                    decision=decision,
                    result_digest=result.result_digest,
                )
            except Exception as error:
                raise SubscriptionDecisionError("feedback forwarding source differs") from error
            outcome = await self._finish(
                task,
                scheduled,
                attempt,
                result,
                decision,
                key,
                primary_state,
                disposition,
            )
            result.application_payload = feedback_receipt
            result.application_digest = canonical_digest(feedback_receipt)
            await self._session.flush()
            return outcome
        if isinstance(decision, BoundScopeResponseDecision):
            prepared = await self._scope_response_proof(
                parent, envelope, decision, result.result_digest
            )
            if prepared is None:
                return await self._reject(task, scheduled, attempt, result, decision, key)
            updated, response_receipt = prepared
            scope_child = await self._session.get(
                SubscriptionTask, decision.task_id, with_for_update=True, populate_existing=True
            )
            child_schedule = await self._session.get(
                SubscriptionScheduledTask,
                decision.task_id,
                with_for_update=True,
                populate_existing=True,
            )
            request_attempt = await self._session.get(
                SubscriptionAttempt, decision.request_attempt_id
            )
            pending_request = await PostgresSchedulingRepository(
                self._session
            ).pending_scope_request(run_id, decision.task_id)
            effects = await self._session.scalar(
                select(SubscriptionScheduledEffect.id)
                .where(
                    SubscriptionScheduledEffect.run_id == run_id,
                    SubscriptionScheduledEffect.task_id == decision.task_id,
                    SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                )
                .limit(1)
            )
            current_child = (
                None if scope_child is None else decode_subscription_record(scope_child.payload)
            )
            if (
                scope_child is None
                or not isinstance(current_child, LogicalTaskContract)
                or child_schedule is None
                or request_attempt is None
                or pending_request is None
                or pending_request[0] != decision.request_attempt_id
                or effects is not None
                or scope_child.run_id != run_id
                or scope_child.parent_task_id != parent.task_id
                or child_schedule.run_id != run_id
                or child_schedule.parent_task_id != parent.task_id
                or scope_child.state != "blocked"
                or child_schedule.state != "blocked"
                or scope_child.pause_requested
                or scope_child.cancel_requested
                or child_schedule.pause_requested
                or child_schedule.cancel_requested
                or child_schedule.lease_owner is not None
                or child_schedule.lease_expires_at is not None
                or child_schedule.worktree_id != scheduled.worktree_id
                or scope_child.version != response_receipt["child_version"]
                or canonical_digest(scope_child.payload) != request_attempt.task_digest
                or child_schedule.lease_generation != request_attempt.lease_generation
                or request_attempt.candidate_epoch != scheduler.candidate_epoch
                or child_schedule.read_only
                or tuple(child_schedule.owned_paths)
                != tuple(policy_path_key(path) for path in current_child.owned_paths)
                or result.application_payload is not None
                or result.application_digest is not None
            ):
                raise SubscriptionDecisionError("scope request is no longer current")
            scope_child.payload = encode_subscription_record(updated)
            scope_child.version += 1
            scope_child.state = child_schedule.state = "queued"
            child_schedule.owned_paths = [policy_path_key(path) for path in updated.owned_paths]
            result.application_payload = response_receipt
            result.application_digest = canonical_digest(response_receipt)
            return await self._finish(
                task, scheduled, attempt, result, decision, key, "queued", disposition
            )
        if isinstance(decision, ScopeRequestDecision):
            if is_read_only(parent.purpose) or len(decision.requested_paths) > 64:
                return await self._reject(task, scheduled, attempt, result, decision, key)
            outcome = await self._finish(
                task, scheduled, attempt, result, decision, key, "blocked", disposition
            )
            await PostgresSchedulingRepository(self._session).wake_parent_for_scope_request(
                run_id, task.id
            )
            return outcome
        if isinstance(decision, TaskHandoff):
            try:
                worktree = ManagedWorktree(
                    identity=WorktreeIdentity.for_run(
                        run.project_id, run.id, run.branch_name or "", policy.database.enabled
                    ),
                    path=Path(run.worktree_path),
                    base_sha=run.base_sha or "",
                )
            except TypeError, ValueError:
                raise SubscriptionDecisionError("handoff managed worktree differs") from None
            proposal = SettledSubscriptionHandoff(
                task=parent,
                handoff=decision,
                result_digest=result.result_digest,
                policy=policy,
                worktree=worktree,
                candidate_epoch=scheduler.candidate_epoch,
                task_version=task.version,
                selected_candidate=selected_candidate,
            )
            if receipt is None:
                return proposal
            if claim_rejection:
                assert observation is not None
                if (
                    proposal != observation.proposal
                    or handoff_claim_error(decision, parent, selected_candidate)
                    != receipt["claim_error"]
                    or result.application_payload is not None
                    or result.application_digest is not None
                    or not await PostgresSubscriptionHandoffFence(self._session).current(
                        observation
                    )
                ):
                    raise SubscriptionDecisionError(
                        "handoff claim or observation is no longer current"
                    )
                result.application_payload, result.application_digest = (
                    receipt,
                    canonical_digest(receipt),
                )
                outcome = await self._reject(task, scheduled, attempt, result, decision, key)
                await self._wake_selected_review_parent(task, selected_candidate)
                if not await PostgresSubscriptionHandoffFence(self._session).release(observation):
                    raise SubscriptionDecisionError("handoff observation release differs")
                await self._session.flush()
                return outcome
            assert observation is not None and verified is not None
            try:
                current_tree = (
                    verified.current_tree_digest
                    if rejection is None
                    else rejection.current_tree_digest
                )
                if current_tree is None:
                    raise ValueError("current tree observation is absent")
                validate_artifact_digest(current_tree)
                validate_artifact_digest(verified.output_digest)
            except TypeError, ValueError:
                raise SubscriptionDecisionError("handoff has no current output proof") from None
            if (
                proposal != observation.proposal
                or (verified.run_id, verified.task_id, verified.attempt_id)
                != (decision.run_id, decision.task_id, decision.attempt_id)
                or verified.policy_version != policy.version
                or verified.task_digest != canonical_digest(encode_subscription_record(parent))
                or verified.handoff_digest != canonical_digest(encode_subscription_record(decision))
                or verified.candidate_tree_digest != decision.candidate_tree_digest
                or (rejection is None and verified.checks_match_snapshot is not True)
                or (
                    rejection is None
                    and selected_candidate is not None
                    and (
                        handoff_claim_error(decision, parent, selected_candidate) is not None
                        or current_tree != selected_candidate.tree_digest
                    )
                )
                or (rejection is not None and rejection.reason(decision) is None)
                or {str(item.call_id) for item in verified.call_proofs}
                != set(decision.evidence_receipt_ids)
                or result.application_payload is not None
                or result.application_digest is not None
                or not await PostgresSubscriptionHandoffFence(self._session).current(observation)
                or not await PostgresSubscriptionHandoffEvidence(self._session).verify(verified)
            ):
                raise SubscriptionDecisionError(
                    "handoff evidence or observation is no longer current"
                )
            if rejection is not None:
                result.application_payload, result.application_digest = (
                    receipt,
                    canonical_digest(receipt),
                )
                outcome = await self._reject(task, scheduled, attempt, result, decision, key)
                await self._wake_selected_review_parent(task, selected_candidate)
                if not await PostgresSubscriptionHandoffFence(self._session).release(observation):
                    raise SubscriptionDecisionError("handoff observation release differs")
                await self._session.flush()
                return outcome
            await subscription.record_decision(decision, idempotency_key=key)
            task.state, attempt.status = "terminal", "terminal"
            task.version += 1
            result.accepted, result.disposition = True, disposition
            result.application_payload, result.application_digest = (
                receipt,
                canonical_digest(receipt),
            )
            await PostgresSchedulingRepository(self._session).reconcile_expired(
                run_id, task.id, retry=False
            )
            await PostgresSubscriptionFeedbackRepository(self._session).requeue_undelivered(
                run_id, task.id
            )
            await self._wake_selected_review_parent(task, selected_candidate)
            if not await PostgresSubscriptionHandoffFence(self._session).release(observation):
                raise SubscriptionDecisionError("handoff observation release differs")
            await self._session.flush()
            return SubscriptionSettlement(True, disposition)
        if isinstance(decision, WaitDecision):
            if len(decision.waiting_on_task_ids) > 64:
                return await self._reject(task, scheduled, attempt, result, decision, key)
            waiting_children = (
                await self._session.scalars(
                    select(SubscriptionScheduledTask)
                    .join(
                        SubscriptionTask, SubscriptionTask.id == SubscriptionScheduledTask.task_id
                    )
                    .where(
                        SubscriptionScheduledTask.task_id.in_(decision.waiting_on_task_ids),
                        SubscriptionScheduledTask.run_id == run_id,
                        SubscriptionScheduledTask.parent_task_id == parent.task_id,
                        SubscriptionTask.run_id == run_id,
                        SubscriptionTask.parent_task_id == parent.task_id,
                        SubscriptionScheduledTask.worktree_id == scheduled.worktree_id,
                    )
                    .with_for_update()
                )
            ).all()
            if len(waiting_children) != len(decision.waiting_on_task_ids):
                return await self._reject(task, scheduled, attempt, result, decision, key)
            state = (
                "queued"
                if all(child.state == "terminal" for child in waiting_children)
                or await PostgresSchedulingRepository(self._session).has_scope_request(
                    run_id, decision.waiting_on_task_ids
                )
                else "blocked"
            )
            return await self._finish(
                task, scheduled, attempt, result, decision, key, state, disposition
            )
        existing_rows = (
            await self._session.scalars(
                select(SubscriptionTask).where(SubscriptionTask.run_id == run_id)
            )
        ).all()
        existing: list[LogicalTaskContract] = []
        for row in existing_rows:
            contract = decode_subscription_record(row.payload)
            if not isinstance(contract, LogicalTaskContract):
                raise SubscriptionDecisionError("delegation task graph differs")
            existing.append(contract)
        children = decision.child_tasks
        try:
            if len(children) > 64:
                raise ValueError("delegation is too large")
            for child in children:
                validate_child_authority(parent, child, envelope)
                if not set(child.named_checks) <= {command.name for command in policy.commands}:
                    raise ValueError("child check is absent from project policy")
            validate_task_dag([*existing, *children])
        except TypeError, ValueError:
            return await self._reject(task, scheduled, attempt, result, decision, key)
        if closed_delegation:
            pending_effect = await self._session.scalar(
                select(SubscriptionScheduledEffect.id)
                .where(
                    SubscriptionScheduledEffect.run_id == run_id,
                    SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                )
                .limit(1)
            )
            pending_task = await self._session.scalar(
                select(SubscriptionScheduledTask.task_id)
                .where(
                    SubscriptionScheduledTask.run_id == run_id,
                    SubscriptionScheduledTask.task_id != parent.task_id,
                    SubscriptionScheduledTask.state != "terminal",
                )
                .limit(1)
            )
            if (
                any(row.id != parent.task_id and row.state != "terminal" for row in existing_rows)
                or pending_effect is not None
                or pending_task is not None
                or await PostgresSchedulingRepository(self._session)._has_exclusive_effect_barrier(
                    scheduled
                )
            ):
                raise SubscriptionDecisionError(
                    "candidate must be quiescent before repair delegation"
                )
            scheduler.candidate_state = "open"
            scheduler.candidate_epoch += 1
        # Persist dependencies before their dependents. All rows stay invisible
        # until the caller commits the entire batch and primary slot release.
        created = {row.id for row in existing_rows}
        pending = list(children)
        while pending:
            ready = [child for child in pending if set(child.dependency_task_ids) <= created]
            if not ready:
                raise SubscriptionDecisionError("delegation dependency order is invalid")
            for child in ready:
                await subscription.create_task(child, idempotency_key=f"{key}:{child.task_id}")
                await PostgresSchedulingRepository(self._session).enqueue(
                    ScheduleTask(
                        run_id=run_id,
                        task_id=child.task_id,
                        worktree_id=scheduled.worktree_id,
                        parent_task_id=parent.task_id,
                        dependency_task_ids=child.dependency_task_ids,
                        owned_paths=child.owned_paths,
                        read_only=is_read_only(child.purpose),
                        max_repairs=child.max_repairs,
                    )
                )
                created.add(child.task_id)
                pending.remove(child)
        outcome = await self._finish(
            task, scheduled, attempt, result, decision, key, "blocked", disposition
        )
        if closed_delegation:
            assert reopening_receipt is not None
            result.application_payload = reopening_receipt
            result.application_digest = canonical_digest(reopening_receipt)
            await self._session.flush()
        return outcome

    async def _scope_response_proof(
        self,
        parent: LogicalTaskContract,
        envelope: ExecutionEnvelope,
        decision: BoundScopeResponseDecision,
        response_digest: str,
        *,
        replay_receipt: dict[str, object] | None = None,
    ) -> tuple[LogicalTaskContract, dict[str, object]] | None:
        """Prove the historical request; current child mutation guards stay separate."""
        request_attempt = await self._session.get(SubscriptionAttempt, decision.request_attempt_id)
        if (
            request_attempt is None
            or request_attempt.run_id != parent.run_id
            or request_attempt.task_row_id != decision.task_id
        ):
            return None
        source = await self._session.get(SubscriptionAttemptResult, decision.request_attempt_id)
        if source is None or source.disposition != "scope_requested" or not source.accepted:
            return None
        proven = await self._apply(decision.request_attempt_id, decision_type=ScopeRequestDecision)
        if (
            not isinstance(proven, SubscriptionSettlement)
            or not proven.accepted
            or not proven.replayed
        ):
            raise SubscriptionDecisionError("scope request proof differs")
        try:
            context = source.result_payload["proposal_context"]
            if not isinstance(context, dict):
                raise TypeError
            original = _decode(context["task"])
            request = _decode(source.result_payload["decision"])
            if not isinstance(original, LogicalTaskContract) or not isinstance(
                request, ScopeRequestDecision
            ):
                raise TypeError
        except KeyError, TypeError, ValueError:
            raise SubscriptionDecisionError("scope request source differs") from None
        if original.parent_task_id != parent.task_id or is_read_only(original.purpose):
            return None
        requested = {policy_path_key(path) for path in request.requested_paths}
        granted = {policy_path_key(path) for path in decision.granted_paths}
        denied = {policy_path_key(path) for path in decision.denied_paths}
        if granted & denied or granted | denied != requested:
            return None
        owned = {policy_path_key(path): path for path in original.owned_paths}
        for path in decision.granted_paths:
            owned.setdefault(policy_path_key(path), path)
        try:
            updated = replace(original, owned_paths=tuple(owned.values()))
            validate_child_authority(parent, updated, envelope)
        except TypeError, ValueError:
            return None
        if any(
            not any(
                policy_path_key(path) == policy_path_key(allowed)
                or policy_path_key(path).startswith(policy_path_key(allowed) + "/")
                for allowed in parent.owned_paths
            )
            for path in updated.owned_paths
        ):
            return None
        request_version = await pending_decision_task_version(
            self._session, request_attempt, source
        )
        if request_version is None:
            raise SubscriptionDecisionError("scope request version is absent")
        from forge.domain.subscription_task_controls import TaskControlConflict
        from forge.persistence.repositories.mutations import MutationRepositoryError
        from forge.persistence.repositories.subscription_idle_task_controls import (
            scope_request_control_version,
        )

        try:
            control_id = (
                None if replay_receipt is None else replay_receipt.get("idle_resume_receipt_id")
            )
            if control_id is not None and not isinstance(control_id, str):
                raise ValueError
            child_version, control_receipt_id = await scope_request_control_version(
                self._session,
                request_attempt,
                request_version + 1,
                replay=replay_receipt is not None,
                receipt_id=None if control_id is None else UUID(control_id),
            )
        except TaskControlConflict, MutationRepositoryError, TypeError, ValueError:
            raise SubscriptionDecisionError(
                "scope request is no longer current"
                if replay_receipt is None
                else "scope task control proof differs"
            ) from None
        receipt: dict[str, object] = {
            "schema_version": 1,
            "kind": "scope_response",
            "response_result_digest": response_digest,
            "request_attempt_id": str(decision.request_attempt_id),
            "request_result_digest": source.result_digest,
            "child_task_id": str(original.task_id),
            "child_version": child_version,
            "prior_task_digest": request_attempt.task_digest,
            "updated_task": encode_subscription_record(updated),
        }
        if control_receipt_id is not None:
            receipt["idle_resume_receipt_id"] = str(control_receipt_id)
        return updated, receipt

    async def _reject(
        self,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        attempt: SubscriptionAttempt,
        result: SubscriptionAttemptResult,
        decision: DelegateDecision
        | AcceptDecision
        | WaitDecision
        | TaskHandoff
        | ScopeRequestDecision
        | BoundScopeResponseDecision
        | BoundReassignDecision
        | ForwardFeedbackDecision
        | ReviewSelection,
        key: str,
    ) -> SubscriptionSettlement:
        # Only semantic rejection reaches here, after current source, control,
        # usage and stopped-launch proofs. Preserve the original provider result.
        repair = (
            scheduled.repairs < scheduled.max_repairs
            and await PostgresSubscriptionBudgetRepository(self._session).try_debit_repair(
                task.run_id, task.id, attempt.id
            )
        )
        subscription = PostgresSubscriptionRepository(self._session)
        await subscription.record_decision(decision, idempotency_key=key)
        record = await self._session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.run_id == task.run_id,
                SubscriptionDecisionRecord.idempotency_key == key,
            )
        )
        if record is None:
            raise SubscriptionDecisionError("rejected decision record was not persisted")
        record.task_row_id, record.attempt_id = task.id, attempt.id
        await subscription.record_decision(
            _rejection_handoff(
                attempt,
                repair=repair,
                handoff=isinstance(decision, TaskHandoff),
                claim_error=_claim_rejection_reason(result.application_payload),
                acceptance=isinstance(decision, AcceptDecision),
            ),
            idempotency_key=f"decision-rejection:{attempt.id}",
        )
        if repair:
            scheduled.repairs += 1
        task.state = "queued" if repair else "terminal"
        task.version += 1
        attempt.status = "terminal"
        result.accepted = False
        if isinstance(decision, TaskHandoff):
            result.disposition = "handoff_repair_queued" if repair else "handoff_rejected"
        else:
            result.disposition = "decision_repair_queued" if repair else "decision_rejected"
        await PostgresSchedulingRepository(self._session).reconcile_expired(
            task.run_id, task.id, retry=repair
        )
        await self._session.flush()
        return SubscriptionSettlement(False, result.disposition)

    async def _finish(
        self,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        attempt: SubscriptionAttempt,
        result: SubscriptionAttemptResult,
        decision: DelegateDecision
        | AcceptDecision
        | WaitDecision
        | ScopeRequestDecision
        | BoundScopeResponseDecision
        | BoundReassignDecision
        | ForwardFeedbackDecision
        | ReviewSelection,
        key: str,
        state: str,
        disposition: str,
    ) -> SubscriptionSettlement:
        if state == "blocked" and await PostgresSubscriptionFeedbackRepository(
            self._session
        ).has_pending_primary(task.run_id, task.id):
            state = "queued"
        await PostgresSubscriptionRepository(self._session).record_decision(
            decision, idempotency_key=key
        )
        record = await self._session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.run_id == task.run_id,
                SubscriptionDecisionRecord.idempotency_key == key,
            )
        )
        if record is None:
            raise SubscriptionDecisionError("delegation record was not persisted")
        record.task_row_id, record.attempt_id = task.id, attempt.id
        task.state = scheduled.state = state
        task.version += 1
        scheduled.lease_owner = scheduled.lease_expires_at = None
        attempt.status = "terminal"
        result.accepted, result.disposition = True, disposition
        await self._session.flush()
        return SubscriptionSettlement(True, disposition)
