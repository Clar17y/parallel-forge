"""Live usage observation tests for the Claude stream-json subscription gateway."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from forge.agents.claude_gateway import _Usage
from forge.agents.subscription_usage_observations import _UsagePublisher
from forge.application.ports.subscription_gateway import (
    ObservableSubscriptionGateway,
    SubscriptionFailure,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
)
from forge.application.ports.task_usage import TaskUsageObserver
from forge.application.services.task_usage_observations import project_source
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
from test_claude_supervised import (
    _anthropic_request,
    _Broker,
    _gateway,
    _Lifecycle,
    _supported_isolation_platform,  # noqa: F401
)


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


@pytest.mark.parametrize(
    "raw_id", [None, "", " ", True, [], "i" * 256, "i" * 257, "forge:missing-event:1"]
)
@pytest.mark.asyncio
async def test_claude_public_observation_id_edges_preserve_settlement(
    tmp_path: Path, raw_id: object,
) -> None:
    peer = Path(__file__).with_name("claude_stream_peer.py").read_text()
    original = 'def send(value):\n    print(json.dumps(value, separators=(",", ":")), flush=True)'
    replacement = (
        "def send(value):\n"
        "    if value.get('type') == 'assistant' and value.get('message', {}).get('usage'):\n"
        + (
            "        value.pop('uuid', None)\n"
            if raw_id is None else f"        value['uuid'] = {raw_id!r}\n"
        )
        + "    print(json.dumps(value, separators=(\",\", \":\")), flush=True)"
    )
    assert original in peer
    script = tmp_path / "claude_metadata_peer.py"
    script.write_text(peer.replace(original, replacement))
    gateway = _gateway("usage_sum", broker=_Broker())
    gateway._installation = replace(gateway._installation, script=(str(script), "usage_sum"))
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    sink = RecordingUsageObserver()
    result = await gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    )
    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 13
    live = [event for event in sink.events if not event.final]
    assert len(live) >= 2
    assert 0 < len(live[0].event_id) <= 256
    if isinstance(raw_id, str) and raw_id.strip() and len(raw_id) <= 256 and not raw_id.startswith("forge:"):
        assert live[0].event_id == raw_id
    else:
        assert live[0].event_id.startswith(("forge:missing-event:", "forge:sha256:"))
    assert sink.events[-1].reconciles_event_id == live[-1].event_id


@pytest.mark.asyncio
async def test_claude_public_live_final_id_collision_settles(tmp_path: Path) -> None:
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    final_id = f"claude-final-{request.attempt.attempt_id}"
    peer = Path(__file__).with_name("claude_stream_peer.py").read_text()
    original = 'def send(value):\n    print(json.dumps(value, separators=(",", ":")), flush=True)'
    replacement = (
        "def send(value):\n"
        "    if value.get('type') == 'result':\n"
        f"        value['uuid'] = {final_id!r}\n"
        "    print(json.dumps(value, separators=(\",\", \":\")), flush=True)"
    )
    assert original in peer
    script = tmp_path / "claude_collision_peer.py"
    script.write_text(peer.replace(original, replacement))
    gateway = _gateway("success", broker=_Broker())
    gateway._installation = replace(gateway._installation, script=(str(script), "success"))
    sink = RecordingUsageObserver()
    result = await gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    )
    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    live = [event for event in sink.events if not event.final]
    assert live[-1].event_id == final_id
    assert sink.events[-1].reconciles_event_id == live[-1].event_id
    assert sink.events[-1].event_id != live[-1].event_id


@pytest.mark.asyncio
async def test_claude_observed_untrusted_context_and_colliding_long_ids_settle(tmp_path: Path) -> None:
    long_prefix = "provider-" + "x" * 260
    peer = Path(__file__).with_name("claude_stream_peer.py").read_text()
    original = 'def send(value):\n    print(json.dumps(value, separators=(",", ":")), flush=True)'
    replacement = (
        "assistant_events = 0\n"
        "def send(value):\n"
        "    global assistant_events\n"
        "    if value.get('type') == 'assistant' and value.get('message', {}).get('usage'):\n"
        "        assistant_events += 1\n"
        "        value['contextWindow'] = {'occupiedTokens': 9, 'capacityTokens': 3, 'compactionCount': True}\n"
        f"        value['uuid'] = {long_prefix!r} + str(assistant_events)\n"
        "    print(json.dumps(value, separators=(\",\", \":\")), flush=True)"
    )
    assert original in peer
    script = tmp_path / "claude_metadata_peer.py"
    script.write_text(peer.replace(original, replacement))
    gateway = _gateway("usage_sum", broker=_Broker())
    gateway._installation = replace(gateway._installation, script=(str(script), "usage_sum"))
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    sink = RecordingUsageObserver()

    result = await gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    )

    assert result.failure is None, result.failure_detail
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 13
    live = [event for event in sink.events if not event.final]
    assert len(live) >= 2
    assert live[0].event_id != live[1].event_id
    assert all(0 < len(event.event_id) <= 256 for event in live)
    assert all(event.context is not None and event.context.occupied_tokens is None
               and event.context.capacity_tokens == 3
               and event.context.compaction_count is None for event in live[:2])
    assert sink.events[-1].reconciles_event_id == live[-1].event_id


@pytest.mark.asyncio
async def test_publisher_close_cancels_pending_observer_once() -> None:
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    publisher = _UsagePublisher(_valid_binding(request), RecordingUsageObserver(hang=True))
    publisher.start()
    publisher.publish_live(event_id="pending", tokens=TokenCounts(input_tokens=1, output_tokens=0))
    await asyncio.sleep(0)
    await publisher.close()
    assert publisher.dropped_or_failed


@pytest.mark.asyncio
async def test_claude_final_delivery_cancellation_isolated() -> None:
    request = _anthropic_request()
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
async def test_claude_public_gateway_repeated_cancel_during_final_delivery_preserves_proof() -> None:
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

    lifecycle = _Lifecycle()
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    gateway = _gateway("success", broker=_Broker(), lifecycle=lifecycle)
    sink = CancelTwiceSink()
    task = asyncio.create_task(gateway.execute_observed(
        request, usage_binding=_valid_binding(request), usage_sink=sink,
    ))
    await asyncio.wait_for(sink.entered.wait(), 5)
    assert lifecycle.result is not None and lifecycle.result.stop_confirmed
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


def test_claude_cached_input_is_in_normalized_total() -> None:
    usage = _Usage()
    observed = usage.observe_assistant(
        {"type": "assistant", "session_id": "session", "message": {
            "id": "one", "usage": {"input_tokens": 2, "cache_read_input_tokens": 20,
                                    "cache_creation_input_tokens": 3, "output_tokens": 4}}},
        "session",
    )
    assert observed is not None
    assert observed[1].input_tokens == 25
    assert observed[1].cached_input_tokens == 23
    assert usage.input == 25
    usage.observe({"input_tokens": 2, "cache_read_input_tokens": 20,
                   "cache_creation_input_tokens": 3, "output_tokens": 4})
    assert usage.input == 25


def test_claude_partial_message_stays_unknown_until_completed() -> None:
    usage = _Usage()
    partial = usage.observe_assistant(
        {"type": "assistant", "session_id": "session", "message": {
            "id": "one", "usage": {"input_tokens": 2, "cache_read_input_tokens": 5}}},
        "session",
    )
    assert partial is not None
    assert partial[1].input_tokens == 7
    assert partial[1].output_tokens is None
    assert usage.output is None
    completed = usage.observe_assistant(
        {"type": "assistant", "session_id": "session", "message": {
            "id": "one", "usage": {"input_tokens": 2, "cache_read_input_tokens": 5,
                                    "output_tokens": 0}}},
        "session",
    )
    assert completed is not None
    assert completed[1].output_tokens == 0
    assert usage.output == 0


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
async def test_claude_gateway_implements_observable_gateway() -> None:
    broker = _Broker()
    gateway = _gateway("success", broker=broker)
    assert isinstance(gateway, ObservableSubscriptionGateway)
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
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
    final = final_events[0]
    assert final.final_status == "succeeded"
    assert final.tokens.input_tokens == 13
    assert final.tokens.output_tokens == 5
    assert final.reconciles_event_id == live.event_id


@pytest.mark.asyncio
async def test_claude_usage_binding_validation_mismatches() -> None:
    broker = _Broker()
    gateway = _gateway("success", broker=broker)
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
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
async def test_claude_sink_delivery_failure_isolation() -> None:
    broker = _Broker()
    gateway = _gateway("success", broker=broker)
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    binding = _valid_binding(request)
    failing_sink = RecordingUsageObserver(fail=True)

    result = await gateway.execute_observed(request, usage_binding=binding, usage_sink=failing_sink)
    assert result.failure is None
    assert "usage observation delivery incomplete" in result.telemetry.unknown_telemetry_reasons


@pytest.mark.asyncio
async def test_claude_sink_hanging_timeout_isolation() -> None:
    broker = _Broker()
    gateway = _gateway("success", broker=broker)
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    binding = _valid_binding(request)
    hanging_sink = RecordingUsageObserver(hang=True)

    result = await gateway.execute_observed(request, usage_binding=binding, usage_sink=hanging_sink)
    assert result.failure is None
    assert "usage observation delivery incomplete" in result.telemetry.unknown_telemetry_reasons


@pytest.mark.parametrize("scenario", ["usage_sum", "usage_reordered", "usage_repeat"])
@pytest.mark.asyncio
async def test_claude_multiple_assistant_messages_usage_sum(scenario: str) -> None:
    broker = _Broker()
    gateway = _gateway(scenario, broker=broker)
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    binding = _valid_binding(request)
    sink = RecordingUsageObserver()

    result = await gateway.execute_observed(request, usage_binding=binding, usage_sink=sink)
    assert result.failure is None
    live_events = [e for e in sink.events if not e.final]
    final_events = [e for e in sink.events if e.final]
    expected_prefixes = (
        [(13, 5), (13, 5)] if scenario == "usage_repeat"
        else [(6, 2), (13, 5), (13, 5)]
    )
    assert [(event.tokens.input_tokens, event.tokens.output_tokens) for event in live_events] == expected_prefixes
    assert [
        (project_source(tuple(sink.events[: index + 1])).input_tokens,
         project_source(tuple(sink.events[: index + 1])).output_tokens)
        for index, event in enumerate(sink.events) if not event.final
    ] == expected_prefixes
    # Each live prefix is the full source aggregate, including repeated turns.
    epochs = [e.counter_epoch for e in live_events]
    assert set(epochs) == {"source"}
    assert len(final_events) == 1
    final = final_events[0]
    # Summed tokens across the two assistant messages: (6+7=13, 2+3=5)
    assert final.tokens.input_tokens == 13
    assert final.tokens.output_tokens == 5
    assert project_source(tuple(sink.events)).input_tokens == 13
    assert project_source(tuple(sink.events)).output_tokens == 5


@pytest.mark.asyncio
async def test_claude_strict_telemetry_policy_classifies_final_after_settlement() -> None:
    broker = _Broker()
    gateway = _gateway("success", broker=broker)
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
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
async def test_claude_cancellation_preserves_final_and_settlement() -> None:
    broker = _Broker()
    gateway = _gateway("cancel", broker=broker, duration=10)
    request = _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
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
