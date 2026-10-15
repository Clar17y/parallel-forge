"""Settle subscription attempts left reconciling after their run ended.

A run cancelled (or failed or completed) while a worker attempt was in flight
leaves that attempt ``reconciling``: its result was fenced (termination not yet
proved when it settled) or stale (the run was no longer current). Nothing
applies decisions to an ended run, so the attempt, its task and its scheduler
slot stayed held forever, and run teardown refused because the run was never
quiescent.

Settlement here is evidence bound: every client launch of the attempt must have
a terminal record whose stop was confirmed, and no admitted or reconciling
effect may remain for the attempt's lease. The provider result stays unapplied;
only the bookkeeping that holds the slot is closed.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.run import RunState
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionTask,
)

ENDED_RUN_STATES = frozenset(
    state.value for state in (RunState.CANCELLED, RunState.FAILED, RunState.COMPLETED)
)


class PostgresRunEndSettlement:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def candidate_run_ids(self, after: UUID | None, limit: int) -> tuple[UUID, ...]:
        query = (
            select(SubscriptionAttempt.run_id)
            .join(Run, Run.id == SubscriptionAttempt.run_id)
            .where(
                SubscriptionAttempt.status == "reconciling",
                Run.state.in_(ENDED_RUN_STATES),
            )
            .distinct()
            .order_by(SubscriptionAttempt.run_id)
            .limit(limit)
        )
        if after is not None:
            query = query.where(SubscriptionAttempt.run_id > after)
        return tuple((await self._session.scalars(query)).all())

    async def settle_run(self, run_id: UUID) -> int:
        """Close every provably stopped attempt of one ended run; return how many."""
        run = await self._session.get(Run, run_id, with_for_update=True)
        if run is None or run.state not in ENDED_RUN_STATES:
            return 0
        attempts = (
            await self._session.scalars(
                select(SubscriptionAttempt)
                .where(
                    SubscriptionAttempt.run_id == run_id,
                    SubscriptionAttempt.status == "reconciling",
                )
                .order_by(SubscriptionAttempt.id)
                .with_for_update()
            )
        ).all()
        settled = 0
        for attempt in attempts:
            if not await self._stopped(attempt) or await self._has_open_effect(attempt):
                continue
            attempt.status = "terminal"
            task = await self._session.get(
                SubscriptionTask, attempt.task_row_id, with_for_update=True
            )
            if task is not None and task.state == "reconciling":
                task.state = "terminal"
                task.version += 1
            scheduled = (
                await self._session.execute(
                    select(SubscriptionScheduledTask)
                    .where(
                        SubscriptionScheduledTask.run_id == run_id,
                        SubscriptionScheduledTask.task_id == attempt.task_row_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                scheduled is not None
                and scheduled.state in {"leased", "reconciling"}
                and scheduled.lease_owner == attempt.lease_owner
                and scheduled.lease_generation == attempt.lease_generation
            ):
                # The run has ended, so the task never runs again; release its slot
                # without waking parents into a run that cannot schedule them.
                scheduled.state = "terminal"
                scheduled.lease_owner = None
                scheduled.lease_expires_at = None
            settled += 1
        await self._session.flush()
        return settled

    async def _stopped(self, attempt: SubscriptionAttempt) -> bool:
        launches = (
            await self._session.scalars(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == attempt.id
                )
            )
        ).all()
        return bool(launches) and all(
            launch.state == "terminal"
            and isinstance(launch.terminal_payload, dict)
            and launch.terminal_payload.get("stop_confirmed") is True
            for launch in launches
        )

    async def _has_open_effect(self, attempt: SubscriptionAttempt) -> bool:
        effect = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == attempt.run_id,
                SubscriptionScheduledEffect.task_id == attempt.task_row_id,
                SubscriptionScheduledEffect.lease_owner == attempt.lease_owner,
                SubscriptionScheduledEffect.lease_generation == attempt.lease_generation,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        return effect is not None


__all__ = ["ENDED_RUN_STATES", "PostgresRunEndSettlement"]
