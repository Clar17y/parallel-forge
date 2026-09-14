"""Composed workers persist real supervised fake-ACP receipts and usage."""

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_subscription_plan_gate as plan_fixture
from forge.agents.gemini_gateway import GeminiCapabilityReport, GeminiInstallation
from forge.agents.gemini_runtime import GeminiRuntimeAdapter
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.domain.subscription import SpecialistPurpose
from forge.persistence.models.subscription import SubscriptionClientLaunch, SubscriptionTask
from forge.persistence.models.subscription_quota import SubscriptionQuotaObservation
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.settings import Settings
from forge.worker.composition import compose_worker_handlers
from sqlalchemy import func, select
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_delegation_application import delegation_case
from test_subscription_usage import _reservation

from apps.orchestrator.tests.agents.capability_support import bind_fake_capability_report


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["production", "rpc_429"])
async def test_composed_google_specialist_preserves_primary_and_durable_settlement(
    session_factory,
    tmp_path,
    monkeypatch,
    scenario,
):
    google = replace(_route("google"), client="gemini_cli", model="gemini-test")

    async def admit_profile(work, run, routes, **kwargs):
        return await _admit_run(work, run, (routes[0], google), **kwargs)

    monkeypatch.setattr(plan_fixture, "_admit_run", admit_profile)
    factory, primary, children, _ = await delegation_case(session_factory, tmp_path)
    assert (
        await SubscriptionDecisionApplication(factory).apply_delegation(primary.attempt.attempt_id)
    ).accepted
    isolated = tmp_path / "isolated-client"
    isolated.mkdir()
    home = tmp_path / "isolated-auth"
    home.mkdir()
    verifications = []
    installation = GeminiInstallation(
        executable=sys.executable,
        cwd=str(isolated),
        home=str(home),
        model=google.model,
        effort=google.effort.value,
        account="test-account",
        executable_digest="c" * 64,
        duration_seconds=10,
        script=(
            str(Path(__file__).parents[1] / "agents/gemini_acp_peer.py"),
            scenario,
            "--acp",
        ),
    )

    def verify(value, scope):
        assert value == installation
        verifications.append(value)
        # Only this report and client are fake. Registration and attempt binding
        # use the same implementation as a trusted production installation.
        report = GeminiCapabilityReport(
            installed_version="0.59.0",
            client_home=value.home,
            subscription_auth=True,
            model=google.model,
            effort=google.effort.value,
            tools_disabled=True,
            billing_never=True,
            isolated_config=True,
            acp_mcp_supported=True,
            account=value.account,
            executable_digest=value.executable_digest,
        )
        return bind_fake_capability_report(
            report,
            scope=scope,
            client_version="0.59.0",
            executable_digest=value.executable_digest,
            client_home=value.home,
            account=value.account,
            verifier_id="fake-gemini-verification",
        )

    handlers = compose_worker_handlers(
        Settings(
            data_root=tmp_path,
            prompt_root=Path(__file__).resolve().parents[4] / "agents",
            provider_secret_reference="",
            subscription_attempt_budget=_reservation(),
        ),
        session_factory,
        subscription_adapters=(GeminiRuntimeAdapter(installation, SimpleNamespace(verify=verify)),),
    )
    try:
        outcome = await handlers.subscription_invocations("supervised-google").run_once()
        assert outcome is not None and len(verifications) == 1
        assert outcome.admission.task.task_id == children[0].task_id
        assert outcome.admission.task.route.effective == google
        assert outcome.attempt.settlement.disposition == (
            "handoff" if scenario == "production" else "failed"
        )
        assert await handlers.subscription_invocations("restarted-poller").run_once() is None
        assert len(verifications) == 1
        if scenario == "rpc_429":
            assert outcome.attempt.result.failure is SubscriptionFailure.THROTTLED
        async with factory() as work:
            result = await work.session.get(
                SubscriptionAttemptResult, outcome.admission.attempt.attempt_id
            )
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == outcome.admission.attempt.attempt_id,
                )
            )
            assert result is not None and launch is not None
            assert launch.state == "terminal" and launch.terminal_payload["stop_confirmed"] is True
            envelope = await work.subscription.envelope_for_run(primary.task.run_id)
            assert envelope.route_for(SpecialistPurpose.PRIMARY) == primary.task.route
            parent = await work.session.get(SubscriptionTask, primary.task.task_id)
            assert parent.state == "queued"
            usage = await work.subscription_budget.usage(primary.task.run_id)
            assert (
                usage.consumed.provider_attempts == 3 and usage.outstanding.provider_attempts == 0
            )
            assert (
                await work.session.scalar(
                    select(func.count()).select_from(SubscriptionQuotaObservation)
                )
                == 0
            )
    finally:
        await handlers.aclose()
