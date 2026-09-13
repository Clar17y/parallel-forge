"""Observe current outputs outside transactions, then apply exact durable proof."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_handoff import (
    HandoffObservation,
    RejectedSubscriptionHandoff,
    SettledSubscriptionHandoff,
    handoff_claim_error,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_handoff import SubscriptionHandoffVerifier


class SubscriptionHandoffApplication:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        verifier: SubscriptionHandoffVerifier,
        snapshot: Callable[[SettledSubscriptionHandoff], Awaitable[GitWorkingTreeSnapshot]],
    ) -> None:
        self._decisions = SubscriptionDecisionApplication(work_factory)
        self._verifier, self._snapshot = verifier, snapshot

    async def apply(self, attempt_id: UUID) -> SubscriptionSettlement:
        replay = await self._decisions.handoff_replay(attempt_id)
        if replay is not None:
            return replay
        observation = await self._decisions.begin_handoff_observation(attempt_id, uuid4())
        try:
            proposal = observation.proposal
            if (
                handoff_claim_error(proposal.handoff, proposal.task, proposal.selected_candidate)
                is not None
            ):
                return await self._decisions.reject_handoff_claim(observation)
            snapshot = await self._snapshot(proposal)
            resource = proposal.worktree.identity.worktree_name
            proof = await self._verifier.assess(
                proposal.handoff,
                task=proposal.task,
                policy_version=proposal.policy.version,
                worktree_id=resource,
                resource_id=resource,
                base_sha=proposal.worktree.base_sha,
                current_snapshot=snapshot,
            )
            if proof is None:
                raise SubscriptionDecisionError("handoff evidence could not be verified")
            if isinstance(proof, RejectedSubscriptionHandoff):
                return await self._decisions.reject_handoff(observation, proof)
            return await self._decisions.apply_handoff(observation, proof)
        finally:
            await self._release(observation)

    async def _release(self, observation: HandoffObservation) -> None:
        cleanup = asyncio.create_task(self._decisions.release_handoff_observation(observation))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        await cleanup
        if cancelled:
            raise asyncio.CancelledError
