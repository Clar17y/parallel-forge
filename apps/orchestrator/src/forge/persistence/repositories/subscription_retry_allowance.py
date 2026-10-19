"""Finite owner-authorized retry counts; monetary and execution ceilings stay intact."""

from dataclasses import asdict, dataclass, replace
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.event import RunEvent
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    BudgetPool,
    LogicalTaskContract,
    TaskBudget,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_budget import UsageAmounts, budget_ceiling
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionBudgetPool, SubscriptionTask
from forge.persistence.models.subscription_recovery import SubscriptionContractRevision
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository


@dataclass(frozen=True)
class RetryAllowance:
    contract: LogicalTaskContract
    pools: tuple[tuple[UUID, dict[str, object]], ...]
    max_repairs: int
    evidence: dict[str, object]
    changes: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def binding(self) -> str:
        return canonical_digest(self.evidence)


def _counts(budget: TaskBudget) -> dict[str, int]:
    return {
        "provider_attempts": budget.max_provider_attempts,
        "repairs": budget.max_repairs,
        "uncertain_attempts": budget.unknown_telemetry_policy.max_uncertain_attempts,
    }


class SubscriptionRetryAllowance:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def propose(
        self,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        *,
        locked: bool,
    ) -> RetryAllowance | None:
        contract = decode_subscription_record(task.payload)
        if (
            not isinstance(contract, LogicalTaskContract)
            or contract.parent_task_id is not None
            or scheduled.max_repairs != contract.max_repairs
        ):
            return None
        scopes = await PostgresSubscriptionBudgetRepository(self._session).recovery_scopes(
            task.run_id,
            task.id,
            locked=locked,
        )
        rows = (
            await self._session.scalars(
                select(SubscriptionBudgetPool)
                .where(
                    SubscriptionBudgetPool.run_id == task.run_id,
                    (SubscriptionBudgetPool.task_row_id.is_(None))
                    | (SubscriptionBudgetPool.task_row_id == task.id),
                )
                .order_by(SubscriptionBudgetPool.id)
            )
        ).all()
        pools_by_scope = {"run" if row.task_row_id is None else "task_pool": row for row in rows}
        proposals: dict[str, TaskBudget] = {}
        evidence: dict[str, dict[str, object]] = {}
        changes = []
        remaining = []
        for name, budget, usage in scopes:
            if usage.policy_violations:
                return None
            used = usage.consumed + usage.outstanding
            if used.repairs is None or used.provider_attempts is None:
                return None
            before = _counts(budget)
            needed = {
                "repairs": used.repairs + 1,
                "provider_attempts": used.provider_attempts + 1,
                "uncertain_attempts": usage.uncertain_attempts + 1
                if usage.uncertain_attempts
                else before["uncertain_attempts"],
            }
            after = {key: max(before[key], needed[key]) for key in before}
            # This is one slot, never a catch-up grant over inconsistent limits.
            if any(after[key] - before[key] > 1 for key in before):
                return None
            proposed = replace(
                budget,
                max_repairs=after["repairs"],
                max_provider_attempts=after["provider_attempts"],
                unknown_telemetry_policy=replace(
                    budget.unknown_telemetry_policy,
                    max_uncertain_attempts=after["uncertain_attempts"],
                ),
            )
            # Admission requires at least a one-second reservation. A count
            # grant must not queue work that the unchanged time ceiling denies.
            if not (
                used + UsageAmounts(duration_ms=1000, repairs=1, provider_attempts=1)
            ).fits_within(proposed):
                return None
            proposals[name] = proposed
            evidence[name] = {
                "before": encode_subscription_record(budget),
                "after": encode_subscription_record(proposed),
                "usage": asdict(usage),
            }
            limits = budget_ceiling(budget).values()
            for dimension in (
                "duration_ms",
                "input_tokens",
                "output_tokens",
                "cost_minor",
                "tool_calls",
                "named_checks",
            ):
                ceiling, charged = limits[dimension], used.values()[dimension]
                available = (
                    "no configured cap"
                    if ceiling is None
                    else str(ceiling - charged)
                    if charged is not None
                    else "unknown"
                )
                remaining.append(
                    f"{name} {dimension}: remaining {available}; charged {charged if charged is not None else 'unknown'}."
                )
        limit = max(scheduled.max_repairs, scheduled.repairs + 1)
        if limit - scheduled.max_repairs > 1:
            return None
        updated = proposals["task"]
        if "run" not in pools_by_scope:
            run_budget = proposals["run"]
            updated = replace(
                updated,
                max_provider_attempts=max(
                    updated.max_provider_attempts, run_budget.max_provider_attempts
                ),
                max_repairs=max(updated.max_repairs, run_budget.max_repairs),
                unknown_telemetry_policy=replace(
                    updated.unknown_telemetry_policy,
                    max_uncertain_attempts=max(
                        updated.unknown_telemetry_policy.max_uncertain_attempts,
                        run_budget.unknown_telemetry_policy.max_uncertain_attempts,
                    ),
                ),
            )
        revised = replace(contract, budget=updated, max_repairs=max(contract.max_repairs, limit))
        if "run" not in pools_by_scope:
            # These scopes share one stored primary ceiling. Show the exact
            # common edit even when children consumed some of the run budget.
            proposals["task"] = proposals["run"] = updated
        for name, budget, _ in scopes:
            before, after = _counts(budget), _counts(proposals[name])
            evidence[name]["after"] = encode_subscription_record(proposals[name])
            changes.append(
                f"{name} limits: "
                + "; ".join(f"{key} {before[key]} → {after[key]}" for key in before)
            )
        pool_payloads = []
        for name, row in pools_by_scope.items():
            pool = decode_subscription_record(row.payload)
            if not isinstance(pool, BudgetPool):
                return None
            pool_payloads.append(
                (row.id, encode_subscription_record(replace(pool, total_budget=proposals[name])))
            )
        if (
            revised == contract
            and all(
                payload == next(row.payload for row in rows if row.id == row_id)
                for row_id, payload in pool_payloads
            )
            and limit == scheduled.max_repairs
        ):
            return None
        warnings = (
            "Known and unknown usage remains retained; unknown spending is not zero.",
            "Spend, token, duration, tool and check limits, billing and unknown-value permissions remain unchanged.",
            "Only one additional repair/provider/uncertain slot is allowed where its count limit was exhausted.",
        ) + tuple(remaining)
        changes.append(f"scheduled repairs {scheduled.max_repairs} → {limit}")
        return RetryAllowance(
            revised,
            tuple(pool_payloads),
            limit,
            {
                "scopes": evidence,
                "scheduled_before": scheduled.max_repairs,
                "scheduled_after": limit,
                "pool_sources": {str(row.id): row.payload for row in rows},
                "contract_before": task.payload,
                "contract_after": encode_subscription_record(revised),
            },
            tuple(changes),
            warnings,
        )

    async def apply(
        self,
        proposal: RetryAllowance,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        *,
        source_attempt_id: UUID,
        approval_id: UUID,
        plan_digest: str,
        actor_id: UUID,
        binding: str,
    ) -> None:
        previous = await self._session.scalar(
            select(func.max(SubscriptionContractRevision.revision)).where(
                SubscriptionContractRevision.task_id == task.id
            )
        )
        payload = encode_subscription_record(proposal.contract)
        self._session.add(
            SubscriptionContractRevision(
                run_id=task.run_id,
                task_id=task.id,
                revision=(previous or 0) + 1,
                source_attempt_id=source_attempt_id,
                approval_id=approval_id,
                plan_digest=plan_digest,
                original_contract_digest=canonical_digest(task.payload),
                original_contract_payload=task.payload,
                contract_digest=canonical_digest(payload),
                contract_payload=payload,
            )
        )
        task.payload = payload
        scheduled.max_repairs = proposal.max_repairs
        for row_id, pool_payload in proposal.pools:
            row = await self._session.get(SubscriptionBudgetPool, row_id)
            assert row is not None
            row.payload = pool_payload
        run = await self._session.get(Run, task.run_id)
        assert run is not None
        await PostgresEventRepository(self._session).append(
            RunEvent(
                run_id=run.id,
                run_version=run.version,
                event_type="subscription_retry_allowance_extended",
                actor_class="operator",
                actor_id=actor_id,
                payload={
                    **proposal.evidence,
                    "action": "retry_failed_attempt_with_allowance",
                    "task_id": str(task.id),
                    "source_attempt_id": str(source_attempt_id),
                    "binding": binding,
                    "warnings": list(proposal.warnings),
                },
            )
        )
