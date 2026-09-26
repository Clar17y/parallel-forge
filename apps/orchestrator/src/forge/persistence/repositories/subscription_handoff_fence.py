"""Acquire/release bounded observation fences in caller-owned transactions."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_handoff import (
    HandoffObservation,
    SettledSubscriptionHandoff,
)
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
from forge.persistence.repositories.runs import PostgresRunRepository


class PostgresSubscriptionHandoffFence:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin(self, proposal: SettledSubscriptionHandoff, token: UUID) -> HandoffObservation:
        """Caller has just revalidated the pending source under its run lock."""
        if not isinstance(token, UUID) or token.int == 0:
            raise ValueError("handoff observation token must be a non-nil UUID")
        worktree = proposal.worktree.identity.worktree_name
        await PostgresRunRepository(self._session).get_for_update(proposal.task.run_id)
        row = await self._session.get(
            SubscriptionHandoffFence, worktree, with_for_update=True, populate_existing=True
        )
        now = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime):
            raise SubscriptionDecisionError("database clock is unavailable")
        if row is not None and row.expires_at > now:
            if (
                row.token != token
                or row.run_id != proposal.task.run_id
                or row.attempt_id != proposal.handoff.attempt_id
                or row.result_digest != proposal.result_digest
            ):
                raise SubscriptionDecisionError("worktree observation is already active")
            return HandoffObservation(proposal, token, row.expires_at)
        if row is not None and row.token == token:
            raise SubscriptionDecisionError("expired observation requires a fresh token")
        # Leased siblings may keep doing reasoning/reads, but every controlled
        # writer/command must enter an effect through the scheduler. Drain only
        # actual effects, avoiding deadlock between stopped child handoffs.
        pending = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .join(
                SubscriptionScheduledTask,
                (SubscriptionScheduledTask.run_id == SubscriptionScheduledEffect.run_id)
                & (SubscriptionScheduledTask.task_id == SubscriptionScheduledEffect.task_id),
            )
            .where(
                SubscriptionScheduledTask.worktree_id == worktree,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        if pending is not None:
            raise SubscriptionDecisionError("worktree effects are still outstanding")
        expires_at = now + timedelta(seconds=60)
        if row is None:
            row = SubscriptionHandoffFence(worktree_id=worktree)
            self._session.add(row)
        row.run_id = proposal.task.run_id
        row.attempt_id = proposal.handoff.attempt_id
        row.token, row.result_digest, row.expires_at = token, proposal.result_digest, expires_at
        await self._session.flush()
        return HandoffObservation(proposal, token, expires_at)

    async def current(self, observation: HandoffObservation) -> bool:
        """Pin the unexpired exact fence for the caller's final application."""
        proposal = observation.proposal
        await PostgresRunRepository(self._session).get_for_update(proposal.task.run_id)
        row = await self._session.get(
            SubscriptionHandoffFence,
            proposal.worktree.identity.worktree_name,
            with_for_update=True,
            populate_existing=True,
        )
        now = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime):
            raise SubscriptionDecisionError("database clock is unavailable")
        return (
            row is not None
            and (row.run_id, row.attempt_id, row.token, row.result_digest, row.expires_at)
            == (
                proposal.task.run_id,
                proposal.handoff.attempt_id,
                observation.token,
                proposal.result_digest,
                observation.expires_at,
            )
            and row.expires_at > now
        )

    async def release(self, observation: HandoffObservation) -> bool:
        # Expired observers can clean up their own row, never a replacement's.
        proposal = observation.proposal
        await PostgresRunRepository(self._session).get_for_update(proposal.task.run_id)
        row = await self._session.get(
            SubscriptionHandoffFence,
            proposal.worktree.identity.worktree_name,
            with_for_update=True,
            populate_existing=True,
        )
        if row is None or (
            row.run_id,
            row.attempt_id,
            row.token,
            row.result_digest,
            row.expires_at,
        ) != (
            proposal.task.run_id,
            proposal.handoff.attempt_id,
            observation.token,
            proposal.result_digest,
            observation.expires_at,
        ):
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True
