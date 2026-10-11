"""Live usage observation tests for the Codex app-server subscription gateway."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from forge.agents.codex_gateway import _Usage
from forge.agents.subscription_protocol import ProtocolError
from forge.agents.subscription_usage_observations import (
    _extract_context_window,
    _UsagePublisher,
)
from forge.application.ports.subscription_gateway import (
    ObservableSubscriptionGateway,
    SubscriptionFailure,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
)
from forge.application.ports.task_usage import TaskUsageObserver
from forge.domain.task_usage_contract import (
    Observation,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    TokenCounts,
    WorkUnitBinding,
)
from forge.domain.tool import ToolName
from test_codex_gateway import _Broker, _gateway, _script
from test_subscription_protocol import _request


class RecordingUsageObserver(TaskUsageObserver):
    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.events: list[Observation] = []
        self.fail = fail
        self.hang = hang

    async def observe_usage(self, observation: Observation) -> None:
        if self.hang:
            await asyncio.sleep(10)
        if self.fail:
            raise RuntimeError("sink delivery failure")
        self.events.append(observation)


def test_codex_optional_reasoning_cumulative_validation_and_core_counters() -> None:
    usage = _Usage()

    def observe(input_tokens: int, output_tokens: int, cached: int, **extra: object) -> None:
        usage.observe({"tokenUsage": {"total": {
            "inputTokens": input_tokens, "outputTokens": output_tokens,
            "cachedInputTokens": cached, **extra,
        }}})

    observe(13, 5, 2, reasoningOutputTokens=2)
    assert usage.reasoning == 2
    observe(13, 5, 2)
    assert usage.reasoning == 2
    for invalid in (True, -1, 6, 2**63):
        observe(13, 5, 2, reasoningOutputTokens=invalid)
        assert usage.reasoning is None
    observe(13, 5, 2, reasoningOutputTokens=0)
    assert usage.reasoning == 0
    observe(2**63 - 1, 2**63 - 1, 2, reasoningOutputTokens=2**63 - 1)
    assert usage.reasoning == 2**63 - 1
    observe(2**63 - 1, 2**63 - 1, 2, reasoningOutputTokens=2**63)
    assert usage.reasoning is None
    with pytest.raises(ProtocolError):
        observe(2**63, 2**63 - 1, 2)
    with pytest.raises(ProtocolError):
        observe(2**63 - 1, 2**63 - 1, 2**63)
    assert (usage.input, usage.output, usage.cached) == (2**63 - 1, 2**63 - 1, 2)


@pytest.mark.asyncio
async def test_codex_observed_optional_reasoning_bool_does_not_fail_settled_turn() -> None:
    script = _script("tool").replace(
        "def send(value): print(json.dumps(value),flush=True)",
        "def send(value):\n"
        " if value.get('method') == 'thread/tokenUsage/updated':\n"
        "  value['params']['tokenUsage']['total']['reasoningOutputTokens'] = True\n"
        " print(json.dumps(value),flush=True)",
    )
    gateway = _gateway("tool", broker=_Broker())
    gateway._installation = replace(gateway._installation, script=("-c", script))
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    sink = RecordingUsageObserver()
    result = await gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    )
    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.output_tokens == 5
    assert [event.tokens.reasoning_output_tokens for event in sink.events] == [None, None]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"occupiedTokens": 0, "capacityTokens": 0, "compactionCount": 0}, (0, 0, 0)),
        ({"occupiedTokens": 2**63 - 1, "capacityTokens": 2**63 - 1},
         (2**63 - 1, 2**63 - 1, None)),
        ({"occupiedTokens": True, "capacityTokens": 7, "compactionCount": 2},
         (None, 7, 2)),
        ({"occupiedTokens": -1, "capacityTokens": 7, "compactionCount": 2**63},
         (None, 7, None)),
        ({"occupiedTokens": 2**63, "capacityTokens": 7}, (None, 7, None)),
        ({"occupiedTokens": 8, "capacityTokens": 7, "compactionCount": 0},
         (None, 7, 0)),
        ({"capacityTokens": 7}, (None, 7, None)),
        ({"occupiedTokens": 7}, (7, None, None)),
        ({"occupiedTokens": False, "capacityTokens": -1}, None),
    ],
)
def test_optional_context_dimensions_are_independently_sanitized(
    raw: dict[str, object], expected: tuple[int | None, int | None, int | None] | None,
) -> None:
    context = _extract_context_window({"contextWindow": raw})
    actual = None if context is None else (
        context.occupied_tokens, context.capacity_tokens, context.compaction_count
    )
    assert actual == expected


@pytest.mark.parametrize(
    "raw_id", [None, "", " ", True, [], "i" * 256, "i" * 257, "forge:missing-event:1"]
)
@pytest.mark.asyncio
async def test_codex_public_observation_id_edges_preserve_settlement(raw_id: object) -> None:
    script = _script("tool").replace(
        "def send(value): print(json.dumps(value),flush=True)",
        "def send(value):\n"
        " if value.get('method') == 'thread/tokenUsage/updated':\n"
        + (
            "  value['params'].pop('eventId', None)\n"
            if raw_id is None else f"  value['params']['eventId'] = {raw_id!r}\n"
        )
        + " print(json.dumps(value),flush=True)",
    )
    gateway = _gateway("tool", broker=_Broker())
    gateway._installation = replace(gateway._installation, script=("-c", script))
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    sink = RecordingUsageObserver()
    result = await gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    )
    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 13
    live = [event for event in sink.events if not event.final]
    assert len(live) == 1
    assert 0 < len(live[0].event_id) <= 256
    if isinstance(raw_id, str) and raw_id.strip() and len(raw_id) <= 256 and not raw_id.startswith("forge:"):
        assert live[0].event_id == raw_id
    else:
        assert live[0].event_id.startswith(("forge:missing-event:", "forge:sha256:"))
    assert sink.events[-1].reconciles_event_id == live[0].event_id


@pytest.mark.asyncio
async def test_codex_public_long_epoch_and_live_final_id_collision_settle() -> None:
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    final_id = f"codex-final-{request.attempt.attempt_id}"
    script = _script("tool").replace("thread-actual", "thread-" + "x" * 120)
    script = script.replace(
        "def send(value): print(json.dumps(value),flush=True)",
        "def send(value):\n"
        " if value.get('method') == 'thread/tokenUsage/updated':\n"
        f"  value['params']['eventId'] = {final_id!r}\n"
        " print(json.dumps(value),flush=True)",
    )
    gateway = _gateway("tool", broker=_Broker())
    gateway._installation = replace(gateway._installation, script=("-c", script))
    sink = RecordingUsageObserver()
    result = await gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    )
    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    live = [event for event in sink.events if not event.final]
    assert len(live) == 1
    assert live[0].event_id == final_id
    assert live[0].counter_epoch.startswith("forge:sha256:")
    assert len(live[0].counter_epoch) <= 96
    assert sink.events[-1].reconciles_event_id == live[0].event_id
    assert sink.events[-1].event_id != live[0].event_id


@pytest.mark.asyncio
async def test_codex_observed_untrusted_context_and_colliding_long_ids_settle() -> None:
    long_prefix = "provider-" + "x" * 260
    original = _script("tool")
    script = original.replace(
        "def send(value): print(json.dumps(value),flush=True)",
        "def send(value):\n"
        " if value.get('method') == 'thread/tokenUsage/updated':\n"
        "  value['params']['contextWindow'] = {'occupiedTokens': 9, 'capacityTokens': 3, 'compactionCount': True}\n"
        f"  value['params']['eventId'] = {long_prefix + 'a'!r}\n"
        "  print(json.dumps(value),flush=True)\n"
        f"  value['params']['eventId'] = {long_prefix + 'b'!r}\n"
        " print(json.dumps(value),flush=True)",
    )
    assert script != original
    gateway = _gateway("tool", broker=_Broker())
    gateway._installation = replace(gateway._installation, script=("-c", script))
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    sink = RecordingUsageObserver()

    result = await gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    )

    assert result.failure is None, result.failure_detail
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 13
    live = [event for event in sink.events if not event.final]
    assert len(live) == 2
    assert live[0].event_id != live[1].event_id
    assert all(0 < len(event.event_id) <= 256 for event in live)
    assert all(event.context is not None and event.context.occupied_tokens is None
               and event.context.capacity_tokens == 3
               and event.context.compaction_count is None for event in live)
    assert sink.events[-1].reconciles_event_id == live[-1].event_id


@pytest.mark.asyncio
async def test_publisher_close_cancels_pending_observer_once() -> None:
    request = _request()
    publisher = _UsagePublisher(_valid_binding(request), RecordingUsageObserver(hang=True))
    publisher.start()
    publisher.publish_live(event_id="pending", tokens=TokenCounts(input_tokens=1, output_tokens=0))
    await asyncio.sleep(0)
    await publisher.close()
    assert publisher.dropped_or_failed


@pytest.mark.asyncio
async def test_codex_final_delivery_cancellation_isolated() -> None:
    request = _request()
    started = asyncio.Event()

    class SlowSink:
        async def observe_usage(self, observation: Observation) -> None:
            started.set()
            await asyncio.sleep(10)

    publisher = _UsagePublisher(_valid_binding(request), SlowSink())
    publisher.start()
    task = asyncio.create_task(publisher.publish_final(
        event_id="final", status="succeeded",
        tokens=TokenCounts(input_tokens=7, output_tokens=2),
    ))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(task, 1)
    assert publisher.dropped_or_failed
    await publisher.close()


@pytest.mark.asyncio
async def test_codex_public_gateway_repeated_cancel_during_final_delivery_preserves_proof() -> None:
    class Lifecycle:
        def __init__(self) -> None:
            self.process_result = None

        async def launch_intent(self, _launch_id: str) -> None:
            pass

        async def started(self, _receipt) -> None:
            pass

        async def finished(self, _receipt, result) -> None:
            self.process_result = result

    class CancelTwiceSink:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.cleaning = asyncio.Event()
            self.active = False
            self.cancellations = 0
            self.delivery_task: asyncio.Task[None] | None = None

        async def observe_usage(self, observation: Observation) -> None:
            if not observation.final:
                return
            self.delivery_task = asyncio.current_task()
            self.active = True
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellations += 1
                self.cleaning.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
                    raise
            finally:
                self.active = False

    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    lifecycle = Lifecycle()
    gateway = _gateway("tool", broker=_Broker())
    gateway._lifecycle = lifecycle
    sink = CancelTwiceSink()
    task = asyncio.create_task(gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    ))
    await asyncio.wait_for(sink.entered.wait(), 5)
    assert lifecycle.process_result is not None and lifecycle.process_result.stop_confirmed
    task.cancel()
    await asyncio.wait_for(sink.cleaning.wait(), 1)
    task.cancel()
    result = await asyncio.wait_for(task, 2)

    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 13
    assert "usage observation delivery incomplete" in result.telemetry.unknown_telemetry_reasons
    assert sink.cancellations == 2 and not sink.active
    assert sink.delivery_task is not None and sink.delivery_task.done()


def _valid_binding(request: SubscriptionInvocationRequest, project_id: UUID | None = None) -> WorkUnitBinding:
    effective = request.task.route.effective
    role = request.task.purpose.value if hasattr(request.task.purpose, "value") else str(request.task.purpose)
    effort = effective.effort.value if effective.effort is not None else None
    route = RouteCohort.from_members(
        (
            RouteMember(
                role=role,
                provider=effective.provider,
                model=effective.model,
                ordinal=0,
                reasoning_effort=effort,
            ),
        )
    )
    return WorkUnitBinding(
        subject=SubjectKey(project_id=project_id or uuid4(), kind="run", subject_id=request.attempt.run_id),
        work_unit_id=uuid4(),
        admitted_phase_id="implement",
        configured_route=route,
        effective_route=route,
        source=SourceBinding(
            kind="subscription_attempt",
            source_id=request.attempt.attempt_id,
            parent_id=request.attempt.task_id,
        ),
    )


@pytest.mark.asyncio
async def test_codex_gateway_implements_observable_gateway() -> None:
    broker = _Broker()
    gateway = _gateway("tool", broker=broker)
    assert isinstance(gateway, ObservableSubscriptionGateway)
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    binding = _valid_binding(request)
    sink = RecordingUsageObserver()
    result = await gateway.execute_observed(request, usage_binding=binding, usage_sink=sink)
    assert result.failure is None
    assert len(sink.events) >= 2
    live_events = [e for e in sink.events if not e.final]
    final_events = [e for e in sink.events if e.final]
    assert len(live_events) >= 1
    assert len(final_events) == 1
    live = live_events[0]
    assert live.tokens.input_tokens == 13
    assert live.tokens.output_tokens == 5
    assert live.tokens.cached_input_tokens == 2
    final = final_events[0]
    assert final.final_status == "succeeded"
    assert final.reconciles_event_id == live.event_id


@pytest.mark.asyncio
async def test_codex_strict_telemetry_policy_classifies_final_after_settlement() -> None:
    broker = _Broker()
    gateway = _gateway("tool", broker=broker)
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    strict = replace(request.task.budget.unknown_telemetry_policy, allow_unknown_cost=False)
    request = replace(
        request,
        task=replace(request.task, budget=replace(request.task.budget, unknown_telemetry_policy=strict)),
    )
    sink = RecordingUsageObserver()

    result = await gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink
    )

    assert result.failure is SubscriptionFailure.POLICY_DENIED
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 13
    finals = [event for event in sink.events if event.final]
    assert len(finals) == 1
    assert finals[0].final_status == result.failure.value
    assert finals[0].tokens.input_tokens == 13


@pytest.mark.asyncio
async def test_codex_usage_binding_validation_mismatches() -> None:
    broker = _Broker()
    gateway = _gateway("tool", broker=broker)
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    valid = _valid_binding(request)
    sink = RecordingUsageObserver()

    # Mismatched subject run_id
    bad_subject = replace(valid, subject=replace(valid.subject, subject_id=uuid4()))
    with pytest.raises(ValueError, match="subject does not match request run"):
        await gateway.execute_observed(request, usage_binding=bad_subject, usage_sink=sink)

    # Mismatched source attempt_id
    bad_source = replace(valid, source=replace(valid.source, source_id=uuid4()))
    with pytest.raises(ValueError, match="source does not match attempt identity"):
        await gateway.execute_observed(request, usage_binding=bad_source, usage_sink=sink)

    # Mismatched parent task_id
    bad_parent = replace(valid, source=replace(valid.source, parent_id=uuid4()))
    with pytest.raises(ValueError, match="parent does not match task identity"):
        await gateway.execute_observed(request, usage_binding=bad_parent, usage_sink=sink)

    # Mismatched route member
    bad_route = RouteCohort.from_members(
        (
            RouteMember(
                role="other_role",
                provider=request.task.route.effective.provider,
                model=request.task.route.effective.model,
                ordinal=0,
            ),
        )
    )
    bad_effective = replace(valid, effective_route=bad_route)
    with pytest.raises(ValueError, match="effective route does not match"):
        await gateway.execute_observed(request, usage_binding=bad_effective, usage_sink=sink)


@pytest.mark.asyncio
async def test_codex_sink_delivery_failure_isolation() -> None:
    broker = _Broker()
    gateway = _gateway("tool", broker=broker)
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    binding = _valid_binding(request)
    failing_sink = RecordingUsageObserver(fail=True)

    result = await gateway.execute_observed(request, usage_binding=binding, usage_sink=failing_sink)
    assert result.failure is None
    assert "usage observation delivery incomplete" in result.telemetry.unknown_telemetry_reasons


@pytest.mark.asyncio
async def test_codex_sink_hanging_timeout_isolation() -> None:
    broker = _Broker()
    gateway = _gateway("tool", broker=broker)
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    binding = _valid_binding(request)
    hanging_sink = RecordingUsageObserver(hang=True)

    result = await gateway.execute_observed(request, usage_binding=binding, usage_sink=hanging_sink)
    assert result.failure is None
    assert "usage observation delivery incomplete" in result.telemetry.unknown_telemetry_reasons


@pytest.mark.asyncio
async def test_codex_cancellation_preserves_final_and_settlement() -> None:
    broker = _Broker()
    gateway = _gateway("cancel", broker=broker, duration=10)
    request = _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    binding = _valid_binding(request)
    sink = RecordingUsageObserver()

    task = asyncio.create_task(
        gateway.execute_observed(request, usage_binding=binding, usage_sink=sink)
    )
    await broker.called.wait()
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(SubscriptionInterrupted) as exc_info:
        await task

    res = exc_info.value.result
    assert res.failure is SubscriptionFailure.INTERRUPTED
    assert res.launch_proof is not None
    assert res.launch_proof.stop_confirmed is True
    final_events = [e for e in sink.events if e.final]
    assert len(final_events) == 1
    assert final_events[0].final_status in ("interrupted", "cancelled")
