"""Caller-owned PostgreSQL transaction for immutable epic launch bindings."""

from collections.abc import Sequence
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.epic_run_bridge import (
    DependencyEvidence,
    EpicAttempt,
    EpicAttemptNotFound,
    EpicExecutionNotFound,
    EpicExecutionSnapshot,
)
from forge.domain.subscription import (
    TaskBudget,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.persistence.models.epic_brainstorm import BrainstormBudgetLedger
from forge.persistence.models.epic_run_bridge import (
    EpicChildBudgetHold,
    EpicExecution,
    EpicExecutionControl,
    EpicItemAttempt,
)
from forge.persistence.repositories.epic_budget import PostgresEpicBudgetRepository


class PostgresEpicRunBridgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_active_execution(self, epic_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(EpicExecution.id)
                .outerjoin(
                    EpicExecutionControl, EpicExecutionControl.execution_id == EpicExecution.id
                )
                .where(
                    EpicExecution.epic_id == epic_id,
                    (EpicExecutionControl.execution_id.is_(None))
                    | EpicExecutionControl.state.not_in(("SUCCEEDED", "CANCELLED")),
                )
                .limit(1)
            )
            is not None
        )

    async def control_state(self, execution_id: UUID) -> str | None:
        row = await self._session.get(EpicExecutionControl, execution_id)
        return row.state if row is not None else None

    async def note_owner_child_admission(self, execution_id: UUID) -> None:
        """A late manual child revokes a previously settled control projection."""
        row = await self._session.get(EpicExecutionControl, execution_id, with_for_update=True)
        if row is not None and row.state not in (
            "ACTIVE",
            "PAUSE_REQUESTED",
            "RESUME_REQUESTED",
            "CANCEL_REQUESTED",
        ):
            row.version += 1
            row.state = "BLOCKED"
            row.blocker_code = "uncontrolled_child"
            await self._session.flush()

    async def child_budget_blockers(
        self, epic_id: UUID, project_id: UUID, *, ceiling: TaskBudget, hold: TaskBudget
    ) -> list[str]:
        await self._session.execute(
            insert(BrainstormBudgetLedger)
            .values(
                epic_id=epic_id,
                project_id=project_id,
                ceiling=encode_subscription_record(ceiling),
                version=1,
            )
            .on_conflict_do_nothing()
        )
        ledger = await self._session.get(BrainstormBudgetLedger, epic_id, with_for_update=True)
        if ledger is None or ledger.project_id != project_id:
            raise ValueError("epic budget project binding conflicts")
        current = decode_subscription_record(ledger.ceiling)
        if not isinstance(current, TaskBudget):
            raise TypeError("epic budget ceiling is invalid")
        totals = await PostgresEpicBudgetRepository(self._session, legacy_hold=hold).totals(epic_id)
        return totals.blockers(current, hold, frozenset(ledger.disabled_dimensions))

    async def create_child_hold(self, attempt: EpicAttempt, budget: TaskBudget) -> None:
        self._session.add(
            EpicChildBudgetHold(
                attempt_id=attempt.attempt_id,
                epic_id=attempt.epic_id,
                run_id=attempt.run_id,
                budget_payload=encode_subscription_record(budget),
                effects_settled=False,
            )
        )
        await self._session.flush()

    async def create_execution(
        self,
        *,
        epic_id: UUID,
        brief_revision_id: UUID,
        brief_digest: str,
        graph_revision_id: UUID,
        graph_digest: str,
    ) -> EpicExecutionSnapshot:
        row = EpicExecution(
            id=uuid4(),
            epic_id=epic_id,
            brief_revision_id=brief_revision_id,
            brief_digest=brief_digest,
            graph_revision_id=graph_revision_id,
            graph_digest=graph_digest,
        )
        self._session.add(row)
        await self._session.flush()
        self._session.add(EpicExecutionControl(execution_id=row.id, epic_id=epic_id))
        await self._session.flush()
        await self._session.refresh(row)
        return _execution(row)

    async def get_execution(self, execution_id: UUID) -> EpicExecutionSnapshot:
        row = await self._session.get(EpicExecution, execution_id)
        if row is None:
            raise EpicExecutionNotFound("execution was not found")
        return _execution(row)

    async def create_attempt(self, attempt: EpicAttempt) -> None:
        data = attempt.model_dump(mode="python", exclude={"attempt_id", "dependency_evidence"})
        self._session.add(
            EpicItemAttempt(
                id=attempt.attempt_id,
                **data,
                dependency_evidence=[
                    value.model_dump(mode="json") for value in attempt.dependency_evidence
                ],
            )
        )
        await self._session.flush()

    async def get_attempt(self, epic_id: UUID, attempt_id: UUID) -> EpicAttempt:
        row = await self._session.scalar(
            select(EpicItemAttempt).where(
                EpicItemAttempt.epic_id == epic_id, EpicItemAttempt.id == attempt_id
            )
        )
        if row is None:
            raise EpicAttemptNotFound("epic attempt was not found")
        return _attempt(row)

    async def list_attempts(
        self, epic_id: UUID, *, execution_id: UUID | None = None
    ) -> Sequence[EpicAttempt]:
        stmt = (
            select(EpicItemAttempt)
            .where(EpicItemAttempt.epic_id == epic_id)
            .order_by(EpicItemAttempt.created_at, EpicItemAttempt.id)
        )
        if execution_id is not None:
            stmt = stmt.where(EpicItemAttempt.execution_id == execution_id)
        rows = (await self._session.scalars(stmt)).all()
        return [_attempt(row) for row in rows]


def _attempt(row: EpicItemAttempt) -> EpicAttempt:
    return EpicAttempt(
        attempt_id=row.id,
        execution_id=row.execution_id,
        epic_id=row.epic_id,
        item_id=row.item_id,
        attempt_number=row.attempt_number,
        item_disposition=cast(Literal["required", "deferred"], row.item_disposition),
        actor_id=row.actor_id,
        expected_epic_version=row.expected_epic_version,
        actual_epic_version=row.actual_epic_version,
        task_id=row.task_id,
        run_id=row.run_id,
        brief_revision_id=row.brief_revision_id,
        brief_digest=row.brief_digest,
        graph_revision_id=row.graph_revision_id,
        graph_digest=row.graph_digest,
        item_digest=row.item_digest,
        context_digest=row.context_digest,
        task_digest=row.task_digest,
        base_ref=row.base_ref,
        base_sha=row.base_sha,
        owner_override=row.owner_override,
        override_note=row.override_note,
        blocker_codes=row.blocker_codes,
        dependency_evidence=[
            DependencyEvidence.model_validate(value) for value in row.dependency_evidence
        ],
        created_at=row.created_at,
    )


def _execution(row: EpicExecution) -> EpicExecutionSnapshot:
    return EpicExecutionSnapshot(
        execution_id=row.id,
        epic_id=row.epic_id,
        brief_revision_id=row.brief_revision_id,
        brief_digest=row.brief_digest,
        graph_revision_id=row.graph_revision_id,
        graph_digest=row.graph_digest,
        created_at=row.created_at,
    )
