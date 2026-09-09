"""Worker-facing handlers for review delivery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.review import ReviewService
from forge.application.services.review_decision import ReviewDecision, ReviewDecisionService
from forge.domain.command import CommandEnvelope


class ReviewHandler:
    """Execute a review command and atomically deliver its decision.

    A decided command is replayed through ``ReviewDecisionService`` directly.
    The service owns all evidence, lease, candidate, and causal-event checks;
    this adapter only avoids invoking the reviewer after a decision event.
    """

    def __init__(
        self, review_service: ReviewService, decision_service: ReviewDecisionService
    ) -> None:
        self._review = review_service
        self._decision = decision_service

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> ReviewDecision:
        decided = any(
            event.event_type == "run.review_decided"
            and event.payload.get("source_command_id") == str(command.id)
            for event in await work.events.list_after(command.run_id, 0)
        )
        if not decided:
            await self._review.execute(command, work)
        return await self._decision.decide(command, work)


ReviewCommandHandler = Callable[[CommandEnvelope, UnitOfWork], Awaitable[ReviewDecision]]

__all__ = ["ReviewCommandHandler", "ReviewHandler"]
