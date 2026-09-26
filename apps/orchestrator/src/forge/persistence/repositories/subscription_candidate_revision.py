"""Reopen a current PR-gate candidate without changing accepted history."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_candidate_revision import AcceptanceRevision
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_validation import AcceptanceValidationRepair
from forge.domain.subscription import HandoffStatus, TaskHandoff
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.subscription_validation import acceptance_revision_source
from forge.persistence.repositories.subscription_validation_repair import (
    _reopen_candidate,
    _verify_reopening,
)


def _feedback_parts(feedback: str | None) -> tuple[str, ...]:
    """Keep complete bounded feedback, including a line longer than the summary."""
    if feedback is None:
        return ()
    parts: list[str] = []
    start = 0
    while start < len(feedback):
        end = min(start + 9000, len(feedback))
        if end < len(feedback):
            newline = feedback.rfind("\n", start, end)
            if newline >= start:
                end = newline + 1
        parts.append(feedback[start:end])
        start = end
    return tuple(
        f"Operator feedback (part {index + 1}/{len(parts)}):\n{part}"
        for index, part in enumerate(parts)
    )


def _handoff(
    source: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance,
    revision: AcceptanceRevision,
    repaired: bool,
) -> TaskHandoff:
    reason = (
        "Candidate contents changed after primary acceptance."
        if revision.reason == "content_drift"
        else "The operator requested changes to the accepted candidate."
    )
    return TaskHandoff(
        run_id=source.decision.run_id,
        task_id=source.decision.task_id,
        attempt_id=source.attempt_id,
        status=HandoffStatus.BLOCKED if repaired else HandoffStatus.REPAIRS_EXHAUSTED,
        summary=f"{reason} Previous PR evidence: {revision.pr_evidence_digest}. "
        + (
            "Revise within the approved scope and obtain fresh selection and acceptance."
            if repaired
            else "The cumulative repair budget is exhausted; human intervention is required."
        ),
        residual_concerns=_feedback_parts(revision.feedback),
    )


async def reopen_acceptance_revision(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    command_id: UUID,
    revision: AcceptanceRevision,
    repair_limit: int,
) -> AcceptanceValidationRepair:
    current, _ = await acceptance_revision_source(session, proposal.attempt_id)
    run = await PostgresRunRepository(session).get_for_update(proposal.decision.run_id)
    if (
        current != proposal
        or type(repair_limit) is not int
        or not 0 <= repair_limit <= proposal.policy.local_remediation_limit
        or run.pending_evidence_digest != revision.pr_evidence_digest
        or revision.observation.base_sha != proposal.worktree.base_sha
        or (
            revision.reason == "operator_feedback"
            and revision.observation != proposal.review.candidate
        )
    ):
        raise SubscriptionDecisionError("candidate revision source changed")
    return await _reopen_candidate(
        session,
        proposal,
        key=f"candidate-revision:{command_id}",
        local_limit=repair_limit if revision.reason == "content_drift" else None,
        handoff=lambda repaired: _handoff(proposal, revision, repaired),
    )


async def verify_acceptance_revision(
    session: AsyncSession,
    source: RetainedSubscriptionAcceptance,
    command_id: UUID,
    revision: AcceptanceRevision,
    receipt: AcceptanceValidationRepair,
) -> None:
    await _verify_reopening(
        session,
        source,
        f"candidate-revision:{command_id}",
        _handoff(source, revision, receipt.repaired),
        receipt,
    )
