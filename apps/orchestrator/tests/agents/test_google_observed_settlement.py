"""Public observed gateway regressions through scripted supervised client exchanges."""

import asyncio
from dataclasses import replace

import pytest
from forge.agents.client_process import ClientSettlementUncertain
from forge.application.ports.subscription_gateway import SubscriptionFailure
from test_antigravity_runtime import gateway, request
from test_gemini_gateway import _gateway as gemini_gateway
from test_gemini_gateway import _google_request, _Lifecycle
from test_google_usage_observations import FakeUsageObserver, _make_subscription_binding


class _CancelTwiceSink:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.active = False
        self.cancellations = 0
        self.delivery_task: asyncio.Task[None] | None = None

    async def observe_usage(self, observation) -> None:
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


@pytest.mark.parametrize("client", ["gemini", "antigravity"])
@pytest.mark.asyncio
async def test_google_public_subscription_repeated_cancel_after_settlement(
    tmp_path, client: str
) -> None:
    if client == "gemini":
        lifecycle = _Lifecycle()
        runtime = gemini_gateway(tmp_path, "no_tools", lifecycle=lifecycle)
        invocation = _google_request()
        model = "gemini-test"
    else:
        runtime, lifecycle = gateway(tmp_path, "success")
        invocation = request()
        model = "gemini-3.8-flash-medium"
    binding = _make_subscription_binding(
        invocation.attempt.run_id, invocation.attempt.attempt_id,
        invocation.attempt.task_id, model=model,
    )
    sink = _CancelTwiceSink()
    task = asyncio.create_task(runtime.execute_observed(
        invocation, usage_binding=binding, usage_sink=sink,
    ))
    await asyncio.wait_for(sink.entered.wait(), 5)
    assert lifecycle.results and lifecycle.results[-1].stop_confirmed
    task.cancel()
    await asyncio.wait_for(sink.cleaning.wait(), 1)
    task.cancel()
    result = await asyncio.wait_for(task, 2)

    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == (10 if client == "gemini" else 13)
    assert "usage observation delivery incomplete" in result.telemetry.unknown_telemetry_reasons
    assert sink.cancellations == 2 and not sink.active
    assert sink.delivery_task is not None and sink.delivery_task.done()


@pytest.mark.asyncio
async def test_gemini_observed_actual_supervised_acp_exchange(tmp_path) -> None:
    runtime = gemini_gateway(tmp_path, "no_tools")
    invocation = _google_request()
    binding = _make_subscription_binding(
        invocation.attempt.run_id,
        invocation.attempt.attempt_id,
        invocation.attempt.task_id,
        model="gemini-test",
    )
    sink = FakeUsageObserver()

    result = await runtime.execute_observed(invocation, usage_binding=binding, usage_sink=sink)

    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 10
    assert result.telemetry.output_tokens == 4
    assert len(sink.observations) == 1
    assert sink.observations[0].final_status == "succeeded"
    assert sink.observations[0].tokens.input_tokens == 10


@pytest.mark.parametrize(
    ("scenario", "expected_input", "expected_output"),
    [("large_usage", 10_000_017, 5), ("sparse_zero_usage", 0, None)],
)
@pytest.mark.asyncio
async def test_antigravity_observed_real_counter_scale_and_sparse_zero(
    tmp_path, scenario, expected_input, expected_output
) -> None:
    runtime, _ = gateway(tmp_path, scenario)
    invocation = request()
    binding = _make_subscription_binding(
        invocation.attempt.run_id,
        invocation.attempt.attempt_id,
        invocation.attempt.task_id,
    )
    sink = FakeUsageObserver()

    result = await runtime.execute_observed(invocation, usage_binding=binding, usage_sink=sink)

    assert result.failure is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == expected_input
    assert result.telemetry.output_tokens == expected_output
    finals = [event for event in sink.observations if event.final]
    assert len(finals) == 1
    assert finals[0].final_status == "succeeded"
    assert finals[0].tokens.input_tokens == expected_input
    assert finals[0].tokens.output_tokens == expected_output


@pytest.mark.asyncio
async def test_antigravity_observed_final_follows_uncertain_settlement(tmp_path, monkeypatch) -> None:
    runtime, _ = gateway(tmp_path, "success")
    original_start = runtime.supervisor.start

    class UncertainClose:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        async def close(self, *args, **kwargs):
            actual = await self.session.close(*args, **kwargs)
            raise ClientSettlementUncertain(
                replace(actual, outcome="stop_uncertain", stop_confirmed=False)
            )

    async def start(*args, **kwargs):
        return UncertainClose(await original_start(*args, **kwargs))

    monkeypatch.setattr(runtime.supervisor, "start", start)
    invocation = request()
    binding = _make_subscription_binding(
        invocation.attempt.run_id,
        invocation.attempt.attempt_id,
        invocation.attempt.task_id,
    )
    sink = FakeUsageObserver()

    result = await runtime.execute_observed(invocation, usage_binding=binding, usage_sink=sink)

    assert result.failure is SubscriptionFailure.UNCERTAIN
    assert result.launch_proof is not None and not result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 13
    finals = [item for item in sink.observations if item.final]
    assert len(finals) == 1
    assert finals[0].final_status == result.failure.value
    assert finals[0].tokens.input_tokens == 13
