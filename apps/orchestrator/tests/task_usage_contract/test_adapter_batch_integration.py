"""Providerless supervised adapter observations through real PostgreSQL."""

import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from forge.domain.subscription import SpecialistPurpose
from forge.domain.task_usage_contract import (
    CounterKind,
    Observation,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    TokenCounts,
    WorkUnitBinding,
)
from forge.domain.tool import ToolName
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.task_usage_observations import PostgresTaskUsageObservations
from forge.persistence.unit_of_work import PostgresUnitOfWork

# Reuse the established supervised-peer and durable-admission fixtures.
for directory in ("agents", "persistence"):
    helper_path = str(Path(__file__).resolve().parents[1] / directory)
    if helper_path not in sys.path:
        sys.path.insert(0, helper_path)

from test_codex_gateway import _Broker, _gateway
from test_scheduler_acceptance import _admit_run, _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_protocol import _request


class _DurableReplaySink:
    def __init__(self, writer: PostgresTaskUsageObservations) -> None:
        self.writer = writer
        self.events: list[Observation] = []

    async def observe_usage(self, observation: Observation) -> None:
        await self.writer.observe_usage(observation)
        await self.writer.observe_usage(observation)
        self.events.append(observation)


@pytest.mark.integration
async def test_supervised_adapter_observations_reconcile_once_in_postgres(
    session_factory, persisted_run
) -> None:
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(
            work,
            persisted_run,
            tuple(route.effective for _, route in request.envelope.routes),
        )
        envelope = await work.subscription.envelope_for_run(persisted_run.id)
        assert envelope is not None
        task = replace(request.task, run_id=persisted_run.id, parent_task_id=primary)
        attempt = replace(request.attempt, run_id=persisted_run.id)
        request = replace(
            request,
            task=task,
            attempt=attempt,
            envelope=envelope,
            authorization=replace(
                request.authorization,
                run_id=persisted_run.id,
                policy_version=envelope.safety_policy_version,
            ),
        )
        await work.subscription.create_task(task, idempotency_key="observed-adapter-task")
        await work.subscription.create_attempt(
            attempt, route_payload=task.route, idempotency_key="observed-adapter-attempt"
        )
        reservation = replace(task.budget, max_provider_attempts=1, max_repairs=0)
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task.task_id, attempt.attempt_id, reservation,
            idempotency_key="observed-adapter-reservation",
        )
        await work.commit()

    route = task.route.effective
    cohort = RouteCohort.from_members(
        (
            RouteMember(
                role=SpecialistPurpose.ROUTINE_IMPLEMENTATION.value,
                provider=route.provider,
                model=route.model,
                reasoning_effort=route.effort.value,
                ordinal=0,
            ),
        )
    )
    binding = WorkUnitBinding(
        subject=SubjectKey(
            project_id=persisted_run.project_id, kind="run", subject_id=persisted_run.id
        ),
        work_unit_id=uuid4(),
        admitted_phase_id="implement",
        configured_route=cohort,
        effective_route=cohort,
        source=SourceBinding(
            kind="subscription_attempt", source_id=attempt.attempt_id, parent_id=task.task_id
        ),
    )
    writer = PostgresTaskUsageObservations(session_factory)
    await writer.bind(binding)
    sink = _DurableReplaySink(writer)
    broker = _Broker()
    result = await _gateway("tool", broker=broker).execute_observed(
        request, usage_binding=binding, usage_sink=sink
    )
    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert broker.revoked and len(broker.calls) == 1
    assert result.telemetry.input_tokens == 13
    assert result.telemetry.output_tokens == 5
    assert sink.events and any(not item.final for item in sink.events)
    assert all(item.binding == binding for item in sink.events)
    assert len({item.event_id for item in sink.events}) == len(sink.events)

    query = TaskUsageTotalsQuery(session_factory)
    observed = await query.totals(binding.subject)
    assert observed.known_total_tokens == 18
    assert observed.source_count == 1
    assert observed.final_source_count == 0
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.settle_attempt(
            persisted_run.id, task.task_id, attempt.attempt_id, result.telemetry
        )
        await work.commit()
    settled = await query.totals(binding.subject)
    assert settled.known_total_tokens == 18
    assert settled.source_count == 1
    assert settled.final_source_count == 1
    assert settled.source_facts[0].evidence_kind == "subscription_consumption"

    late = Observation(
        binding=binding,
        event_id="late-counter",
        sequence=max(item.sequence for item in sink.events) + 1,
        counter_kind=CounterKind.CUMULATIVE,
        tokens=TokenCounts(input_tokens=80, output_tokens=40),
    )
    await writer.observe_usage(late)
    await writer.observe_usage(late)
    after_late = await query.totals(binding.subject)
    assert after_late.known_total_tokens == 18
    assert after_late.final_source_count == 1
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id, task.task_id)
        assert usage.consumed.input_tokens == 13
        assert usage.consumed.output_tokens == 5
        assert usage.consumed.repairs == 0
