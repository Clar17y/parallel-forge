"""Inspect frozen Git contents and apply their retained review selection."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from forge.application.ports.subscription_candidate import (
    CandidateInspection,
    PreparedReviewSelection,
)
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication


class SubscriptionCandidateApplication:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        snapshot: Callable[[PreparedReviewSelection], Awaitable[GitWorkingTreeSnapshot]],
    ) -> None:
        self._factory, self._snapshot = work_factory, snapshot

    async def apply(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            prepared = await work.subscription_decisions.prepare_review_selection(attempt_id)
            if prepared.disposition != "candidate_prepared":
                await work.commit()
                return prepared
            proposal = await work.subscription_decisions.review_selection_proposal(attempt_id)
            await work.commit()
        # A retained observation belongs to the frozen selection. Git failures
        # leave preparation recoverable, without another provider invocation.
        snapshot = await self._snapshot(proposal) if proposal.inspection is None else None
        async with self._factory() as work:
            prepared = await work.subscription_decisions.prepare_review_selection(attempt_id)
            if prepared.disposition != "candidate_prepared":
                await work.commit()
                return prepared
            current = await work.subscription_decisions.review_selection_proposal(attempt_id)
            observed = current.inspection
            if snapshot is not None:
                observed = await work.subscription_decisions.record_candidate_inspection(
                    proposal, snapshot
                )
            if observed is None:
                raise ValueError("prepared candidate observation is required")
            selection = current.selection
            matches = observed.tree_digest == selection.candidate_tree_digest and (
                selection.candidate_commit is None
                or observed.head_sha == selection.candidate_commit
            )
            if matches:
                result = await work.subscription_decisions.finalize_review_selection(attempt_id)
            else:
                result = await work.subscription_decisions.reject_candidate_mismatch(attempt_id)
            await work.commit()
            return result


class SubscriptionCandidateInspection:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        snapshot: Callable[[PreparedReviewSelection], Awaitable[GitWorkingTreeSnapshot]],
    ) -> None:
        self._factory, self._snapshot = work_factory, snapshot

    async def inspect(self, attempt_id: UUID) -> CandidateInspection:
        await SubscriptionDecisionApplication(self._factory).prepare_review_selection(attempt_id)
        async with self._factory() as work:
            proposal = await work.subscription_decisions.review_selection_proposal(attempt_id)
            await work.rollback()
        if proposal.inspection is not None:
            return proposal.inspection
        snapshot = await self._snapshot(proposal)
        async with self._factory() as work:
            observed = await work.subscription_decisions.record_candidate_inspection(
                proposal, snapshot
            )
            await work.commit()
            return observed
