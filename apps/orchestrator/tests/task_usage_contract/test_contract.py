import asyncio
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from forge.api.schemas.task_usage import (
    BaselineWire,
    CheckpointWire,
    ObservationWire,
    OwnerCommandReceiptWire,
    OwnerCommandWire,
    PolicyWire,
    TaskAllowanceInput,
    UsageReportWire,
    decode_legacy_allowance,
)
from forge.application.ports.agents import ObservableAgentGateway
from forge.application.ports.task_usage import TaskUsageObserver, TaskUsageRepository
from forge.domain.task_usage_contract import (
    BaselineReference,
    CheckpointRecord,
    CheckpointState,
    ContextWindow,
    CounterKind,
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
from pydantic import ValidationError

from .fixtures import bindings, examples


def binding() -> WorkUnitBinding:
    project, run, execution = uuid4(), uuid4(), uuid4()
    configured = RouteCohort.from_members(
        (RouteMember(role="developer", provider="test", model="configured", ordinal=0),)
    )
    effective = RouteCohort.from_members(
        (RouteMember(role="developer", provider="test", model="effective", ordinal=0),)
    )
    return WorkUnitBinding(
        subject=SubjectKey(project_id=project, kind="run", subject_id=run),
        work_unit_id=uuid4(),
        admitted_phase_id="implement",
        configured_route=configured,
        effective_route=effective,
        source=SourceBinding(kind="agent_execution", source_id=execution),
    )


def test_observation_preserves_zero_unknown_and_subsets():
    unit = binding()
    observation = Observation(
        binding=unit,
        event_id="ev1",
        sequence=1,
        counter_kind=CounterKind.CUMULATIVE,
        tokens=TokenCounts(input_tokens=0, output_tokens=None, cached_input_tokens=0),
    )
    assert observation.tokens.input_tokens == 0
    assert observation.tokens.output_tokens is None
    assert observation.context is None
    assert replace(observation, unknown_dimensions=("output_tokens",)).unknown_dimensions == (
        "output_tokens",
    )
    with pytest.raises(ValueError):
        replace(observation, unknown_dimensions=("input_tokens",))
    with pytest.raises(ValueError):
        TokenCounts(input_tokens=2, cached_input_tokens=3)
    with pytest.raises(ValueError):
        TokenCounts(output_tokens=1, reasoning_output_tokens=2)
    with pytest.raises(ValueError):
        TokenCounts(input_tokens=True)
    with pytest.raises(ValueError):
        Observation(
            binding=unit, event_id="ev2", sequence=0, counter_kind="delta", tokens=TokenCounts()
        )
    with pytest.raises(ValueError):
        replace(
            unit, source=SourceBinding(kind="agent_execution", source_id=unit.subject.subject_id)
        )
    payload = ObservationWire.dump_python(observation, mode="json")
    assert ObservationWire.validate_python(payload) == observation
    payload["tokens"]["input_tokens"] = True
    with pytest.raises(ValidationError):
        ObservationWire.validate_python(payload)
    payload = ObservationWire.dump_python(observation, mode="json")
    payload["final"] = "false"
    with pytest.raises(ValidationError):
        ObservationWire.validate_python(payload)
    final = replace(
        observation,
        event_id="final-2",
        sequence=2,
        tokens=TokenCounts(input_tokens=120, output_tokens=30, cached_input_tokens=20),
        context=ContextWindow(occupied_tokens=500, capacity_tokens=1000, compaction_count=1),
        final=True,
        final_status="succeeded",
        reconciles_event_id="ev1",
    )
    assert ObservationWire.validate_python(ObservationWire.dump_python(final, mode="json")) == final
    with pytest.raises(ValueError):
        replace(final, reconciles_event_id="final-2")
    with pytest.raises(ValueError):
        ContextWindow(occupied_tokens=1001, capacity_tokens=1000)


def test_route_composition_fingerprint_changes_with_effective_member():
    members = (
        RouteMember(role="planner", provider="provider-a", model="model-a", ordinal=0),
        RouteMember(role="developer", provider="provider-b", model="model-b", ordinal=1),
    )
    configured = RouteCohort.from_members(members)
    effective = RouteCohort.from_members((members[0], replace(members[1], model="model-c")))
    assert configured.fingerprint != effective.fingerprint
    assert (
        configured.fingerprint
        != RouteCohort.from_members(
            (members[0], replace(members[1], reasoning_effort="high"))
        ).fingerprint
    )
    with pytest.raises(ValueError):
        RouteCohort(fingerprint=configured.fingerprint, members=effective.members)


def test_policy_baseline_and_caps_are_distinct():
    policy = MonitoringPolicy(project_id=uuid4())
    assert policy.mode == "report_only"
    assert policy.warning_multiplier == 1.5
    assert policy.checkpoint_multiplier == 2
    assert policy.history_limit == 50 and policy.minimum_comparables == 20
    unit = binding()
    reference = BaselineReference(
        binding=unit,
        policy_revision=1,
        sample_count=0,
        status="insufficient_history",
        history_digest="c" * 64,
        snapshot_digest="d" * 64,
        estimator_version="median_p90_v1",
        window_limit=50,
        minimum_comparables=20,
        cohort_digest=unit.configured_route.fingerprint,
        frozen_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    assert reference.reference_tokens is None
    assert TaskAllowanceInput.model_validate({}).hard_token_cap is None
    assert TaskAllowanceInput.model_validate({"hard_token_cap": None}).hard_token_cap is None
    assert TaskAllowanceInput.model_validate({"hard_token_cap": 300}).hard_token_cap == 300
    assert decode_legacy_allowance({"max_tokens": 300}).hard_token_cap == 300
    with pytest.raises(ValidationError):
        TaskAllowanceInput.model_validate({"hard_token_cap": 0})


def test_policy_write_requires_explicit_actor_and_revision():
    signature = inspect.signature(TaskUsageRepository.append_policy_revision)
    policy = MonitoringPolicy(project_id=uuid4())
    actor = uuid4()
    with pytest.raises(TypeError):
        signature.bind(object(), policy, expected_revision=0)
    bound = signature.bind(object(), policy, expected_revision=0, actor_id=actor)
    assert bound.arguments["actor_id"] == actor
    assert bound.arguments["expected_revision"] == 0

    class FakeRepository:
        async def append_policy_revision(
            self, policy: MonitoringPolicy, *, expected_revision: int, actor_id: UUID
        ) -> None:
            self.written = (policy, expected_revision, actor_id)

    fake = FakeRepository()
    asyncio.run(fake.append_policy_revision(policy, expected_revision=0, actor_id=actor))
    assert fake.written == (policy, 0, actor)


def test_new_integer_boundaries_match_signed_bigint():
    maximum = 2**63 - 1
    project = uuid4()
    for cap in (2**31, maximum):
        assert MonitoringPolicy(project_id=project, hard_token_cap=cap).hard_token_cap == cap
        assert TaskAllowanceInput.model_validate({"hard_context_cap": cap}).hard_context_cap == cap
        assert (
            PolicyWire.validate_python(
                PolicyWire.dump_python(
                    MonitoringPolicy(project_id=project, hard_token_cap=cap), mode="json"
                )
            ).hard_token_cap
            == cap
        )
    for bad in (maximum + 1, 0, -1, True, 1.5):
        with pytest.raises((ValueError, TypeError)):
            MonitoringPolicy(project_id=project, hard_token_cap=bad)
        with pytest.raises(ValidationError):
            TaskAllowanceInput.model_validate({"hard_token_cap": bad})
    with pytest.raises(ValidationError):
        PolicyWire.validate_python({"project_id": str(project), "hard_token_cap": maximum + 1})
    assert TokenCounts(input_tokens=0, cached_input_tokens=0).input_tokens == 0
    with pytest.raises(ValueError):
        TokenCounts(input_tokens=maximum + 1)
    fixture = examples()
    for value, field in (
        (fixture["policy"], "revision"),
        (fixture["policy"], "history_limit"),
        (fixture["live"], "sequence"),
        (fixture["baseline"], "window_limit"),
        (fixture["checkpoint_states"][0], "version"),
        (fixture["command"], "expected_version"),
        (fixture["receipt"], "result_version"),
    ):
        with pytest.raises(ValueError):
            replace(value, **{field: maximum + 1})
    with pytest.raises(ValueError):
        ContextWindow(compaction_count=maximum + 1)


def test_checkpoint_and_owner_versions():
    unit = binding()
    values = {
        "checkpoint_id": uuid4(),
        "binding": unit,
        "baseline_binding": unit,
        "policy_revision": 1,
        "reference_digest": "f" * 64,
        "trigger_event_id": "event-1",
    }
    for state in (
        CheckpointState.REQUESTED,
        CheckpointState.PAUSING,
        CheckpointState.PAUSED,
        CheckpointState.RESUMED,
        CheckpointState.RESOLVED,
    ):
        settled = state in {
            CheckpointState.PAUSED,
            CheckpointState.RESUMED,
            CheckpointState.RESOLVED,
        }
        assert CheckpointRecord(**values, state=state, process_settled=settled).state == state
    with pytest.raises(ValueError):
        CheckpointRecord(**values, state=CheckpointState.PAUSED, process_settled=False)
    command = OwnerCommand(
        command_id=uuid4(),
        checkpoint_id=values["checkpoint_id"],
        expected_version=0,
        idempotency_key="owner-key",
        action="override_checkpoint",
        actor_id=uuid4(),
    )
    assert command.override_note is None
    with pytest.raises(ValueError):
        replace(command, expected_version=-1)
    with pytest.raises(ValueError):
        replace(command, action="continue")
    assert (
        replace(command, action="continue", continue_mode="without_usage_pauses").continue_mode
        == "without_usage_pauses"
    )
    with pytest.raises(ValueError):
        replace(command, action="set_policy")
    with pytest.raises(TypeError):
        replace(command, action="set_policy", requested_policy=object())
    with pytest.raises(ValueError):
        replace(command, idempotency_key=True)
    assert (
        replace(
            command,
            action="set_policy",
            requested_policy=MonitoringPolicy(project_id=unit.subject.project_id),
        ).requested_policy
        is not None
    )


def test_legacy_allowances_and_strict_wire():
    allowance = decode_legacy_allowance({"max_input_tokens": 0, "max_output_tokens": 17})
    assert allowance.legacy_max_input_tokens == 0
    assert allowance.legacy_max_output_tokens == 17
    assert allowance.hard_token_cap is None
    assert (
        decode_legacy_allowance(
            {"max_input_tokens": 11, "max_output_tokens": 7}
        ).legacy_max_output_tokens
        == 7
    )
    assert decode_legacy_allowance({"max_input_tokens": None}).legacy_max_input_tokens is None
    with pytest.raises(ValueError):
        decode_legacy_allowance({"max_input_tokens": 1, "max_tokens": 2})
    with pytest.raises(ValidationError):
        decode_legacy_allowance({"unexpected": 1})
    unit = binding()
    baseline = BaselineReference(
        binding=unit,
        policy_revision=1,
        sample_count=2,
        status="ready",
        history_digest="a" * 64,
        snapshot_digest="b" * 64,
        estimator_version="median_p90_v1",
        window_limit=50,
        minimum_comparables=2,
        cohort_digest=unit.configured_route.fingerprint,
        reference_tokens=1.5,
        median_tokens=1.5,
        p90_tokens=2.5,
        mean_tokens=1.75,
        warning_threshold_tokens=2.25,
        checkpoint_threshold_tokens=3.0,
        window_started_at=datetime(2026, 10, 1, tzinfo=UTC),
        window_ended_at=datetime(2026, 10, 9, tzinfo=UTC),
        frozen_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    assert BaselineWire.validate_python(BaselineWire.dump_python(baseline, mode="json")) == baseline
    with pytest.raises(ValidationError):
        BaselineWire.validate_python(
            {**BaselineWire.dump_python(baseline, mode="json"), "extra": 1}
        )
    nested = BaselineWire.dump_python(baseline, mode="json")
    nested["binding"]["subject"]["extra"] = 1
    with pytest.raises(ValidationError):
        BaselineWire.validate_python(nested)
    report = UsageReportWire.validate_python(
        {
            "binding": baseline.binding,
            "policy_revision": 1,
            "baseline": baseline,
            "input_tokens": 0,
            "output_tokens": None,
            "context_occupied_tokens": None,
            "freshness": "live",
            "checkpoint_version": 0,
        }
    )
    assert report.input_tokens == 0
    with pytest.raises(ValidationError):
        UsageReportWire.validate_python(
            {**UsageReportWire.dump_python(report, mode="json"), "checkpoint_version": True}
        )
    with pytest.raises(ValidationError):
        OwnerCommandReceiptWire.validate_python(
            {
                "command_id": str(uuid4()),
                "checkpoint_id": str(uuid4()),
                "result_version": True,
                "state": "resolved",
                "actor_id": str(uuid4()),
                "action": "resolve",
                "warnings": (),
                "affected_snapshot": "a" * 64,
            }
        )


def test_deterministic_whole_subject_fixtures_round_trip():
    first = examples()
    assert first == examples()
    anchor, fallback = bindings()
    assert anchor.subject == fallback.subject
    assert anchor.work_unit_id != fallback.work_unit_id
    authoring_anchor, authoring_retry = bindings("authoring_job")
    assert authoring_anchor.subject == authoring_retry.subject
    assert authoring_anchor.source != authoring_retry.source
    assert first["baseline"].binding == anchor
    assert replace(first["report"], binding=fallback).baseline == first["baseline"]
    adapters = {
        "policy": PolicyWire,
        "checkpoint_policy": PolicyWire,
        "baseline": BaselineWire,
        "ready_baseline": BaselineWire,
        "live": ObservationWire,
        "final": ObservationWire,
        "delta": ObservationWire,
        "command": OwnerCommandWire,
        "receipt": OwnerCommandReceiptWire,
        "replay_receipt": OwnerCommandReceiptWire,
        "stale_command": OwnerCommandWire,
        "report": UsageReportWire,
        "checkpoint_report": UsageReportWire,
    }
    for name, adapter in adapters.items():
        value = first[name]
        assert adapter.validate_python(adapter.dump_python(value, mode="json")) == value
    for state in first["checkpoint_states"]:
        assert (
            CheckpointWire.validate_python(CheckpointWire.dump_python(state, mode="json")) == state
        )


def test_opt_in_observed_gateway_emits_to_runtime_sink():
    examples_by_name = examples()

    class Sink(TaskUsageObserver):
        def __init__(self) -> None:
            self.events: list[Observation] = []

        async def observe_usage(self, observation: Observation) -> None:
            self.events.append(observation)

    class OldFake:
        async def execute(self, request: object) -> str:
            return "old"

    class ObservedFake(OldFake):
        async def execute_observed(
            self, request: object, *, usage_binding: WorkUnitBinding, usage_sink: TaskUsageObserver
        ) -> str:
            await usage_sink.observe_usage(replace(examples_by_name["live"], binding=usage_binding))
            await usage_sink.observe_usage(
                replace(examples_by_name["final"], binding=usage_binding)
            )
            return "observed"

    sink = Sink()
    gateway: ObservableAgentGateway = ObservedFake()  # type: ignore[assignment]
    invocation_binding = bindings()[1]
    assert (
        asyncio.run(
            gateway.execute_observed(None, usage_binding=invocation_binding, usage_sink=sink)
        )
        == "observed"
    )  # type: ignore[arg-type]
    assert [event.event_id for event in sink.events] == ["live", "final"]
    assert all(event.binding == invocation_binding for event in sink.events)
    assert asyncio.run(OldFake().execute(None)) == "old"


def test_subject_baseline_snapshot_and_child_trigger_contract():
    anchor, child = bindings()
    frozen_at = datetime(2026, 10, 10, tzinfo=UTC)
    ready = BaselineReference(
        binding=anchor,
        policy_revision=1,
        sample_count=20,
        status="ready",
        history_digest="a" * 64,
        snapshot_digest="b" * 64,
        estimator_version="median_p90_v1",
        window_limit=50,
        minimum_comparables=20,
        window_started_at=frozen_at - timedelta(days=7),
        window_ended_at=frozen_at,
        cohort_digest=anchor.configured_route.fingerprint,
        reference_tokens=21.5,
        median_tokens=10.5,
        p90_tokens=21.5,
        mean_tokens=12.25,
        warning_threshold_tokens=32.25,
        checkpoint_threshold_tokens=43.0,
        coverage_count=20,
        outcome_count=20,
        source_count=25,
        frozen_at=frozen_at,
    )
    checkpoint = CheckpointRecord(
        checkpoint_id=child.work_unit_id,
        binding=child,
        baseline_binding=anchor,
        state=CheckpointState.REQUESTED,
        policy_revision=1,
        reference_digest=ready.snapshot_digest,
        trigger_event_id="child-event",
    )
    assert checkpoint.binding == child and checkpoint.baseline_binding == anchor
    assert BaselineWire.validate_python(BaselineWire.dump_python(ready, mode="json")) == ready
    with pytest.raises(ValueError):
        replace(checkpoint, baseline_binding=bindings("authoring_job")[0])
    with pytest.raises(ValueError):
        replace(ready, warning_threshold_tokens=float("nan"))
    with pytest.raises(ValueError):
        replace(ready, window_ended_at=frozen_at - timedelta(days=8))
