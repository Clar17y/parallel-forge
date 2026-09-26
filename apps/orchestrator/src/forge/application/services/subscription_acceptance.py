"""Inspect a closed candidate for final acceptance without a DB transaction over Git."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from forge.application.ports.subscription_acceptance import PreparedSubscriptionAcceptance
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication


class SubscriptionAcceptanceInspection:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        snapshot: Callable[[PreparedSubscriptionAcceptance], Awaitable[GitWorkingTreeSnapshot]],
    ) -> None:
        self._factory, self._snapshot = work_factory, snapshot

    async def inspect(self, attempt_id: UUID) -> CandidateInspection:
        await SubscriptionDecisionApplication(self._factory).prepare_acceptance(attempt_id)
        async with self._factory() as work:
            proposal = await work.subscription_decisions.acceptance_proposal(attempt_id)
            await work.rollback()
        # Even a retry observes Git again; a retained observation cannot certify
        # that out-of-band edits have not occurred since the previous call.
        snapshot = await self._snapshot(proposal)
        async with self._factory() as work:
            observed = await work.subscription_decisions.record_acceptance_inspection(
                proposal, snapshot
            )
            await work.commit()
            return observed

    async def reject_mismatch(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            prior = await work.subscription_decisions.prepare_acceptance(attempt_id)
            if not prior.accepted:
                await work.commit()
                return prior
            proposal = await work.subscription_decisions.acceptance_proposal(attempt_id)
            await work.commit()
        # A stored contradiction already invalidates the frozen epoch. Otherwise
        # observe again so drift after an earlier matching inspection can recover.
        snapshot = (
            await self._snapshot(proposal)
            if proposal.inspection is None or proposal.inspection == proposal.review.candidate
            else None
        )
        async with self._factory() as work:
            result = await work.subscription_decisions.reject_acceptance_mismatch(
                proposal, snapshot
            )
            await work.commit()
            return result
