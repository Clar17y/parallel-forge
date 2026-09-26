"""One transaction owns each settled decision's application and replay."""

from collections.abc import Callable
from uuid import UUID

from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_handoff import (
    HandoffObservation,
    RejectedSubscriptionHandoff,
    SettledSubscriptionHandoff,
    VerifiedSubscriptionHandoff,
)
from forge.application.ports.unit_of_work import UnitOfWork


class SubscriptionDecisionApplication:
    def __init__(self, work_factory: Callable[[], UnitOfWork]) -> None:
        self._factory = work_factory

    async def prepare_acceptance(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.prepare_acceptance(attempt_id)
            await work.commit()
            return result

    async def finalize_review_selection(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.finalize_review_selection(attempt_id)
            await work.commit()
            return result

    async def reject_candidate_mismatch(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.reject_candidate_mismatch(attempt_id)
            await work.commit()
            return result

    async def prepare_review_selection(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.prepare_review_selection(attempt_id)
            await work.commit()
            return result

    async def apply_scope_request(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.apply_scope_request(attempt_id)
            await work.commit()
            return result

    async def apply_scope_response(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.apply_scope_response(attempt_id)
            await work.commit()
            return result

    async def reject_handoff_claim(self, observation: HandoffObservation) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.reject_handoff_claim(observation)
            await work.commit()
            return result

    async def reject_handoff(
        self, observation: HandoffObservation, proof: RejectedSubscriptionHandoff
    ) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.reject_handoff(observation, proof)
            await work.commit()
            return result

    async def handoff_replay(self, attempt_id: UUID) -> SubscriptionSettlement | None:
        async with self._factory() as work:
            result = await work.subscription_decisions.handoff_replay(attempt_id)
            await work.rollback()
            return result

    async def apply_handoff(
        self, observation: HandoffObservation, proof: VerifiedSubscriptionHandoff
    ) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.apply_handoff(observation, proof)
            await work.commit()
            return result

    async def begin_handoff_observation(self, attempt_id: UUID, token: UUID) -> HandoffObservation:
        async with self._factory() as work:
            observation = await work.subscription_decisions.begin_handoff_observation(
                attempt_id, token
            )
            await work.commit()
            return observation

    async def release_handoff_observation(self, observation: HandoffObservation) -> bool:
        async with self._factory() as work:
            released = await work.subscription_decisions.release_handoff_observation(observation)
            await work.commit()
            return released

    async def handoff_proposal(self, attempt_id: UUID) -> SettledSubscriptionHandoff:
        async with self._factory() as work:
            proposal = await work.subscription_decisions.handoff_proposal(attempt_id)
            await work.rollback()
            return proposal

    async def apply_delegation(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.apply_delegation(attempt_id)
            await work.commit()
            return result

    async def apply_wait(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.apply_wait(attempt_id)
            await work.commit()
            return result

    async def apply_reassignment(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.apply_reassignment(attempt_id)
            await work.commit()
            return result

    async def apply_feedback(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.apply_feedback(attempt_id)
            await work.commit()
            return result
