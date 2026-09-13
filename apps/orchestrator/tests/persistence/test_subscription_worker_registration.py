"""Trusted registrations use the public worker's actual recovery, poll and drain path."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessSupervisor,
    terminal_launch_proof,
)
from forge.application.services.projects import PolicyUpdateRequest, ProjectService
from forge.application.services.runs import RunService
from forge.domain.approval import ApprovalGate
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.run import RunState
from forge.domain.subscription import OperatorProfile, RolePreference, RouteSpec, SpecialistPurpose
from forge.domain.subscription_quota import QuotaPoolKey
from forge.domain.tool import ToolName
from forge.evaluations.credentials import assert_credential_free
from forge.evaluations.subscription_fixtures import get_acceptance_command_specs
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionClientLaunch
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_task10_run_service_integration import StableInspector, _seed_project_task


@pytest.mark.integration
@pytest.mark.parametrize("blocked", [False, True])
async def test_registered_worker_processes_preserve_quota_and_human_gate(
    session_factory, migrated_database_url, tmp_path, blocked
):
    actor, project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    factory = lambda: PostgresUnitOfWork(session_factory)
    await ProjectService(factory).update_policy(
        actor=actor,
        project_id=project_id,
        idempotency_key="worker-registration-check-policy",
        request=PolicyUpdateRequest(
            expected_policy_version=1, commands=get_acceptance_command_specs()
        ),
    )
    primary = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=primary),),
    )
    key = QuotaPoolKey("openai", "local", "subscription-allowance_only")
    observed = datetime.now(UTC)
    async with factory() as work:
        await work.subscription.select_project_profile(project_id, profile)
        if blocked:
            await work.quota.report_exhaustion(
                key,
                QuotaExhaustion(observed, "operator_report", observed + timedelta(hours=1)),
                actor_id=actor.actor_id,
                idempotency_key="before-worker-start",
            )
        await work.commit()
    run = await RunService(
        factory,
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    ).create_run(actor=actor, idempotency_key="worker-registration-run", task_id=task_id)
    (tmp_path / "repo/README.md").write_text(
        "Bound input through the normal worker", encoding="utf-8"
    )
    (tmp_path / "isolated-client").mkdir()
    (tmp_path / "client-home").mkdir()
    store = SubscriptionRuntimeStatusStore(session_factory)
    clients, terminals = [], []

    async def start(registered):
        session = await ClientProcessSupervisor().start(
            ClientLaunchSpec(
                argv=(
                    sys.executable,
                    str(Path(__file__).with_name("subscription_worker_registration_peer.py")),
                    str(tmp_path),
                    "registered" if registered else "empty",
                ),
                cwd=tmp_path,
                environment={"FORGE_DATABASE_URL": migrated_database_url},
                allowed_environment=frozenset({"FORGE_DATABASE_URL"}),
                duration_seconds=45,
            )
        )
        clients.append(session)
        assert await session.receive() == {"operation": "worker-starting", "registered": registered}
        return session

    async def stop(session):
        acknowledged = False
        try:
            await session.send({"operation": "stop"})
            assert await session.receive() == {"operation": "worker-stopped", "settled": True}
            acknowledged = True
        finally:
            terminal = await session.close(completed=acknowledged)
            assert terminal.stop_confirmed
            terminals.append(terminal_launch_proof(terminal).model_dump(mode="json"))
            clients.remove(session)

    async def wait_for(state, active_workers):
        async with asyncio.timeout(20):
            while True:
                async with factory() as work:
                    current = await work.runs.get(run.id)
                status = await store.status()
                workers = [row for row in status["workers"] if row["state"] == "current"]
                if current.state is state and len(workers) == active_workers:
                    return current, workers
                await asyncio.sleep(0.025)

    async def assert_durable(expected_attempts):
        async with factory() as work:
            usage = await work.subscription_budget.usage(run.id)
            assert usage.consumed.provider_attempts == expected_attempts
            assert usage.consumed.repairs == usage.outstanding.provider_attempts == 0
            assert await work.scheduler._active_count() == 0
            assert (
                await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
                == expected_attempts
            )
            status = await work.quota.status(key)
            assert status.status == ("blocked" if blocked else "unknown")
            if blocked:
                assert status.observed_at == observed
                assert status.reset_at == observed + timedelta(hours=1)
                assert status.retry_basis == "known_reset" and status.probe_attempt_id is None
            envelope = await work.subscription.envelope_for_run(run.id)
            assert envelope.route_for(SpecialistPurpose.PRIMARY).effective == primary

    try:
        # The ordinary default worker handles its queued start command and
        # reports an empty registry, without bypassing subscription admission.
        empty = await start(False)
        _, workers = await wait_for(RunState.PLANNING, 1)
        assert workers[0]["routes"] == []
        await stop(empty)
        await assert_durable(0)

        # Two fresh worker processes share the same PostgreSQL scheduler and
        # concrete Codex registration. Capability and model clients are fake.
        first = await start(True)
        second = await start(True)
        state = RunState.PLANNING if blocked else RunState.AWAITING_PLAN_APPROVAL
        current, workers = await wait_for(state, 2)
        assert all(
            len(row["routes"]) == 1 and row["routes"][0]["model"] == primary.model
            for row in workers
        )
        # Allow both normal pollers several opportunities to attempt admission.
        await asyncio.sleep(0.15)
        await stop(first)
        await stop(second)
        await assert_durable(0 if blocked else 1)
        restarted = await start(True)
        await wait_for(state, 1)
        await asyncio.sleep(0.15)
        await stop(restarted)
        await assert_durable(0 if blocked else 1)
        if not blocked:
            assert current.pending_gate is ApprovalGate.PLAN and current.pending_evidence_digest
            async with factory() as work:
                calls = await work.tool_calls.list_for_run(run.id)
                assert len(calls) == 1 and calls[0].tool_name is ToolName.REPOSITORY_READ_FILE
                result = await work.session.get(
                    SubscriptionAttemptResult, calls[0].subscription_attempt_id
                )
                assert result.accepted and result.disposition == "plan_approval"
                launches = list(
                    (await work.session.scalars(select(SubscriptionClientLaunch))).all()
                )
                assert len(launches) == 1 and launches[0].state == "terminal"
                proof = launches[0].terminal_payload
                assert proof["stop_confirmed"] and proof["pid"] > 0 and proof["process_identity"]
        status = await store.status()
        assert len(status["workers"]) == 4
        assert all(row["state"] == "stopped" for row in status["workers"])
        evidence = {
            "scenario": "public-worker-registration-blocked"
            if blocked
            else "public-worker-registration-plan",
            "run_id": str(run.id),
            "state": state.value,
            "provider_attempts": 0 if blocked else 1,
            "worker_registrations": status,
            "worker_processes": terminals,
            "provider_and_capability_source": "fake Codex peer and explicit test report",
            "repository_inspector": "StableInspector fixture",
            "review": "selected-primary source self-review",
        }
        encoded = json.dumps(evidence, default=str, indent=2)
        assert_credential_free(encoded)
        assert migrated_database_url not in encoded
        output = (
            Path(__file__).resolve().parents[4]
            / ".llm-output/root-worker-registration-manifests"
            / str(run.id)
        )
        output.mkdir(parents=True)
        (output / "result.json").write_text(encoded + "\n", encoding="utf-8")
    finally:
        for session in tuple(clients):
            terminal = await session.close(completed=False)
            assert terminal.stop_confirmed
