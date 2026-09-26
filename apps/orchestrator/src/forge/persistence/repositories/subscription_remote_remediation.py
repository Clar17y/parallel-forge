"""Atomic bounded primary reopening for an authenticated remote failure."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_validation import AcceptanceValidationRepair
from forge.domain.agent import UntrustedContent, UntrustedSourceKind
from forge.domain.artifact import validate_artifact_digest
from forge.domain.subscription import HandoffStatus, TaskHandoff
from forge.persistence.repositories.subscription_validation import acceptance_remote_source
from forge.persistence.repositories.subscription_validation_repair import (
    _reopen_candidate,
    _verify_reopening,
)


def _handoff(
    source: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance,
    pr_digest: str,
    feedback: UntrustedContent,
    repaired: bool,
) -> TaskHandoff:
    validate_artifact_digest(pr_digest)
    if (
        feedback.source_kind is not UntrustedSourceKind.CHECK
        or feedback.source_reference != feedback.content_digest
        or feedback.truncated
    ):
        raise SubscriptionDecisionError("remote repair feedback differs")
    # The complete observation stays in its artifact. Keep the primary handoff
    # compact and explicitly identify a bounded excerpt when a remote log is large.
    excerpt = feedback.content.encode("utf-8")[:65_536].decode("utf-8", errors="ignore")
    parts = tuple(excerpt[index : index + 9000] for index in range(0, len(excerpt), 9000))
    return TaskHandoff(
        run_id=source.decision.run_id,
        task_id=source.decision.task_id,
        attempt_id=source.attempt_id,
        status=HandoffStatus.FAILED if repaired else HandoffStatus.REPAIRS_EXHAUSTED,
        summary=f"Remote PR checks or reviews require repair. PR evidence: {pr_digest}. "
        + f"Remote observation: {feedback.source_reference}. "
        + ("Remote context is a truncated excerpt. " if excerpt != feedback.content else "")
        + (
            "Repair within the approved scope and obtain fresh selection and acceptance."
            if repaired
            else "The cumulative repair budget is exhausted; human intervention is required."
        ),
        residual_concerns=tuple(
            f"Untrusted remote observation (part {index + 1}/{len(parts)}):\n{part}"
            for index, part in enumerate(parts)
        ),
    )


async def reopen_acceptance_remote(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    command_id: UUID,
    pr_digest: str,
    feedback: UntrustedContent,
) -> AcceptanceValidationRepair:
    current, _ = await acceptance_remote_source(session, proposal.attempt_id)
    if current != proposal:
        raise SubscriptionDecisionError("remote repair acceptance source changed")
    return await _reopen_candidate(
        session,
        proposal,
        key=f"remote-repair:{command_id}",
        local_limit=None,
        handoff=lambda repaired: _handoff(proposal, pr_digest, feedback, repaired),
    )


async def verify_acceptance_remote(
    session: AsyncSession,
    source: RetainedSubscriptionAcceptance,
    command_id: UUID,
    pr_digest: str,
    feedback: UntrustedContent,
    receipt: AcceptanceValidationRepair,
) -> None:
    await _verify_reopening(
        session,
        source,
        f"remote-repair:{command_id}",
        _handoff(source, pr_digest, feedback, receipt.repaired),
        receipt,
    )
