"""Build an honest subject report from durable source facts and context observations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.task_usage import UsageReport, UsageSourceReport, UsageTotals
from forge.domain.task_usage_contract import BaselineReference, CheckpointRecord
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.task_usage import TaskUsageObservation


async def build_usage_report(
    session: AsyncSession,
    *,
    totals: UsageTotals,
    policy_revision: int | None,
    baseline: BaselineReference | None,
    checkpoint: CheckpointRecord | None,
    warning: str | None = None,
) -> UsageReport | None:
    """Project per-source totals; never sum independent context windows."""

    facts = totals.source_facts
    if not facts:
        return None
    unit_ids = [fact.binding.work_unit_id for fact in facts]
    peak_rows = (
        await session.execute(
            select(
                TaskUsageObservation.work_unit_id,
                func.max(TaskUsageObservation.context_occupied_tokens),
            )
            .where(TaskUsageObservation.work_unit_id.in_(unit_ids))
            .group_by(TaskUsageObservation.work_unit_id)
        )
    ).all()
    observed_peaks: dict[UUID, int] = {
        identity: peak for identity, peak in peak_rows if peak is not None
    }
    task_ids = [
        fact.binding.source.parent_id
        for fact in facts
        if fact.binding.source.kind == "subscription_attempt"
    ]
    task_ancestry: dict[UUID, bool] = {}
    if totals.subject.kind == "run" and task_ids:
        task_rows = (
            await session.execute(
                select(SubscriptionTask.id, SubscriptionTask.parent_task_id)
                .where(
                    SubscriptionTask.run_id == totals.subject.subject_id,
                    SubscriptionTask.id.in_(task_ids),
                )
            )
        ).all()
        task_ancestry = {task_id: parent_id is not None for task_id, parent_id in task_rows}
    sources = tuple(
        UsageSourceReport(
            binding=fact.binding,
            phase=fact.binding.admitted_phase_id,
            role=fact.binding.effective_route.members[0].role,
            delegated=(
                task_ancestry.get(fact.binding.source.parent_id)
                if fact.binding.source.parent_id is not None
                else None
            ),
            input_tokens=fact.input_tokens,
            output_tokens=fact.output_tokens,
            completeness=fact.completeness,
            context_occupied_tokens=fact.context_occupied_tokens,
            context_peak_tokens=observed_peaks.get(fact.binding.work_unit_id),
            context_capacity_tokens=fact.context_capacity_tokens,
            compaction_count=fact.compaction_count,
        )
        for fact in facts
    )
    only = sources[0] if len(sources) == 1 else None
    compactions = [source.compaction_count for source in sources]
    return UsageReport(
        binding=baseline.binding if baseline is not None else facts[0].binding,
        policy_revision=policy_revision,
        baseline=baseline,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cached_input_tokens=totals.cached_input_tokens,
        reasoning_output_tokens=totals.reasoning_output_tokens,
        context_occupied_tokens=only.context_occupied_tokens if only else None,
        context_capacity_tokens=only.context_capacity_tokens if only else None,
        context_peak_tokens=max(observed_peaks.values()) if observed_peaks else None,
        compaction_count=(
            sum(value for value in compactions if value is not None)
            if compactions and all(value is not None for value in compactions)
            else None
        ),
        freshness=(
            "final"
            if totals.source_count and totals.final_source_count == totals.source_count
            else "live"
        ),
        checkpoint_version=checkpoint.version if checkpoint is not None else None,
        completeness=totals.completeness,
        checkpoint=checkpoint,
        warning=warning,
        sources=sources,
    )
