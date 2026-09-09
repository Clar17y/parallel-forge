"""Preserve ambiguous startup evidence while isolating only affected runs."""

import asyncio
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.services.recovery import RecoveryError
from forge.application.services.state_engine import LEGAL
from forge.domain.event import RunEvent as Event
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.persistence.models import (
    AgentExecution,
    OperationIntent,
    Run,
    RunCommand,
    Step,
    ToolCall,
)
from forge.persistence.models.recovery import RECOVERY_BARRIER_ID, RecoveryBarrier
from forge.persistence.queries.recovery import unresolved_work
from forge.persistence.repositories.recovery import RecoveryBarrierLost
from forge.persistence.unit_of_work import PostgresUnitOfWork


class StartupInterventionRecovery:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._owner: tuple[UUID, int] | None = None

    async def wait_for_owners(self) -> None:
        """Drain previously admitted owners; startup never steals a live effect."""
        self._owner = None
        while True:
            async with self._factory() as session:
                owner = await self._barrier(session)
                if self._owner is None:
                    self._owner = owner
                if owner != self._owner:
                    raise RecoveryBarrierLost("startup recovery owner changed")
                if not await self._active(session):
                    return
            await asyncio.sleep(0.25)

    async def quarantine(self) -> tuple[UUID, ...]:
        """Persist causal intervention before ordinary admission can reopen."""
        async with PostgresUnitOfWork(self._factory) as work:
            if self._owner is None or await self._barrier(work.session) != self._owner:
                raise RecoveryBarrierLost("startup intervention has no current owner")
            if await self._active(work.session):
                raise RecoveryError("startup recovery still has active owners")
            run_ids = tuple(
                await work.session.scalars(
                    select(Run.id).where(unresolved_work(Run.id)).order_by(Run.id)
                )
            )
            proofs: dict[UUID, dict[str, object]] = {}
            for run_id in run_ids:
                run = await work.runs.get_for_update(run_id)
                payload = await self._proof(work.session, run_id)
                proofs[run_id] = payload
                events = await work.events.list_after(run_id, 0)
                digest = canonical_digest(payload)
                if any(
                    e.event_type == "run.recovery_intervention"
                    and canonical_digest(e.payload) == digest
                    for e in events
                ):
                    continue
                if RunState.AWAITING_HUMAN_INTERVENTION in LEGAL[run.state]:
                    await work.runs.intervene(
                        run_id,
                        run.version,
                        "run.recovery_intervention",
                        payload,
                        actor_class="worker",
                    )
                elif run.state in {
                    RunState.PAUSED,
                    RunState.AWAITING_HUMAN_INTERVENTION,
                    RunState.COMPLETED,
                    RunState.FAILED,
                    RunState.CANCELLED,
                }:
                    # Preserve an operator pause or terminal business result;
                    # the unresolved evidence still blocks ordinary dispatch.
                    await work.events.append(
                        Event(
                            run_id=run_id,
                            run_version=run.version,
                            event_type="run.recovery_intervention",
                            actor_class="worker",
                            payload=payload,
                        )
                    )
                else:
                    raise RecoveryError("unresolved work has an unsupported run state")
            # Fail closed if an owner or durable item appeared during the sweep.
            if await self._active(work.session) or await self._barrier(work.session) != self._owner:
                raise RecoveryError("startup recovery changed during intervention")
            current = tuple(
                await work.session.scalars(
                    select(Run.id).where(unresolved_work(Run.id)).order_by(Run.id)
                )
            )
            if current != run_ids:
                raise RecoveryError("startup recovery discovered additional unresolved runs")
            for run_id in current:
                if await self._proof(work.session, run_id) != proofs[run_id]:
                    raise RecoveryError("startup recovery evidence changed during intervention")
            await work.commit()
            return run_ids

    @staticmethod
    async def _barrier(session: AsyncSession) -> tuple[UUID, int]:
        row = (
            await session.execute(
                select(
                    RecoveryBarrier.owner_id,
                    RecoveryBarrier.generation,
                ).where(
                    RecoveryBarrier.id == RECOVERY_BARRIER_ID,
                    RecoveryBarrier.required.is_(True),
                    RecoveryBarrier.owner_id.is_not(None),
                    RecoveryBarrier.expires_at > func.clock_timestamp(),
                )
            )
        ).one_or_none()
        if row is None:
            raise RecoveryBarrierLost("startup recovery barrier is not owned")
        return row[0], row[1]

    @staticmethod
    async def _active(session: AsyncSession) -> bool:
        return bool(
            await session.scalar(
                select(
                    or_(
                        exists(
                            select(RunCommand.id).where(
                                RunCommand.status == "LEASED",
                                RunCommand.lease_expires_at > func.clock_timestamp(),
                            )
                        ),
                        exists(
                            select(OperationIntent.id).where(
                                OperationIntent.execution_owner.is_not(None),
                                OperationIntent.execution_lease_expires_at > func.clock_timestamp(),
                            )
                        ),
                    )
                )
            )
        )

    @staticmethod
    async def _proof(session: AsyncSession, run_id: UUID) -> dict[str, object]:
        operations = (
            await session.execute(
                select(
                    OperationIntent.id,
                    OperationIntent.request_digest,
                )
                .where(
                    OperationIntent.run_id == run_id,
                    OperationIntent.status.in_(("PENDING", "NEEDS_RECONCILIATION")),
                )
                .order_by(OperationIntent.id)
            )
        ).all()
        return {
            "reason": "startup_outcome_unresolved",
            "operation_ids": [str(row.id) for row in operations],
            "operation_digests": {str(row.id): row.request_digest for row in operations},
            "execution_ids": [
                str(value)
                for value in await session.scalars(
                    select(AgentExecution.id)
                    .where(
                        AgentExecution.run_id == run_id,
                        AgentExecution.status == "RUNNING",
                    )
                    .order_by(AgentExecution.id)
                )
            ],
            "step_ids": [
                str(value)
                for value in await session.scalars(
                    select(Step.id)
                    .where(
                        Step.run_id == run_id,
                        Step.status == "RUNNING",
                    )
                    .order_by(Step.id)
                )
            ],
            "tool_call_ids": [
                str(value)
                for value in await session.scalars(
                    select(ToolCall.id)
                    .where(
                        ToolCall.run_id == run_id,
                        ToolCall.status == "RUNNING",
                    )
                    .order_by(ToolCall.id)
                )
            ],
        }
