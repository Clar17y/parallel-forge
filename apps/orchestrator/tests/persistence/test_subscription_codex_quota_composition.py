"""Pinned-shape fake Codex notifications reach durable production quota admission."""

import asyncio
import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_subscription_planning_start as planning_fixture
from forge.agents.codex_gateway import (
    CodexCapabilityReport,
    CodexInstallation,
    codex_account_identity,
)
from forge.agents.codex_runtime import CodexRuntimeAdapter
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.runs import RunService
from forge.application.services.subscription_planning import SubscriptionPlanningService
from forge.application.services.tasks import PlainTextTaskRequest, TaskService
from forge.domain.subscription import RouteSpec, SpecialistPurpose, TaskBudget
from forge.domain.subscription_quota import QuotaPolicy
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionClientLaunch
from forge.persistence.models.subscription_quota import SubscriptionQuotaObservation
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.settings import Settings
from forge.worker import composition
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_usage import _reservation
from test_task10_run_service_integration import StableInspector

from apps.orchestrator.tests.agents.capability_support import bind_fake_capability_report


@pytest.mark.integration
@pytest.mark.parametrize("known_reset", [True, False])
async def test_codex_notifications_suppress_restarted_concurrent_pollers_and_admit_one_probe(
    session_factory, tmp_path, monkeypatch, known_reset
):
    clock = [datetime.now(UTC).replace(microsecond=0)]
    reset = clock[0] + timedelta(seconds=30)
    primary = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    policy = QuotaPolicy(unknown_reset_cooldown_seconds=60)
    real_uow = composition.PostgresUnitOfWork

    def unit_of_work(*args, **kwargs):
        kwargs["quota_clock"] = lambda: clock[0]
        return real_uow(*args, **kwargs)

    monkeypatch.setattr(composition, "PostgresUnitOfWork", unit_of_work)
    monkeypatch.setattr(planning_fixture, "_route", lambda _: primary)
    factory, run, command = await planning_fixture.planning_start_case(session_factory, tmp_path)
    runs = [run]
    commands = [command]
    actor = AuthenticatedActor(
        actor_id=command.actor_id, actor_class="operator", session_id=uuid4()
    )
    for index in range(2):
        task = await TaskService(factory).create_plain_text(
            actor=actor,
            idempotency_key=f"quota-task-{index}",
            request=PlainTextTaskRequest(
                project_id=run.project_id, title=f"Queued {index}", body="Body"
            ),
        )
        queued = await RunService(
            factory,
            repository_inspector=StableInspector(tmp_path / "repo"),
            data_root=tmp_path / "data",
        ).create_run(actor=actor, idempotency_key=f"quota-run-{index}", task_id=task.id)
        start = await PostgresCommandRepository(session_factory).claim_next(
            worker_id=f"planning-{index}", lease_seconds=60
        )
        assert start is not None and start.run_id == queued.id
        runs.append(queued)
        commands.append(start)
    for command in commands:
        async with factory() as work:
            await SubscriptionPlanningService(TaskBudget(max_provider_attempts=64)).execute(
                command, work
            )

    isolated = tmp_path / "client"
    isolated.mkdir()
    client_home = isolated / "client-home"
    client_home.mkdir()
    verifications = []
    pool = "codex" if known_reset else None
    installation = CodexInstallation(
        executable=sys.executable,
        cwd=str(isolated),
        model=primary.model,
        effort=primary.effort.value,
        quota_limit_id=pool,
        client_home=str(client_home),
        account=codex_account_identity("codex@example.invalid"),
        executable_digest=hashlib.sha256(
            Path(sys.executable).resolve(strict=True).read_bytes()
        ).hexdigest(),
        duration_seconds=10,
        script=(
            str(Path(__file__).parents[1] / "agents/codex_notification_peer.py"),
            "quota",
            primary.model,
            primary.effort.value,
            str(int(reset.timestamp())),
        ),
    )

    def verify(value, scope):
        assert value == installation
        verifications.append(value)
        # Only the capability source and client process are fake. The adapter
        # registration, gateway, broker and durable lifecycle are Forge's own.
        report = CodexCapabilityReport(
            supported=True,
            installed_version="0.153.4",
            account_kind="chatgpt",
            native_tools_isolated=True,
            model=primary.model,
            effort=primary.effort.value,
            quota_limit_id=pool,
            client_home=str(client_home),
            account=value.account,
            executable_digest=value.executable_digest,
        )
        return bind_fake_capability_report(
            report,
            scope=scope,
            client_version="0.153.4",
            executable_digest=value.executable_digest,
            client_home=value.client_home,
            account=value.account,
            verifier_id="fake-codex-verification",
        )

    settings = Settings(
        _env_file=None,
        data_root=tmp_path,
        prompt_root=Path(__file__).resolve().parents[4] / "agents",
        provider_secret_reference="",
        subscription_attempt_budget=_reservation(),
        subscription_quota_policy=policy,
        subscription_installations_path=None,
    )
    # Ambient personal installations must not remap this fixture's quota pool.
    assert settings.subscription_quota_policy == policy

    def handlers():
        return composition.compose_worker_handlers(
            settings,
            session_factory,
            subscription_adapters=(
                CodexRuntimeAdapter(
                    installation, SimpleNamespace(verify=verify), now=lambda: clock[0]
                ),
            ),
        )

    first = handlers()
    try:
        outcome = await first.subscription_invocations("first-client").run_once()
        assert outcome is not None and outcome.attempt.settlement.disposition == "quota_deferred"
    finally:
        await first.aclose()
    assert len(verifications) == 1
    key = policy.key_for(primary)
    eligible_at = reset if known_reset else clock[0] + timedelta(seconds=60)

    async def assert_counts(attempts):
        async with unit_of_work(session_factory, quota_policy=policy) as work:
            status = await work.quota.status(key)
            assert status.status == "blocked" and status.probe_attempt_id is None
            assert status.reason == "codex_account_usage_exhausted"
            assert (
                await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
                == attempts
            )
            assert (
                await work.session.scalar(
                    select(func.count()).select_from(SubscriptionQuotaObservation)
                )
                == attempts
            )
            launches = list((await work.session.scalars(select(SubscriptionClientLaunch))).all())
            assert len(launches) == attempts
            assert all(
                row.state == "terminal" and row.terminal_payload["stop_confirmed"] is True
                for row in launches
            )
            totals = [await work.subscription_budget.usage(run.id) for run in runs]
            assert sum(item.consumed.provider_attempts for item in totals) == attempts
            assert sum(item.consumed.repairs for item in totals) == 0
            assert sum(item.outstanding.provider_attempts for item in totals) == 0
            assert await work.scheduler._active_count() == 0
            for run in runs:
                envelope = await work.subscription.envelope_for_run(run.id)
                assert envelope.route_for(SpecialistPurpose.PRIMARY).effective == primary
            return status

    status = await assert_counts(1)
    assert status.observed_at == clock[0] and status.next_eligible_at == eligible_at
    assert status.reset_at == (reset if known_reset else None)
    assert status.retry_basis == ("known_reset" if known_reset else "probe_cooldown")
    restarted = [handlers(), handlers()]
    try:
        blocked = await asyncio.gather(
            *[
                value.subscription_invocations(f"restarted-{index}").run_once()
                for index, value in enumerate(restarted)
            ]
        )
        assert blocked == [None, None] and len(verifications) == 1
        assert await assert_counts(1) == status
        clock[0] = eligible_at
        results = await asyncio.gather(
            *[
                value.subscription_invocations(f"probe-{index}").run_once()
                for index, value in enumerate(restarted)
            ]
        )
        assert sum(result is not None for result in results) == 1
        assert len(verifications) == 2
        after = await assert_counts(2)
        assert after.observed_at == eligible_at and after.recovered_at is None
        # The old reset is now expired. Reconfirmed exhaustion uses the bounded
        # unknown-reset cooldown; a clock crossing never declares availability.
        assert after.reset_at is None and after.retry_basis == "probe_cooldown"
        assert after.next_eligible_at == eligible_at + timedelta(seconds=60)
    finally:
        for value in restarted:
            await value.aclose()
