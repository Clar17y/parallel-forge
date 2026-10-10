"""Deterministic whole-subject inputs; no runtime evaluator is implied."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from forge.application.ports.task_usage import OwnerCommandReceipt, UsageReport
from forge.domain.task_usage_contract import (
    BaselineReference,
    CheckpointRecord,
    CheckpointState,
    ContextWindow,
    CounterKind,
    MonitoringMode,
    MonitoringPolicy,
    Observation,
    OwnerCommand,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    TokenCounts,
    WorkUnitBinding,
)


def identity(number: int) -> UUID:
    return UUID(int=number)


def bindings(kind: str = "run") -> tuple[WorkUnitBinding, WorkUnitBinding]:
    subject = SubjectKey(project_id=identity(1), kind=kind, subject_id=identity(2))
    route = RouteCohort.from_members(
        (
            RouteMember(
                role="developer",
                provider="test",
                model="model",
                ordinal=0,
                reasoning_effort="medium",
            ),
        )
    )
    return tuple(
        WorkUnitBinding(
            subject=subject,
            work_unit_id=identity(number),
            admitted_phase_id="implement" if kind == "run" else "brainstorm",
            configured_route=route,
            effective_route=route,
            source=SourceBinding(
                kind="agent_execution" if kind == "run" else "authoring_attempt",
                source_id=identity(number + 10),
            ),
        )
        for number in (3, 4)
    )  # type: ignore[return-value]


def examples() -> dict[str, object]:
    anchor, fallback = bindings()
    policy = MonitoringPolicy(project_id=anchor.subject.project_id)
    checkpoint_policy = replace(policy, mode=MonitoringMode.CHECKPOINT)
    baseline = BaselineReference(
        binding=anchor,
        policy_revision=policy.revision,
        sample_count=0,
        status="insufficient_history",
        history_digest="a" * 64,
        snapshot_digest="b" * 64,
        estimator_version="median_p90_v1",
        window_limit=50,
        minimum_comparables=20,
        cohort_digest=anchor.configured_route.fingerprint,
        frozen_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    ready_baseline = BaselineReference(
        binding=anchor,
        policy_revision=policy.revision,
        sample_count=20,
        status="ready",
        history_digest="c" * 64,
        snapshot_digest="d" * 64,
        estimator_version="median_p90_v1",
        window_limit=50,
        minimum_comparables=20,
        cohort_digest=anchor.configured_route.fingerprint,
        window_started_at=datetime(2026, 10, 10, tzinfo=UTC) - timedelta(days=7),
        window_ended_at=datetime(2026, 10, 10, tzinfo=UTC),
        reference_tokens=21.5,
        median_tokens=10.5,
        p90_tokens=21.5,
        mean_tokens=12.25,
        warning_threshold_tokens=32.25,
        checkpoint_threshold_tokens=43.0,
        coverage_count=20,
        outcome_count=20,
        source_count=25,
        frozen_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    live = Observation(
        binding=anchor,
        event_id="live",
        sequence=1,
        counter_kind=CounterKind.CUMULATIVE,
        tokens=TokenCounts(input_tokens=0, output_tokens=None, cached_input_tokens=0),
        unknown_dimensions=("output_tokens",),
    )
    final = replace(
        live,
        event_id="final",
        sequence=2,
        tokens=TokenCounts(input_tokens=20, output_tokens=4),
        context=ContextWindow(occupied_tokens=100, capacity_tokens=200, compaction_count=1),
        unknown_dimensions=(),
        final=True,
        final_status="succeeded",
        reconciles_event_id="live",
    )
    delta = Observation(
        binding=fallback,
        event_id="fallback-delta",
        sequence=1,
        counter_kind=CounterKind.DELTA,
        tokens=TokenCounts(input_tokens=3, output_tokens=2),
    )
    checkpoint = CheckpointRecord(
        checkpoint_id=identity(30),
        binding=fallback,
        baseline_binding=anchor,
        state=CheckpointState.REQUESTED,
        policy_revision=1,
        reference_digest=ready_baseline.snapshot_digest,
        trigger_event_id="fallback-delta",
        version=0,
    )
    states = tuple(
        replace(checkpoint, state=state, version=index, process_settled=index >= 2)
        for index, state in enumerate(CheckpointState)
    )
    command = OwnerCommand(
        command_id=identity(31),
        checkpoint_id=checkpoint.checkpoint_id,
        expected_version=0,
        idempotency_key="continue-1",
        action="continue",
        actor_id=identity(32),
        continue_mode="next_checkpoint",
    )
    receipt = OwnerCommandReceipt(
        command_id=command.command_id,
        checkpoint_id=checkpoint.checkpoint_id,
        result_version=1,
        state="pausing",
        actor_id=command.actor_id,
        action=command.action,
        affected_snapshot=ready_baseline.snapshot_digest,
    )
    report = UsageReport(
        binding=anchor,
        policy_revision=1,
        baseline=baseline,
        input_tokens=0,
        output_tokens=None,
        context_occupied_tokens=None,
        freshness="live",
        completeness="lower_bound",
    )
    checkpoint_report = replace(
        report,
        binding=fallback,
        baseline=ready_baseline,
        input_tokens=3,
        output_tokens=2,
        checkpoint=checkpoint,
        checkpoint_version=0,
    )
    return {
        "policy": policy,
        "checkpoint_policy": checkpoint_policy,
        "baseline": baseline,
        "ready_baseline": ready_baseline,
        "live": live,
        "final": final,
        "delta": delta,
        "checkpoint_states": states,
        "command": command,
        "replay_receipt": replace(receipt, replayed=True),
        "receipt": receipt,
        "stale_command": replace(
            command, command_id=identity(33), expected_version=0, idempotency_key="stale-1"
        ),
        "report": report,
        "checkpoint_report": checkpoint_report,
        "fallback": fallback,
    }
