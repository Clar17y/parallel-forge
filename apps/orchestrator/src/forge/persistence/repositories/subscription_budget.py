"""Run-serialized accounting without retaining locks across provider execution."""

from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_budget import AttemptUsageReceipt, SubscriptionUsage
from forge.domain.subscription import (
    AttemptTelemetry,
    BudgetPool,
    LogicalTaskContract,
    SpecialistPurpose,
    TaskBudget,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_budget import (
    AttemptCharge,
    UsageAmounts,
    budget_ceiling,
    project_attempt_charge,
)
from forge.domain.subscription_execution import run_allows_subscription_attempt
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionBudgetPool,
    SubscriptionBudgetReservation,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from forge.persistence.models.subscription_usage import (
    SubscriptionAttemptConsumption,
    SubscriptionAttemptReservation,
)


class SubscriptionBudgetConflict(ValueError):
    """Admission or immutable accounting identity could not be proved."""


def _decode[T](payload: Mapping[str, object], expected: type[T]) -> T:
    value = decode_subscription_record(payload)
    if not isinstance(value, expected):
        raise SubscriptionBudgetConflict("stored budget record type conflicts")
    return value


def _amounts(payload: Mapping[str, object]) -> UsageAmounts:
    if set(payload) != set(UsageAmounts().values()):
        raise SubscriptionBudgetConflict("stored usage fields conflict")
    return UsageAmounts(**cast(dict[str, int | None], dict(payload)))


class PostgresSubscriptionBudgetRepository:
    """One debit per admitted attempt; child allocations are not usage debits.

    Repair units belong to scheduling, so this API accepts zero repair units.
    Callers commit admission before launch and settle even failed/no-result work.
    Unknown-policy violations and overages are stored before later admissions stop.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_run(self, run_id: UUID) -> None:
        if await self._session.get(Run, run_id, with_for_update=True) is None:
            raise SubscriptionBudgetConflict("run does not exist")

    async def _contracts(
        self, run_id: UUID, task_id: UUID, attempt_id: UUID
    ) -> tuple[LogicalTaskContract, LogicalTaskContract, SubscriptionTask, SubscriptionAttempt]:
        contract, primary, task = await self._task_contracts(run_id, task_id)
        attempt = await self._session.get(SubscriptionAttempt, attempt_id, with_for_update=True)
        if attempt is None or attempt.run_id != run_id or attempt.task_row_id != task_id:
            raise SubscriptionBudgetConflict("attempt budget lineage conflicts")
        return contract, primary, task, attempt

    async def _task_contracts(
        self, run_id: UUID, task_id: UUID
    ) -> tuple[LogicalTaskContract, LogicalTaskContract, SubscriptionTask]:
        await self._lock_run(run_id)
        task = await self._session.get(SubscriptionTask, task_id, with_for_update=True)
        if task is None or task.run_id != run_id:
            raise SubscriptionBudgetConflict("task budget lineage conflicts")
        contract = _decode(task.payload, LogicalTaskContract)
        roots = (
            await self._session.scalars(
                select(SubscriptionTask).where(
                    SubscriptionTask.run_id == run_id, SubscriptionTask.parent_task_id.is_(None)
                )
            )
        ).all()
        primaries = [
            value
            for row in roots
            if (value := _decode(row.payload, LogicalTaskContract)).purpose
            is SpecialistPurpose.PRIMARY
        ]
        if len(primaries) != 1:
            raise SubscriptionBudgetConflict("one frozen primary budget is required")
        if contract.run_id != run_id or contract.task_id != task_id:
            raise SubscriptionBudgetConflict("task budget identity conflicts")
        return contract, primaries[0], task

    async def fit_reservation(
        self, run_id: UUID, task_id: UUID, ceiling: TaskBudget
    ) -> TaskBudget | None:
        """Bound one attempt by remaining frozen budgets under the run lock.

        This is admission policy, not a reservation or remaining provider quota.
        The caller must reserve the returned budget in this same transaction.
        """
        if ceiling.max_provider_attempts != 1 or ceiling.max_repairs != 0:
            raise SubscriptionBudgetConflict("fit one attempt and no repair unit")
        contract, primary, _ = await self._task_contracts(run_id, task_id)
        legacy = await self._session.scalar(
            select(SubscriptionBudgetReservation.id)
            .where(
                SubscriptionBudgetReservation.run_id == run_id,
                SubscriptionBudgetReservation.status != "released",
            )
            .limit(1)
        )
        if legacy is not None:
            return None
        pending_repairs = await self._session.scalar(
            select(func.count())
            .select_from(SubscriptionRepairDebit)
            .join(
                SubscriptionAttempt,
                SubscriptionAttempt.id == SubscriptionRepairDebit.attempt_id,
            )
            .where(
                SubscriptionAttempt.run_id == run_id,
                SubscriptionAttempt.task_row_id == task_id,
                SubscriptionRepairDebit.next_attempt_id.is_(None),
            )
        )
        if pending_repairs not in (0, 1):
            raise SubscriptionBudgetConflict("ambiguous pending repair slot")
        proposed = budget_ceiling(ceiling).values()
        for scope, _, budget in await self._budget_scopes(run_id, task_id, contract, primary):
            current = await self._usage(run_id, scope)
            if (
                ceiling.billing_mode is not budget.billing_mode
                or current.policy_violations
                or (
                    current.uncertain_attempts
                    and current.uncertain_attempts
                    >= budget.unknown_telemetry_policy.max_uncertain_attempts
                )
            ):
                return None
            used = (current.consumed + current.outstanding).values()
            # reserve_attempt atomically transfers this task's existing repair
            # slot. Do not count it twice or borrow another task's reservation.
            attempts = used["provider_attempts"]
            if attempts is None or attempts < pending_repairs:
                raise SubscriptionBudgetConflict("pending repair accounting conflicts")
            used["provider_attempts"] = attempts - pending_repairs
            for name, limit in budget_ceiling(budget).values().items():
                if limit is None:
                    continue
                amount = used[name]
                if amount is None or amount > limit:
                    return None
                remaining = limit - amount
                prior = proposed[name]
                proposed[name] = remaining if prior is None else min(prior, remaining)
        duration = proposed["duration_ms"]
        if duration is None or duration < 1000 or proposed["provider_attempts"] != 1:
            return None
        return replace(
            ceiling,
            max_duration_seconds=duration // 1000,
            max_tool_calls=cast(int, proposed["tool_calls"]),
            max_named_checks=cast(int, proposed["named_checks"]),
            max_input_tokens=proposed["input_tokens"],
            max_output_tokens=proposed["output_tokens"],
            max_cost_minor=proposed["cost_minor"],
        )

    async def _budget_scopes(
        self,
        run_id: UUID,
        task_id: UUID,
        contract: LogicalTaskContract,
        primary: LogicalTaskContract,
    ) -> tuple[tuple[UUID | None, str, TaskBudget], ...]:
        scopes: list[tuple[UUID | None, str, TaskBudget]] = [(task_id, "task", contract.budget)]
        run_budget = primary.budget
        pools = (
            await self._session.scalars(
                select(SubscriptionBudgetPool).where(SubscriptionBudgetPool.run_id == run_id)
            )
        ).all()
        for pool in pools:
            if pool.task_row_id is None:
                run_budget = _decode(pool.payload, BudgetPool).total_budget
            elif pool.task_row_id == task_id:
                scopes.append(
                    (task_id, "task_pool", _decode(pool.payload, BudgetPool).total_budget)
                )
        scopes.append((None, "run", run_budget))
        return tuple(scopes)

    async def _usage(self, run_id: UUID, task_id: UUID | None) -> SubscriptionUsage:
        query = (
            select(SubscriptionAttemptReservation, SubscriptionAttemptConsumption)
            .outerjoin(
                SubscriptionAttemptConsumption,
                SubscriptionAttemptConsumption.attempt_id
                == SubscriptionAttemptReservation.attempt_id,
            )
            .where(SubscriptionAttemptReservation.run_id == run_id)
        )
        if task_id is not None:
            query = query.where(SubscriptionAttemptReservation.task_id == task_id)
        consumed, outstanding = UsageAmounts(), UsageAmounts()
        uncertain, violations = 0, set()
        for reservation, consumption in (await self._session.execute(query)).all():
            if consumption is None:
                outstanding += budget_ceiling(_decode(reservation.budget_payload, TaskBudget))
            else:
                consumed += _amounts(consumption.charged)
                uncertain += int(consumption.uncertain)
                violations.update(consumption.policy_violations)
        repair_query = (
            select(SubscriptionRepairDebit)
            .join(SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionRepairDebit.attempt_id)
            .where(SubscriptionAttempt.run_id == run_id)
        )
        if task_id is not None:
            repair_query = repair_query.where(SubscriptionAttempt.task_row_id == task_id)
        repair_rows = (await self._session.scalars(repair_query)).all()
        consumed += UsageAmounts(repairs=len(repair_rows))
        outstanding += UsageAmounts(
            provider_attempts=sum(row.next_attempt_id is None for row in repair_rows)
        )
        return SubscriptionUsage(
            consumed=consumed,
            outstanding=outstanding,
            uncertain_attempts=uncertain,
            policy_violations=tuple(sorted(violations)),
        )

    async def usage(self, run_id: UUID, task_id: UUID | None = None) -> SubscriptionUsage:
        await self._lock_run(run_id)
        if task_id is not None:
            task = await self._session.get(SubscriptionTask, task_id)
            if task is None or task.run_id != run_id:
                raise SubscriptionBudgetConflict("task usage lineage conflicts")
        return await self._usage(run_id, task_id)

    async def reserved_budget(self, run_id: UUID, task_id: UUID, attempt_id: UUID) -> TaskBudget:
        """Read an exact outstanding reservation; callers hold the run lock."""
        row = await self._session.get(SubscriptionAttemptReservation, attempt_id)
        if row is None or row.run_id != run_id or row.task_id != task_id:
            raise SubscriptionBudgetConflict("attempt has no matching reservation")
        if await self._session.get(SubscriptionAttemptConsumption, attempt_id) is not None:
            raise SubscriptionBudgetConflict("attempt reservation is already settled")
        budget = _decode(row.budget_payload, TaskBudget)
        if budget.max_provider_attempts != 1 or budget.max_repairs != 0:
            raise SubscriptionBudgetConflict("attempt reservation units differ")
        return budget

    async def reserve_attempt(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        reservation: TaskBudget,
        *,
        idempotency_key: str,
    ) -> None:
        if (
            not isinstance(reservation, TaskBudget)
            or reservation.max_provider_attempts != 1
            or reservation.max_repairs != 0
        ):
            raise SubscriptionBudgetConflict(
                "reserve one attempt; repairs are debited at scheduling"
            )
        if type(idempotency_key) is not str or not idempotency_key or len(idempotency_key) > 255:
            raise SubscriptionBudgetConflict("invalid reservation key")
        contract, primary, task, attempt = await self._contracts(run_id, task_id, attempt_id)
        payload = encode_subscription_record(reservation)
        existing = await self._session.get(SubscriptionAttemptReservation, attempt_id)
        if existing is not None:
            if (
                existing.run_id != run_id
                or existing.task_id != task_id
                or existing.idempotency_key != idempotency_key
                or existing.budget_payload != payload
            ):
                raise SubscriptionBudgetConflict("attempt reservation replay conflicts")
            return
        run = await self._session.get(Run, run_id)
        if run is None or not run_allows_subscription_attempt(run.state, run.pending_gate):
            raise SubscriptionBudgetConflict("run is not eligible for attempt admission")
        latest = await self._session.scalar(
            select(func.max(SubscriptionAttempt.attempt_number)).where(
                SubscriptionAttempt.run_id == run_id, SubscriptionAttempt.task_row_id == task_id
            )
        )
        if latest != attempt.attempt_number:
            raise SubscriptionBudgetConflict("attempt was superseded")
        if (
            task.pause_requested
            or task.cancel_requested
            or task.state not in {"queued", "running"}
            or attempt.status not in {"queued", "running"}
        ):
            raise SubscriptionBudgetConflict("attempt is not eligible for admission")
        duplicate = await self._session.scalar(
            select(SubscriptionAttemptReservation.attempt_id).where(
                SubscriptionAttemptReservation.run_id == run_id,
                SubscriptionAttemptReservation.idempotency_key == idempotency_key,
            )
        )
        legacy = await self._session.scalar(
            select(SubscriptionBudgetReservation.id)
            .where(
                SubscriptionBudgetReservation.run_id == run_id,
                SubscriptionBudgetReservation.status != "released",
            )
            .limit(1)
        )
        if duplicate is not None or legacy is not None:
            raise SubscriptionBudgetConflict(
                "reservation key or unresolved legacy accounting conflicts"
            )
        # Transfer the previously reserved repair slot into this actual attempt.
        # The caller's admission transaction rolls this back if any budget fails.
        pending_repairs = (
            await self._session.scalars(
                select(SubscriptionRepairDebit)
                .join(
                    SubscriptionAttempt,
                    SubscriptionAttempt.id == SubscriptionRepairDebit.attempt_id,
                )
                .where(
                    SubscriptionAttempt.run_id == run_id,
                    SubscriptionAttempt.task_row_id == task_id,
                    SubscriptionRepairDebit.next_attempt_id.is_(None),
                )
                .with_for_update()
            )
        ).all()
        if len(pending_repairs) > 1:
            raise SubscriptionBudgetConflict("ambiguous pending repair slot")
        if pending_repairs:
            parent = await self._session.get(SubscriptionAttempt, pending_repairs[0].attempt_id)
            if parent is None or parent.attempt_number + 1 != attempt.attempt_number:
                raise SubscriptionBudgetConflict("repair attempt lineage conflicts")
            pending_repairs[0].next_attempt_id = attempt_id
            await self._session.flush()
        for scope, _, budget in await self._budget_scopes(run_id, task_id, contract, primary):
            current = await self._usage(run_id, scope)
            if (
                reservation.billing_mode is not budget.billing_mode
                or current.policy_violations
                or (
                    current.uncertain_attempts
                    and current.uncertain_attempts
                    >= budget.unknown_telemetry_policy.max_uncertain_attempts
                )
                or not (
                    current.consumed + current.outstanding + budget_ceiling(reservation)
                ).fits_within(budget)
            ):
                raise SubscriptionBudgetConflict("aggregate attempt budget exhausted")
        self._session.add(
            SubscriptionAttemptReservation(
                attempt_id=attempt_id,
                run_id=run_id,
                task_id=task_id,
                idempotency_key=idempotency_key,
                budget_payload=payload,
            )
        )
        await self._session.flush()

    async def settle_attempt(
        self, run_id: UUID, task_id: UUID, attempt_id: UUID, telemetry: AttemptTelemetry | None
    ) -> AttemptUsageReceipt:
        if telemetry is not None and not isinstance(telemetry, AttemptTelemetry):
            raise TypeError("attempt telemetry must be typed or unknown")
        contract, primary, _, _ = await self._contracts(run_id, task_id, attempt_id)
        reservation = await self._session.get(SubscriptionAttemptReservation, attempt_id)
        if reservation is None or reservation.run_id != run_id or reservation.task_id != task_id:
            raise SubscriptionBudgetConflict("attempt has no matching reservation")
        payload = None if telemetry is None else encode_subscription_record(telemetry)
        existing = await self._session.get(SubscriptionAttemptConsumption, attempt_id)
        if existing is not None:
            if existing.telemetry_payload != payload:
                raise SubscriptionBudgetConflict("attempt consumption replay conflicts")
            return self._receipt(existing)
        charge = project_attempt_charge(
            _decode(reservation.budget_payload, TaskBudget), telemetry, repair=False
        )
        violations = []
        for _, scope, budget in await self._budget_scopes(run_id, task_id, contract, primary):
            try:
                budget.unknown_telemetry_policy.validate_telemetry(
                    telemetry if telemetry is not None else AttemptTelemetry(), budget.billing_mode
                )
            except ValueError:
                violations.append(f"{scope}:unknown_telemetry")
        consumption = SubscriptionAttemptConsumption(
            attempt_id=attempt_id,
            telemetry_payload=payload,
            observed=dict(charge.observed.values()),
            charged=dict(charge.charged.values()),
            unknown_fields=list(charge.unknown_fields),
            exceeded_fields=list(charge.exceeded_fields),
            policy_violations=violations,
            uncertain=telemetry is None
            or not (
                telemetry.is_token_telemetry_known
                and telemetry.is_cost_known
                and telemetry.is_quota_known
            ),
        )
        self._session.add(consumption)
        await self._session.flush()
        return self._receipt(consumption)

    @staticmethod
    def _receipt(row: SubscriptionAttemptConsumption) -> AttemptUsageReceipt:
        return AttemptUsageReceipt(
            attempt_id=row.attempt_id,
            telemetry=None
            if row.telemetry_payload is None
            else _decode(row.telemetry_payload, AttemptTelemetry),
            charge=AttemptCharge(
                observed=_amounts(row.observed),
                charged=_amounts(row.charged),
                unknown_fields=tuple(row.unknown_fields),
                exceeded_fields=tuple(row.exceeded_fields),
            ),
        )

    async def try_debit_repair(self, run_id: UUID, task_id: UUID, attempt_id: UUID) -> bool:
        contract, primary, _, _ = await self._contracts(run_id, task_id, attempt_id)
        if await self._session.get(SubscriptionRepairDebit, attempt_id) is not None:
            return True
        if await self._session.get(SubscriptionAttemptConsumption, attempt_id) is None:
            raise SubscriptionBudgetConflict("repair requires settled attempt usage")
        for scope, _, budget in await self._budget_scopes(run_id, task_id, contract, primary):
            current = await self._usage(run_id, scope)
            if (
                current.policy_violations
                or (
                    current.uncertain_attempts
                    and current.uncertain_attempts
                    >= budget.unknown_telemetry_policy.max_uncertain_attempts
                )
                or not (
                    current.consumed
                    + current.outstanding
                    + UsageAmounts(repairs=1, provider_attempts=1)
                ).fits_within(budget)
            ):
                return False
        self._session.add(SubscriptionRepairDebit(attempt_id=attempt_id))
        await self._session.flush()
        # A repair reserves its next provider attempt immediately. Reconcile any
        # accepted, unbound feedback under the same run lock so that consuming
        # the final cumulative slot cannot leave a receipt pending forever.
        from forge.persistence.repositories.subscription_feedback import (
            PostgresSubscriptionFeedbackRepository,
        )

        await PostgresSubscriptionFeedbackRepository(self._session).close_exhausted(run_id)
        return True
