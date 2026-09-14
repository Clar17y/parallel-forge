"""Claude wire evidence reaches durable admission through the real attempt runner."""

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_scheduler_acceptance as scheduler_fixture
from forge.agents.claude_gateway import ClaudeCapabilityReport, ClaudeInstallation
from forge.agents.claude_runtime import ClaudeRuntimeAdapter
from forge.agents.runtime_factory import AgentRuntimeFactory
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.run import RunSnapshot
from forge.domain.subscription import ReasoningEffort, RouteSpec, SpecialistPurpose
from forge.domain.subscription_quota import QuotaPolicy, QuotaRoutePool
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionClientLaunch
from forge.persistence.models.subscription_quota import SubscriptionQuotaObservation
from forge.worker.subscription_broker import DurableClientProcessLifecycle
from forge.worker.subscription_runtime import SubscriptionAttemptRunner
from sqlalchemy import func, select
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
)
from test_subscription_attempt_runner_integration import request_for
from test_subscription_quota import _factory
from test_subscription_usage import _reservation

from apps.orchestrator.tests.agents.capability_support import bind_fake_capability_report


@pytest.mark.integration
@pytest.mark.parametrize("known_reset", [True, False])
async def test_claude_specialist_exhaustion_survives_worker_recreation_and_single_probe(
    session_factory, persisted_run, tmp_path, monkeypatch, known_reset
):
    clock = [datetime.now(UTC).replace(microsecond=0)]
    reset = clock[0] + timedelta(seconds=30)
    primary = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    specialist = RouteSpec(
        provider="anthropic",
        client="claude_code",
        model="claude-test",
        effort=ReasoningEffort.MEDIUM,
    )
    policy = QuotaPolicy(
        route_pools=(
            QuotaRoutePool(
                provider="anthropic",
                client="claude_code",
                account="test-account",
                pool="test-allowance",
            ),
        ),
        unknown_reset_cooldown_seconds=60,
    )
    factory = _factory(session_factory, clock, policy=policy)
    monkeypatch.setattr(scheduler_fixture, "_route", lambda _: specialist)
    runs = [persisted_run]
    async with factory() as work:
        for _ in range(2):
            run = RunSnapshot(
                id=uuid4(),
                project_id=persisted_run.project_id,
                task_id=persisted_run.task_id,
                policy_version=1,
            )
            await work.runs.create(run)
            runs.append(run)
        for run in runs:
            parent = await _admit_run(work, run, (primary, specialist))
            await _enqueue(
                work,
                run.id,
                provider="anthropic",
                worktree="constraint-tree",
                parent_id=parent,
                paths=("apps",),
            )
        await work.commit()
    windows = frozenset({"five_hour"}) if known_reset else frozenset()
    launches = []

    async def execute(admission, owner, work_factory):
        launches.append(admission.attempt.attempt_id)
        request = replace(request_for(admission), attempt_budget=_reservation())

        class NoTools:
            revoked = False

            async def __call__(self, call):
                raise AssertionError("This quota fixture grants no tools")

            async def revoke(self):
                self.revoked = True

        broker = NoTools()
        # Explicit test capability assertions, never evidence about installed
        # clients, signed-in accounts, billing enforcement or tool isolation.
        report = ClaudeCapabilityReport(
            installed_version="2.1.263",
            subscription_auth=True,
            model=specialist.model,
            effort=specialist.effort.value,
            builtins_disabled=True,
            hooks_disabled=True,
            strict_mcp=True,
            allowance_only_enforced=True,
            quota_limit_types=windows,
            client_home=str(tmp_path.resolve()),
            account="test-account",
            executable_digest="b" * 64,
        )
        installation = ClaudeInstallation(
            executable=sys.executable,
            cwd=str(tmp_path),
            model=specialist.model,
            effort=specialist.effort.value,
            client_home=str(tmp_path.resolve()),
            account="test-account",
            executable_digest="b" * 64,
            duration_seconds=5,
            quota_limit_types=windows,
            script=(
                str(Path(__file__).parents[1] / "agents/claude_notification_peer.py"),
                "quota",
                str(int(reset.timestamp())),
            ),
        )

        def verify(value, scope):
            assert value == installation
            return bind_fake_capability_report(
                report,
                scope=scope,
                client_version="2.1.263",
                executable_digest=value.executable_digest,
                client_home=value.client_home,
                account=value.account,
                verifier_id="fake-claude-verification",
            )

        adapter = ClaudeRuntimeAdapter(
            installation,
            SimpleNamespace(verify=verify),
            now=lambda: clock[0],
        )
        client = AgentRuntimeFactory(subscription_adapters=(adapter,)).subscription_gateway_for(
            request,
            broker=broker,
            lifecycle=DurableClientProcessLifecycle(
                work_factory, attempt_id=admission.attempt.attempt_id, worker_identity=owner
            ),
        )
        outcome = await SubscriptionAttemptRunner(work_factory).execute(
            admission, request, client, broker.revoke
        )
        assert broker.revoked
        assert outcome.settlement.disposition == "quota_deferred"
        assert outcome.result.quota_exhaustion is not None
        assert outcome.result.launch_proof.stop_confirmed
        assert (
            await SubscriptionDecisionExecutor(work_factory).settle(admission, outcome.result)
        ).replayed

    async def inspect(attempts):
        async with factory() as work:
            status = await work.quota.status(policy.key_for(specialist))
            assert status.status == "blocked" and status.probe_attempt_id is None
            assert status.reason == "claude_account_usage_exhausted" and status.recovered_at is None
            assert (await work.quota.status(policy.key_for(primary))).status == "unknown"
            assert (
                await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
                == attempts
            )
            observations = list(
                (await work.session.scalars(select(SubscriptionQuotaObservation))).all()
            )
            assert len(observations) == attempts
            assert {row.reason for row in observations} == {"claude_account_usage_exhausted"}
            assert {(row.provider, row.account, row.pool) for row in observations} == {
                ("anthropic", "test-account", "test-allowance")
            }
            rows = list((await work.session.scalars(select(SubscriptionClientLaunch))).all())
            assert len(rows) == attempts
            assert all(
                row.state == "terminal" and row.terminal_payload["stop_confirmed"] for row in rows
            )
            totals = [await work.subscription_budget.usage(run.id) for run in runs]
            assert sum(item.consumed.provider_attempts for item in totals) == attempts
            assert sum(item.consumed.repairs for item in totals) == 0
            assert sum(item.outstanding.provider_attempts for item in totals) == 0
            assert await work.scheduler._active_count() == 0
            for run in runs:
                envelope = await work.subscription.envelope_for_run(run.id)
                assert envelope.route_for(SpecialistPurpose.PRIMARY).effective == primary
                assert (
                    envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION).effective
                    == specialist
                )
            return status

    admitted = await SubscriptionDecisionExecutor(factory).admit_next("original", _reservation())
    assert admitted is not None
    await execute(admitted, "original", factory)
    status = await inspect(1)
    eligible = reset if known_reset else clock[0] + timedelta(seconds=60)
    assert status.observed_at == clock[0] and status.next_eligible_at == eligible
    assert status.reset_at == (reset if known_reset else None)
    assert status.retry_basis == ("known_reset" if known_reset else "probe_cooldown")
    # New executor, runner and UOW objects have no previous process-local state.
    factories = [_factory(session_factory, clock, policy=policy) for _ in range(2)]
    executors = [SubscriptionDecisionExecutor(value) for value in factories]
    blocked = await asyncio.gather(
        *(
            value.admit_next(f"blocked-{index}", _reservation())
            for index, value in enumerate(executors)
        )
    )
    assert blocked == [None, None] and len(launches) == 1
    assert await inspect(1) == status
    clock[0] = eligible
    probes = await asyncio.gather(
        *(
            value.admit_next(f"probe-{index}", _reservation())
            for index, value in enumerate(executors)
        )
    )
    assert sum(value is not None for value in probes) == 1
    index, probe = next((index, value) for index, value in enumerate(probes) if value is not None)
    await execute(probe, f"probe-{index}", factories[index])
    after = await inspect(2)
    assert len(launches) == 2 and after.observed_at == eligible
    assert after.reset_at is None and after.retry_basis == "probe_cooldown"
    assert after.next_eligible_at == eligible + timedelta(seconds=60)
