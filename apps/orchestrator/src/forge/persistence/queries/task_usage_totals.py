"""Consistent subject totals from source evidence and authoritative final usage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from forge.application.ports.task_usage import FinalizedSourceFact, UsageTotals
from forge.application.services.task_usage_observations import SourceUsage, project_source
from forge.domain.task_usage_contract import (
    MAX_SIGNED_BIGINT,
    SubjectKey,
    TokenCounts,
)
from forge.persistence.models import ModelUsage
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.models.task_usage import TaskUsageObservation, TaskUsageWorkUnit
from forge.persistence.repositories.task_usage_observations import (
    decode_binding,
    decode_observation,
)

__all__ = ["FinalizedSourceFact", "TaskUsageTotalsQuery", "UsageTotals"]
_BATCH_SIZE = 1000


def _batches(identities: list[UUID]) -> Iterator[list[UUID]]:
    for start in range(0, len(identities), _BATCH_SIZE):
        yield identities[start : start + _BATCH_SIZE]


def _sum_known(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    total = sum(known)
    if total > MAX_SIGNED_BIGINT:
        raise ValueError("work-unit usage exceeds signed-64-bit range")
    return total


def _measured(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_SIGNED_BIGINT:
        raise ValueError("stored measured usage is malformed")
    return value


def _finalized(fact: FinalizedSourceFact) -> bool:
    return fact.evidence_kind in {"subscription_consumption", "model_usage"} or (
        fact.binding.source.kind == "authoring_attempt"
        and fact.evidence_kind == "observation_final"
    )


def _from_authoritative(
    live: SourceUsage,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None,
) -> SourceUsage:
    # Existing final consumption/model usage wins even if a late live event arrives.
    TokenCounts(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
    )
    disagreements = list(live.disagreements)
    for name, final_value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        provisional = getattr(live, name)
        if provisional is not None and final_value is not None and provisional > final_value:
            disagreements.append("final_below_live")
    if live.final and (live.input_tokens != input_tokens or live.output_tokens != output_tokens):
        disagreements.append("final_evidence_mismatch")
    reasoning = live.reasoning_output_tokens if live.final else None
    if reasoning is not None and (output_tokens is None or reasoning > output_tokens):
        reasoning = None
        disagreements.append("reasoning_subset_unverified")
    return replace(
        live,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_output_tokens=reasoning,
        final=True,
        final_status=live.final_status or "settled",
        completeness=(
            "unknown"
            if input_tokens is None and output_tokens is None
            else "complete"
            if input_tokens is not None and output_tokens is not None
            else "lower_bound"
        ),
        disagreements=tuple(sorted(set(disagreements))),
    )


class TaskUsageTotalsQuery:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def totals(self, subject: SubjectKey, *, as_of: datetime | None = None) -> UsageTotals:
        """Read one internally consistent snapshot in a dedicated transaction."""
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            return await self.totals_in(session, subject, as_of=as_of)

    async def totals_in(
        self, session: AsyncSession, subject: SubjectKey, *, as_of: datetime | None = None,
    ) -> UsageTotals:
        """Read in the caller's transaction so other projections share its snapshot."""
        if not isinstance(subject, SubjectKey):
            raise TypeError("subject must be typed")
        if as_of is not None and as_of.tzinfo is None:
            raise ValueError("as_of must be timezone aware")
        unit_filters = [
            TaskUsageWorkUnit.project_id == subject.project_id,
            TaskUsageWorkUnit.subject_kind == subject.kind,
            TaskUsageWorkUnit.subject_id == subject.subject_id,
        ]
        if as_of is not None:
            unit_filters.append(TaskUsageWorkUnit.created_at <= as_of)
        units = (
            await session.scalars(
                select(TaskUsageWorkUnit)
                .where(*unit_filters)
                .order_by(TaskUsageWorkUnit.id)
            )
        ).all()
        return (await self.totals_for_units_in(session, {subject: list(units)}, as_of=as_of))[subject]

    async def totals_for_units_in(
        self,
        session: AsyncSession,
        units_by_subject: dict[SubjectKey, list[TaskUsageWorkUnit]],
        *,
        as_of: datetime | None = None,
    ) -> dict[SubjectKey, UsageTotals]:
        """Project already selected subjects in one transaction with bounded evidence reads."""
        if as_of is not None and as_of.tzinfo is None:
            raise ValueError("as_of must be timezone aware")
        units = [unit for subject_units in units_by_subject.values() for unit in subject_units]
        unit_ids = [unit.id for unit in units]
        observation_rows: dict[UUID, list[TaskUsageObservation]] = {}
        for batch in _batches(unit_ids):
            observation_filters: list[ColumnElement[bool]] = [
                TaskUsageObservation.work_unit_id.in_(batch)
            ]
            if as_of is not None:
                observation_filters.append(TaskUsageObservation.observed_at <= as_of)
            for row in (
                await session.scalars(
                    select(TaskUsageObservation)
                    .where(*observation_filters)
                    .order_by(TaskUsageObservation.work_unit_id, TaskUsageObservation.sequence)
                )
            ).all():
                observation_rows.setdefault(row.work_unit_id, []).append(row)
        agent_keys = [
            (unit.subject_id, unit.source_id)
            for unit in units if unit.source_kind == "agent_execution"
        ]
        model_rows: dict[tuple[UUID, UUID], list[ModelUsage]] = {}
        for start in range(0, len(agent_keys), _BATCH_SIZE):
            agent_batch = agent_keys[start : start + _BATCH_SIZE]
            model_filters: list[ColumnElement[bool]] = [
                tuple_(ModelUsage.run_id, ModelUsage.agent_execution_id).in_(agent_batch),
            ]
            if as_of is not None:
                model_filters.append(ModelUsage.created_at <= as_of)
            for record in (
                await session.scalars(
                    select(ModelUsage)
                    .where(*model_filters)
                    .order_by(ModelUsage.agent_execution_id, ModelUsage.id)
                )
            ).all():
                model_rows.setdefault((record.run_id, record.agent_execution_id), []).append(record)
        attempt_ids = [
            unit.source_id for unit in units if unit.source_kind == "subscription_attempt"
        ]
        consumptions: dict[UUID, SubscriptionAttemptConsumption] = {}
        for batch in _batches(attempt_ids):
            consumption_filters: list[ColumnElement[bool]] = [
                SubscriptionAttemptConsumption.attempt_id.in_(batch)
            ]
            if as_of is not None:
                consumption_filters.append(SubscriptionAttemptConsumption.created_at <= as_of)
            consumption_rows = await session.scalars(
                select(SubscriptionAttemptConsumption).where(*consumption_filters)
            )
            consumptions.update({row.attempt_id: row for row in consumption_rows.all()})
        return {
            subject: _project_totals(subject, sorted(subject_units, key=lambda unit: str(unit.id)),
                                     observation_rows, model_rows, consumptions)
            for subject, subject_units in units_by_subject.items()
        }

    async def finalized_sources(self, subject: SubjectKey) -> tuple[FinalizedSourceFact, ...]:
        totals = await self.totals(subject)
        return tuple(fact for fact in totals.source_facts if _finalized(fact))


def _project_totals(
    subject: SubjectKey,
    units: list[TaskUsageWorkUnit],
    observation_rows: dict[UUID, list[TaskUsageObservation]],
    model_rows: dict[tuple[UUID, UUID], list[ModelUsage]],
    consumptions: dict[UUID, SubscriptionAttemptConsumption],
) -> UsageTotals:
    facts: list[FinalizedSourceFact] = []
    usages: list[SourceUsage] = []
    fingerprint: list[object] = []
    for unit in units:
        binding = decode_binding(unit)
        rows = observation_rows.get(unit.id, [])
        usage = project_source(tuple(decode_observation(row, binding) for row in rows))
        evidence_kind = "observation_final" if usage.final else "live"
        fingerprint.append((str(unit.id), [(row.event_id, row.sequence) for row in rows]))
        if unit.source_kind == "subscription_attempt":
            consumption = consumptions.get(unit.source_id)
            if consumption is not None:
                measured = consumption.observed
                usage = _from_authoritative(
                    usage,
                    input_tokens=_measured(measured.get("input_tokens")),
                    output_tokens=_measured(measured.get("output_tokens")),
                    cached_input_tokens=_measured(measured.get("cached_input_tokens")),
                )
                evidence_kind = "subscription_consumption"
                fingerprint.append(("subscription", str(unit.source_id), measured))
        elif unit.source_kind == "agent_execution":
            records = model_rows.get((subject.subject_id, unit.source_id), [])
            if records:
                usage = _from_authoritative(
                    usage,
                    input_tokens=_sum_known([
                        record.input_tokens + record.cached_input_tokens for record in records
                    ]),
                    output_tokens=_sum_known([record.output_tokens for record in records]),
                    cached_input_tokens=_sum_known(
                        [record.cached_input_tokens for record in records]
                    ),
                )
                evidence_kind = "model_usage"
                fingerprint.append(("model_usage", [str(record.id) for record in records]))
        usages.append(usage)
        facts.append(
            FinalizedSourceFact(
                binding=binding,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                context_occupied_tokens=usage.context_occupied_tokens,
                context_capacity_tokens=usage.context_capacity_tokens,
                compaction_count=usage.compaction_count,
                completeness=usage.completeness,
                final_status=usage.final_status,
                evidence_kind=evidence_kind,
                disagreements=usage.disagreements,
            )
        )
    inputs = _sum_known([usage.input_tokens for usage in usages])
    outputs = _sum_known([usage.output_tokens for usage in usages])
    completeness = (
        "unknown"
        if inputs is None and outputs is None
        else "complete"
        if usages and all(usage.completeness == "complete" for usage in usages)
        else "lower_bound"
    )
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return UsageTotals(
        subject=subject,
        snapshot_digest=digest,
        source_count=len(units),
        final_source_count=sum(_finalized(fact) for fact in facts),
        input_tokens=inputs,
        output_tokens=outputs,
        cached_input_tokens=_sum_known([usage.cached_input_tokens for usage in usages]),
        reasoning_output_tokens=_sum_known([usage.reasoning_output_tokens for usage in usages]),
        completeness=completeness,
        source_facts=tuple(facts),
    )
