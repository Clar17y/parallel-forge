"""Queued primary work reaches the human gate through real durable boundaries."""

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_planning import SubscriptionPlanningService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.approval import ApprovalGate
from forge.domain.plan import PlanOutput
from forge.domain.run import RunState
from forge.domain.subscription import TaskBudget
from forge.worker.subscription_invocation import (
    SubscriptionInvocationSession,
    SubscriptionInvocationWorker,
)
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_planning_start import planning_start_case
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
@pytest.mark.parametrize("publication_error", [False, True])
async def test_worker_applies_queued_primary_plan_to_human_gate(
    session_factory, tmp_path, monkeypatch, publication_error
):
    from forge.agents.subscription_protocol import ProviderToolCall
    from forge.domain.tool import ToolName
    from forge.settings import Settings
    from forge.worker.delivery_runtime import DeliveryRuntime
    from forge.worker.subscription_session import ControlledSubscriptionSessionFactory
    from forge.worker.subscription_tools import SubscriptionToolServiceFactory

    factory, original, command = await planning_start_case(session_factory, tmp_path)
    (tmp_path / "repo" / "README.md").write_text("Bound planning source", encoding="utf-8")
    async with factory() as work:
        await SubscriptionPlanningService(TaskBudget(max_provider_attempts=64)).execute(
            command, work
        )
    calls = []
    revoked = []

    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    tools_for = SubscriptionToolServiceFactory(
        factory,
        artifacts=artifacts,
        delivery=DeliveryRuntime(Settings(data_root=tmp_path / "data"), session_factory, artifacts),
    )

    def gateway_for(admission, request, broker, lifecycle):
        class Gateway:
            async def execute(self, value):
                assert value == request and value.attempt_budget == _reservation()
                calls.append(value.attempt.attempt_id)
                receipt = await broker(
                    ProviderToolCall(
                        call_key="read-once",
                        thread_id="test-thread",
                        turn_id="test-turn",
                        name=ToolName.REPOSITORY_READ_FILE.value,
                        arguments={"path": "README.md"},
                    )
                )
                assert receipt["status"] == "succeeded"
                proof = await record_stopped_launch(session_factory, admission)
                return SubscriptionInvocationResult(
                    attempt=value.attempt,
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
                    launch_proof=proof,
                )

        return Gateway()

    bound_sessions = ControlledSubscriptionSessionFactory(
        factory, tools=tools_for, gateway=gateway_for
    )

    def session_for(admission, request):
        session = bound_sessions(admission, request)

        async def revoke():
            await session.revoke()
            revoked.append(request.attempt.attempt_id)

        return SubscriptionInvocationSession(session.gateway, revoke)

    if publication_error:

        async def unavailable(*args, **kwargs):
            raise RuntimeError("artifact store unavailable")

        monkeypatch.setattr(artifacts, "put_bytes", unavailable)
    worker = SubscriptionInvocationWorker(
        factory,
        session_for,
        artifacts=artifacts,
        owner="primary-worker",
        reservation=_reservation(),
    )
    if publication_error:
        from forge.persistence.models.subscription_results import SubscriptionAttemptResult

        with pytest.raises(RuntimeError, match="artifact store unavailable"):
            await worker.run_once()
        assert calls == revoked and len(calls) == 1
        async with factory() as work:
            run = await work.runs.get(original.id)
            assert run.state is RunState.PLANNING and run.pending_gate is None
            result = await work.session.get(SubscriptionAttemptResult, calls[0])
            assert result.disposition == "decision_pending" and not result.accepted
            usage = await work.subscription_budget.usage(run.id)
            assert usage.consumed.provider_attempts == 1
        assert await worker.run_once() is None
        from pathlib import Path

        from forge.settings import Settings
        from forge.worker.composition import compose_worker_handlers

        restarted = compose_worker_handlers(
            Settings(
                data_root=tmp_path,
                prompt_root=Path(__file__).resolve().parents[4] / "agents",
                provider_secret_reference="",
                pricing_catalog_path=None,
            ),
            session_factory,
        )
        try:
            recovery = restarted.subscription_decision_recovery
            assert recovery is not None
            report = await recovery.reconcile_all()
            assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
            async with factory() as work:
                run = await work.runs.get(original.id)
                assert run.state is RunState.AWAITING_PLAN_APPROVAL
            assert len(calls) == 1
        finally:
            await restarted.aclose()
        return
    outcome = await worker.run_once()
    assert outcome is not None and outcome.application is not None
    assert calls == revoked == [outcome.admission.attempt.attempt_id]
    assert outcome.attempt.settlement.disposition == "decision_pending"
    async with factory() as work:
        run = await work.runs.get(original.id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert run.pending_gate is ApprovalGate.PLAN
        assert run.pending_evidence_digest == outcome.application.evidence_digest
        usage = await work.subscription_budget.usage(run.id)
        assert usage.consumed.provider_attempts == 1 and usage.outstanding.provider_attempts == 0
        records = await work.tool_calls.list_for_run(run.id)
        assert len(records) == 1
        assert records[0].subscription_attempt_id == outcome.admission.attempt.attempt_id
    assert await worker.run_once() is None
