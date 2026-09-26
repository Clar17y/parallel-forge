"""Real composition reaches the human gate with a fake provider and PostgreSQL."""

from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.client_process import (
    ClientProcessReceipt,
    ClientProcessResult,
    terminal_launch_proof,
)
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_broker import BrokerDenied
from forge.application.services.subscription_planning import SubscriptionPlanningService
from forge.domain.approval import ApprovalGate
from forge.domain.plan import PlanOutput
from forge.domain.run import RunState
from forge.domain.subscription import TaskBudget
from forge.domain.tool import ToolName
from forge.settings import Settings
from forge.worker.composition import compose_worker_handlers
from test_scheduler_acceptance import _remove_disposable_subscription_rows, _route  # noqa: F401
from test_subscription_planning_start import planning_start_case
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_composed_subscription_executor_binds_tools_lifecycle_and_human_gate(
    session_factory, tmp_path
):
    factory, original, command = await planning_start_case(session_factory, tmp_path)
    (tmp_path / "repo" / "README.md").write_text("Bound source", encoding="utf-8")
    async with factory() as work:
        await SubscriptionPlanningService(TaskBudget(max_provider_attempts=64)).execute(
            command, work
        )
    settings = Settings(
        data_root=tmp_path,
        prompt_root=Path(__file__).resolve().parents[4] / "agents",
        provider_secret_reference="",
        subscription_attempt_budget=_reservation(),
    )
    calls, brokers = [], []

    class Adapter:
        route = _route("openai")

        def gateway_for(self, request, *, broker, lifecycle):
            brokers.append(broker)

            class Gateway:
                async def execute(self, value):
                    assert request == value and request.budget == _reservation()
                    calls.append(request.attempt.attempt_id)
                    receipt = await broker(
                        ProviderToolCall(
                            call_key="read-once",
                            thread_id="thread",
                            turn_id="turn",
                            name=ToolName.REPOSITORY_READ_FILE.value,
                            arguments={"path": "README.md"},
                        )
                    )
                    assert receipt["status"] == "succeeded"
                    # Explicit fake supervisor evidence, persisted through the
                    # same lifecycle supplied to official adapters in production.
                    process = ClientProcessReceipt(str(uuid4()), 12345, "synthetic-start", 0.0)
                    result = ClientProcessResult(
                        process, 0, (), 100, "", 0, False, False, "exited", True
                    )
                    await lifecycle.launch_intent(process.launch_id)
                    await lifecycle.started(process)
                    await lifecycle.finished(process, result)
                    return SubscriptionInvocationResult(
                        attempt=request.attempt,
                        decision=PlanOutput(
                            summary="One bounded change",
                            assumptions=(),
                            affected_components=("apps",),
                            steps=("Implement and validate",),
                            required_checks=("unit",),
                            risks=("Regression",),
                            security_considerations=(),
                            dependency_changes=(),
                        ),
                        telemetry=_known(),
                        launch_proof=terminal_launch_proof(result),
                    )

            return Gateway()

    unconfigured = compose_worker_handlers(settings, session_factory)
    try:
        assert unconfigured.subscription_invocations is not None
        assert await unconfigured.subscription_invocations("unconfigured").run_once() is None
    finally:
        await unconfigured.aclose()
    handlers = compose_worker_handlers(
        settings, session_factory, subscription_adapters=(Adapter(),)
    )
    try:
        assert handlers.subscription_invocations is not None
        worker = handlers.subscription_invocations("production-one")
        assert worker._candidates is handlers.subscription_candidates
        assert worker._acceptance is handlers.subscription_decision_recovery._acceptance
        outcome = await worker.run_once()
        assert outcome is not None and outcome.application is not None
        assert calls == [outcome.admission.attempt.attempt_id]
        assert await worker.run_once() is None
        async with factory() as work:
            run = await work.runs.get(original.id)
            assert run.state is RunState.AWAITING_PLAN_APPROVAL
            assert run.pending_gate is ApprovalGate.PLAN
            assert run.pending_evidence_digest == outcome.application.evidence_digest
            usage = await work.subscription_budget.usage(run.id)
            assert (
                usage.consumed.provider_attempts == 1 and usage.outstanding.provider_attempts == 0
            )
            records = await work.tool_calls.list_for_run(run.id)
            assert len(records) == 1 and records[0].subscription_attempt_id == calls[0]
        with pytest.raises(BrokerDenied):
            await brokers[0](
                ProviderToolCall(
                    call_key="late-read",
                    thread_id="thread",
                    turn_id="turn",
                    name=ToolName.REPOSITORY_READ_FILE.value,
                    arguments={"path": "README.md"},
                )
            )
    finally:
        await handlers.aclose()
