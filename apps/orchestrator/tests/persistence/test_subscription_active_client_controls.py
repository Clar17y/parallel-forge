"""A7: operator stops a supervised fake client after an actual managed write."""

import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.agents.client_process import (
    ClientProcessReceipt,
    ClientProcessSupervisor,
    ProcessIdentityStatus,
    _process_token,
)
from forge.agents.gemini_gateway import GeminiCapabilityReport, GeminiInstallation
from forge.agents.gemini_runtime import GeminiRuntimeAdapter
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.application.services.subscription_broker import BrokerDenied
from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.subscription_quota import (
    QuotaExhaustionReport,
    SubscriptionQuotaService,
)
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.policy import RunnerMode
from forge.domain.subscription import (
    DelegateDecision,
    SpecialistPurpose,
    TaskBudget,
    UnknownTelemetryPolicy,
)
from forge.domain.subscription_quota import QuotaPolicy
from forge.domain.subscription_task_controls import SubscriptionTaskControlRequest
from forge.domain.tool import ToolName
from forge.evaluations.subscription_fixtures import clean_slow_unit_markers, wait_slow_unit_entered
from forge.persistence.models.subscription import SubscriptionClientLaunch, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.worker.composition import compose_worker_handlers
from sqlalchemy import select
from subscription_counter_manifest import retain_counter_manifest
from subscription_docker_stop import DockerCheckObservation
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import (
    PATHS,
    PRIMARY,
    WRITER,
    CounterScript,
    prepared_counter_case,
)
from test_subscription_counter_docker import counter_runner_image  # noqa: F401

from apps.orchestrator.tests.agents.capability_support import bind_fake_capability_report


class ActiveClientScript(CounterScript):
    """Keep the established fake primary; supervise the actual fake Gemini peer."""

    def __init__(self, root, *, run_check=False):
        super().__init__()
        self.root = root
        self.run_check = run_check
        (root / "launches").mkdir(parents=True)
        (root / "home").mkdir()
        self.boundary = asyncio.Event()
        self.sessions = []
        self.descendants = []
        self.brokers = []

    async def execute(self, request, broker):
        decision = await super().execute(request, broker)
        if isinstance(decision, DelegateDecision):
            # This scenario explicitly approves a continuation with another
            # unknown subscription measurement. Keep production defaults intact.
            decision = replace(
                decision,
                child_tasks=tuple(
                    replace(
                        child,
                        budget=replace(
                            child.budget,
                            unknown_telemetry_policy=UnknownTelemetryPolicy(
                                max_uncertain_attempts=2
                            ),
                        ),
                    )
                    for child in decision.child_tasks
                ),
            )
        return decision

    def adapter(self, route):
        if route != WRITER:
            return super().adapter(route)
        owner = self

        class Session:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            async def send(self, value):
                return await self.wrapped.send(value)

            async def receive(self):
                value = await self.wrapped.receive()
                if value and value.get("method") == "session/update":
                    update = value["params"]["update"]
                    if update.get("sessionUpdate") == "agent_thought_chunk":
                        marker = json.loads(update["content"]["text"])
                        assert marker["fixture_active_stop"] is True
                        pid = marker["mcp_pid"]
                        token = _process_token(pid)
                        assert token
                        owner.descendants.append(
                            ClientProcessReceipt("fixture-mcp-descendant", pid, token, 0.0)
                        )
                        owner.boundary.set()
                return value

            async def close(self, **kwargs):
                return await self.wrapped.close(**kwargs)

        class Supervisor(ClientProcessSupervisor):
            async def start(self, spec, **kwargs):
                session = await super().start(spec, **kwargs)
                owner.sessions.append(session)
                return Session(session)

        class Adapter:
            route = WRITER

            def gateway_for(self, request, *, broker, lifecycle):
                owner.requests.append(request)
                owner.brokers.append(broker)

                class Broker:
                    async def __call__(self, call):
                        receipt = await broker(call)
                        owner.receipts.append(receipt)
                        return receipt

                    async def revoke(self):
                        await broker.revoke()

                # Explicit fake capability proof; the official runtime registry
                # is not enabled by this fixture or by the local assertions.
                installation = GeminiInstallation(
                    executable=sys.executable,
                    cwd=str(owner.root / "launches"),
                    home=str(owner.root / "home"),
                    model=WRITER.model,
                    effort=WRITER.effort.value,
                    account="test-account",
                    executable_digest="c" * 64,
                    duration_seconds=45,
                    script=(
                        str(Path(__file__).parents[1] / "agents/gemini_acp_peer.py"),
                        "production_active_check_stop"
                        if owner.run_check
                        else "production_active_stop",
                        "--acp",
                    ),
                )
                report = GeminiCapabilityReport(
                    installed_version="0.59.0",
                    client_home=str((owner.root / "home").resolve()),
                    subscription_auth=True,
                    model=WRITER.model,
                    effort=WRITER.effort.value,
                    tools_disabled=True,
                    billing_never=True,
                    isolated_config=True,
                    acp_mcp_supported=True,
                    account=installation.account,
                    executable_digest=installation.executable_digest,
                )

                def verify(value, scope):
                    assert value == installation
                    return bind_fake_capability_report(
                        report,
                        scope=scope,
                        client_version="0.59.0",
                        executable_digest=value.executable_digest,
                        client_home=value.home,
                        account=value.account,
                        verifier_id="fake-gemini-verification",
                    )

                adapter = GeminiRuntimeAdapter(
                    installation,
                    SimpleNamespace(verify=verify),
                )
                gateway = adapter.gateway_for(request, broker=Broker(), lifecycle=lifecycle)
                # Observe real process/receipt boundaries without replacing
                # the production adapter's request binding or capability checks.
                gateway._supervisor = Supervisor()
                return gateway

        return Adapter()


@pytest.mark.integration
@pytest.mark.parametrize("action", ["pause", "cancel"])
@pytest.mark.parametrize(
    "boundary",
    ["write", pytest.param("docker-check", marks=pytest.mark.docker)],
)
async def test_active_client_stop_preserves_write_and_quota_across_restart(
    session_factory, tmp_path, action, boundary, request, monkeypatch
):
    running_check = boundary == "docker-check"
    runner_image = request.getfixturevalue("counter_runner_image") if running_check else ""
    docker = DockerCheckObservation(monkeypatch, runner_image) if running_check else None
    script = ActiveClientScript(tmp_path / "client", run_check=running_check)
    case = await prepared_counter_case(
        session_factory,
        tmp_path,
        script=script,
        primary_budget=TaskBudget(
            max_provider_attempts=64,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=2),
        ),
        runner_mode=RunnerMode.DOCKER if running_check else RunnerMode.TRUSTED_HOST,
        runner_image=runner_image,
    )
    handlers = case.handlers
    operation = None
    stop = asyncio.Event()
    actor = LocalOperatorProfileActor()
    key = str(uuid4())
    try:
        if running_check:
            async with case.factory() as work:
                tree = Path((await work.runs.get(case.run.id)).worktree_path)
            clean_slow_unit_markers(tree)
        operation = asyncio.create_task(case.worker.run_once(stop_event=stop))
        async with asyncio.timeout(20):
            await script.boundary.wait()
        assert not operation.done() and len(script.sessions) == len(script.descendants) == 1
        processes = [script.sessions[0].receipt, script.descendants[0]]
        assert all(
            ClientProcessSupervisor.identity_status(value) is ProcessIdentityStatus.MATCH
            for value in processes
        )
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            tree = Path(run.worktree_path)
        if docker is not None:
            assert await asyncio.to_thread(wait_slow_unit_entered, tree, 10)
            assert not operation.done()
            await docker.assert_running()
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            task = await work.session.get(SubscriptionTask, script.child_id)
            assert task.state == "running" and await work.scheduler._active_count() == 1
            body = SubscriptionTaskControlRequest(
                action=action,
                expected_run_version=run.version,
                expected_task_version=task.version,
                reason="A7 active controlled Docker check"
                if running_check
                else "A7 active client after controlled write",
            )
        original_files = {
            path: hashlib.sha256((tree / path).read_bytes()).hexdigest() for path in PATHS
        }
        assert "value + 1" in (tree / PATHS[0]).read_text()
        assert [receipt["tool_name"] for receipt in script.receipts] == [
            ToolName.REPOSITORY_READ_FILE.value,
            ToolName.REPOSITORY_WRITE_FILE.value,
        ]

        async def control(request, mutation_key):
            return await SubscriptionTaskControlService(case.factory).control(
                run_id=case.run.id,
                task_id=script.child_id,
                actor=actor,
                idempotency_key=mutation_key,
                request=request,
            )

        receipt = await control(body, key)
        assert receipt.status == f"{action}_requested"
        assert await control(body, key) == receipt
        # The production lease heartbeat sees the persisted control; no test
        # cancellation or synthetic terminal launch receipt drives this stop.
        async with asyncio.timeout(20):
            outcome = await asyncio.shield(operation)
        assert outcome.attempt.result.failure is SubscriptionFailure.INTERRUPTED
        assert outcome.application is None and outcome.attempt.result.decision is None
        assert outcome.attempt.result.telemetry.input_tokens is None
        assert outcome.attempt.result.telemetry.tool_call_count == (3 if running_check else 2)
        assert outcome.attempt.result.telemetry.named_check_count == int(running_check)
        proof = outcome.attempt.result.launch_proof
        assert proof.stop_confirmed and proof.pid == processes[0].pid
        assert all(
            ClientProcessSupervisor.identity_status(value) is ProcessIdentityStatus.GONE
            for value in processes
        )
        if docker is not None:
            await docker.assert_gone()
        with pytest.raises(BrokerDenied):
            await script.brokers[0](
                ProviderToolCall(
                    call_key="late-write",
                    thread_id="late",
                    turn_id="late",
                    name=ToolName.REPOSITORY_WRITE_FILE.value,
                    arguments={"path": PATHS[0], "content": "late unapproved replacement"},
                )
            )

        # Recreate composition before applying the durable requested stop.
        await handlers.aclose()
        handlers = compose_worker_handlers(
            case.settings,
            session_factory,
            subscription_adapters=(script.adapter(PRIMARY), script.adapter(WRITER)),
        )
        recovery = SubscriptionTaskControlService(case.factory)
        if running_check:
            assert (await recovery.reconcile_all()).deferred == 1
        # Reuse the public worker's startup order: terminal tool verification
        # and effect reconciliation precede the requested task-stop settlement.
        await handlers.tool_recovery.recover_all(allow_unresolved=True)
        effects = SubscriptionEffectRecovery(case.factory, terminal_verifier=handlers.tool_recovery)
        reconciled_effects = await effects.reconcile_all()
        assert reconciled_effects == int(running_check)
        assert await effects.reconcile_all() == 0
        assert (await recovery.reconcile_all()).stopped == 1
        assert (await recovery.reconcile_all()).stopped == 0
        before_resume = await SubscriptionTaskQuery(session_factory).tasks(case.run.id)
        projected = next(row for row in before_resume["tasks"] if row["task_id"] == script.child_id)
        assert projected["pause_requested"] if action == "pause" else projected["cancel_requested"]
        assert projected["unsettled_effects"] == 0
        async with case.factory() as work:
            assert await work.scheduler._active_count() == 0
            attempt_id = outcome.admission.attempt.attempt_id
            result = await work.session.get(SubscriptionAttemptResult, attempt_id)
            retained_digest = result.result_digest
            assert not result.accepted
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == attempt_id
                )
            )
            assert launch.state == "terminal" and launch.terminal_payload["stop_confirmed"] is True
            envelope = await work.subscription.envelope_for_run(case.run.id)
            assert envelope.route_for(SpecialistPurpose.PRIMARY).effective == PRIMARY

        quota_key = QuotaPolicy().key_for(WRITER)
        quota_body = QuotaExhaustionReport(
            provider=quota_key.provider,
            account=quota_key.account,
            pool=quota_key.pool,
            reason="Operator reported fixture allowance exhaustion while task is stopped",
        )
        quota_service = SubscriptionQuotaService(case.factory, now=lambda: datetime.now(UTC))
        quota = await quota_service.report_exhaustion(
            actor=actor, idempotency_key=key, request=quota_body
        )
        assert quota.status == "blocked" and quota.retry_basis == "probe_cooldown"
        resumed = None
        if action == "pause":
            async with case.factory() as work:
                run = await work.runs.get(case.run.id)
                task = await work.session.get(SubscriptionTask, script.child_id)
                resume_body = SubscriptionTaskControlRequest(
                    action="resume",
                    expected_run_version=run.version,
                    expected_task_version=task.version,
                    reason="Resume retained partial work with quota admission",
                    pause_receipt_id=receipt.receipt_id,
                )
            resume_key = str(uuid4())
            resumed = await control(resume_body, resume_key)
            assert resumed.status == "queued"
            assert await control(resume_body, resume_key) == resumed
        async with case.factory() as work:
            before_poll = await work.subscription_budget.usage(case.run.id)
            assert before_poll.consumed.provider_attempts == 3
            assert before_poll.consumed.named_checks == int(running_check)
            assert before_poll.consumed.repairs == (1 if action == "pause" else 0)
            # Ordinary stopped-attempt resume reserves its existing repair's
            # next-attempt unit; the later quota-skipped poll must not add one.
            assert before_poll.outstanding.provider_attempts == (1 if action == "pause" else 0)
        # Poll only the stopped specialist route. A cancelled child may correctly
        # wake its unchanged primary; that continuation is outside this hook.
        isolated = compose_worker_handlers(
            case.settings, session_factory, subscription_adapters=(script.adapter(WRITER),)
        )
        try:
            assert (
                await isolated.subscription_invocations("restarted-active-stop").run_once() is None
            )
        finally:
            await isolated.aclose()
        assert await control(body, key) == receipt
        assert (
            await quota_service.report_exhaustion(
                actor=actor, idempotency_key=key, request=quota_body
            )
            == quota
        )
        async with case.factory() as work:
            assert await work.subscription_budget.usage(case.run.id) == before_poll
            assert (
                await work.session.get(SubscriptionAttemptResult, attempt_id)
            ).result_digest == retained_digest
            assert await work.quota.status(quota_key) == quota
            assert await work.scheduler._active_count() == 0
        assert len(script.requests) == 3 and len(script.sessions) == 1
        assert {
            path: hashlib.sha256((tree / path).read_bytes()).hexdigest() for path in PATHS
        } == original_files
        manifest = await retain_counter_manifest(
            case.factory,
            FilesystemArtifactStore(case.settings.artifact_root),
            case.fixture,
            script,
            run_id=case.run.id,
            tmp_path=tmp_path,
            grade=None,
            scenario=f"A7-active-docker-check-{action}"
            if running_check
            else f"A7-active-client-{action}",
            worker_check_repair_sequences=0,
            quota_status=quota,
            restarted_before_handoff=False,
            operator_view={
                "hook": "check_dispatched_before_result_receipt"
                if running_check
                else "active_task_stop_committed_before_provider_result",
                "original_control": receipt,
                "resume": resumed,
                "snapshot_after_restart": before_resume,
                "snapshot_after_recovery": await SubscriptionTaskQuery(session_factory).tasks(
                    case.run.id
                ),
                "file_digests_preserved": original_files,
                "result_digest_preserved": retained_digest,
                "physical_processes": [
                    {
                        "pid": value.pid,
                        "process_identity": value.process_start_token,
                        "role": role,
                    }
                    for value, role in zip(
                        processes, ("fake-client", "mcp-descendant"), strict=True
                    )
                ],
                "physical_processes_alive_before_control": True,
                "physical_processes_gone_after_control": True,
                "physical_proof_scope": "Gemini fake client and its actual MCP descendant; primary planning/delegation retains synthetic fixture lifecycle",
                "late_broker_effect_denied": True,
                "attempts_or_repairs_from_suppressed_poll": 0,
                "ordinary_interruption_resume_repairs": 1 if action == "pause" else 0,
                "interrupted_effects_reconciled_once": reconciled_effects,
                "docker_container_before_control": docker.before if docker is not None else None,
                "docker_container_after_control": docker.after if docker is not None else None,
            },
        )
        if docker is not None:
            evidence = json.loads(manifest.read_text(encoding="utf-8"))["evidence"]
            results = [
                json.loads((manifest.parent / digest).read_text(encoding="utf-8"))
                for digest, item in evidence["artifacts"].items()
                if item["media_type"] == "application/vnd.forge.command-result+json"
            ]
            assert len(results) == 1
            result = results[0]
            assert result["command_name"] == "slow-unit"
            assert result["runner_mode"] == "docker" and result["image_digest"] == runner_image
            assert result["unsandboxed"] is result["network_enabled"] is False
            assert result["timed_out"] is False and result["exit_code"] == 137
            receipts = [
                json.loads((manifest.parent / digest).read_text(encoding="utf-8"))
                for digest, item in evidence["artifacts"].items()
                if item["media_type"] == "application/vnd.forge.named-check-receipt+json"
            ]
            assert len(receipts) == 1 and receipts[0]["caller_cancelled"] is True
            assert not (tree / ".forge-acceptance/slow-unit.release").exists()
    finally:
        stop.set()
        try:
            if operation is not None and not operation.done():
                async with asyncio.timeout(15):
                    await asyncio.shield(operation)
        finally:
            try:
                await asyncio.gather(*(session.close() for session in script.sessions))
            finally:
                await handlers.aclose()
