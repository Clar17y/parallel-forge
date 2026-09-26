"""Opt-in browser proof for active specialist feedback and task controls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from forge.agents.client_process import ClientProcessSupervisor, ProcessIdentityStatus
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.subscription_quota import (
    QuotaExhaustionReport,
    SubscriptionQuotaService,
)
from forge.domain.subscription import (
    ForwardFeedbackDecision,
    SpecialistPurpose,
    TaskBudget,
    UnknownTelemetryPolicy,
)
from forge.domain.subscription_quota import QuotaPolicy
from forge.evaluations.credentials import assert_credential_free
from forge.persistence.models import ApiMutation
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_feedback import SubscriptionTaskFeedback
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from sqlalchemy import func, select

from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - imported autouse fixture
)
from scripts.dev import DefaultCommandRunner
from tests.acceptance.process_harness import ForgeProcessHarness, free_loopback_port
from tests.acceptance.test_subscription_operator_process import _retain_evidence

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FORGE_SUBSCRIPTION_BROWSER_TEST") != "1",
        reason="requires explicit local browser acceptance setup (Node 24 and Playwright Chromium)",
    ),
]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def _wait_web(process: Any, origin: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert process.poll() is None, "Next.js exited before browser acceptance"
        try:
            if httpx.get(origin, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("Next.js did not become ready")


class ActiveBrowserController:
    """Bridge browser milestones to the in-process supervised fake client."""

    def __init__(self, case: Any, script: Any, session_factory: Any, tree: Path) -> None:
        self.case = case
        self.script = script
        self.session_factory = session_factory
        self.tree = tree
        self.operations: list[asyncio.Task[Any]] = []
        self.reset_at: datetime | None = None

    async def start_first(self) -> None:
        operation = asyncio.create_task(self.case.worker.run_once())
        self.operations.append(operation)
        async with asyncio.timeout(20):
            await self.script.boundary.wait()
        assert not operation.done()
        assert len(self.script.sessions) == len(self.script.descendants) == 1
        self._assert_processes(0, ProcessIdentityStatus.MATCH)

    async def finish_first(self) -> dict[str, object]:
        outcome = await self._finish(0)
        return {
            "stop_confirmed": outcome.attempt.result.launch_proof.stop_confirmed,
            "partial_write": self._partial_write(),
        }

    async def forward_feedback(self) -> dict[str, object]:
        outcome = await self.case.worker.run_once()
        assert outcome is not None
        assert outcome.admission.task.purpose is SpecialistPurpose.PRIMARY
        assert outcome.application is not None
        assert outcome.application.disposition == "feedback_forwarded"
        async with self.case.factory() as work:
            row = await work.session.scalar(
                select(SubscriptionTaskFeedback).where(
                    SubscriptionTaskFeedback.run_id == self.case.run.id,
                    SubscriptionTaskFeedback.task_id == self.script.child_id,
                )
            )
            assert row is not None
            return {"status": row.state, "receipt_id": str(row.id)}

    async def block_quota(self) -> dict[str, object]:
        key = QuotaPolicy().key_for(self._writer_route())
        self.reset_at = datetime.now(UTC) + timedelta(seconds=12)
        status = await SubscriptionQuotaService(self.case.factory).report_exhaustion(
            actor=LocalOperatorProfileActor(),
            idempotency_key=str(uuid4()),
            request=QuotaExhaustionReport(
                provider=key.provider,
                account=key.account,
                pool=key.pool,
                reason="Browser acceptance quota fence",
                reset_at=self.reset_at,
            ),
        )
        assert status.status == "blocked"
        return {"status": status.status, "reset_at": self.reset_at.isoformat()}

    async def probe_quota(self) -> dict[str, object]:
        before = await self.snapshot()
        assert self.reset_at is not None and datetime.now(UTC) < self.reset_at
        assert await self.case.worker.run_once() is None
        after = await self.snapshot()
        assert after["attempt_count"] == before["attempt_count"]
        assert after["repair_debits"] == before["repair_debits"]
        return {"outcome": "deferred", "before": before, "after": after}

    async def start_second(self) -> dict[str, object]:
        assert self.reset_at is not None
        delay = (self.reset_at - datetime.now(UTC)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay + 0.25)
        self.script.boundary.clear()
        operation = asyncio.create_task(self.case.worker.run_once())
        self.operations.append(operation)
        async with asyncio.timeout(20):
            await self.script.boundary.wait()
        assert not operation.done()
        assert len(self.script.sessions) == len(self.script.descendants) == 2
        self._assert_processes(1, ProcessIdentityStatus.MATCH)
        feedback = self.script.requests[-1].untrusted_context["operator_feedback"]
        assert len(feedback) == 1
        return {"feedback_receipt_id": feedback[0]["receipt_id"]}

    async def finish_second(self) -> dict[str, object]:
        outcome = await self._finish(1)
        return {"stop_confirmed": outcome.attempt.result.launch_proof.stop_confirmed}

    async def wait_for_settled_control(self) -> dict[str, object]:
        deadline = time.monotonic() + 30
        latest: dict[str, object] | None = None
        while time.monotonic() < deadline:
            page = await SubscriptionTaskQuery(self.session_factory).tasks(self.case.run.id)
            assert page is not None
            latest = next(task for task in page["tasks"] if task["task_id"] == self.script.child_id)
            expected = "cancelled" if latest["cancel_requested"] else "paused"
            control = latest["control"]
            if control is not None and control["status"] == expected:
                return {"status": expected}
            await asyncio.sleep(0.1)
        raise AssertionError(f"task control did not settle after worker restart: {latest!r}")

    async def snapshot(self) -> dict[str, object]:
        key = QuotaPolicy().key_for(self._writer_route())
        scope = f"run:{self.case.run.id}:task:{self.script.child_id}"
        async with self.case.factory() as work:
            task = await work.session.get(SubscriptionTask, self.script.child_id)
            scheduled = await work.session.get(SubscriptionScheduledTask, self.script.child_id)
            assert task is not None and scheduled is not None
            feedback_states = list(
                await work.session.scalars(
                    select(SubscriptionTaskFeedback.state)
                    .where(
                        SubscriptionTaskFeedback.run_id == self.case.run.id,
                        SubscriptionTaskFeedback.task_id == self.script.child_id,
                    )
                    .order_by(SubscriptionTaskFeedback.created_at)
                )
            )
            attempt_count = int(
                await work.session.scalar(
                    select(func.count())
                    .select_from(SubscriptionAttempt)
                    .where(SubscriptionAttempt.task_row_id == self.script.child_id)
                )
                or 0
            )
            repair_debits = int(
                await work.session.scalar(
                    select(func.count())
                    .select_from(SubscriptionRepairDebit)
                    .join(
                        SubscriptionAttempt,
                        SubscriptionAttempt.id == SubscriptionRepairDebit.attempt_id,
                    )
                    .where(SubscriptionAttempt.task_row_id == self.script.child_id)
                )
                or 0
            )
            unsettled_effects = int(
                await work.session.scalar(
                    select(func.count())
                    .select_from(SubscriptionScheduledEffect)
                    .where(
                        SubscriptionScheduledEffect.run_id == self.case.run.id,
                        SubscriptionScheduledEffect.task_id == self.script.child_id,
                        SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                    )
                )
                or 0
            )

            async def mutation_count(action: str) -> int:
                return int(
                    await work.session.scalar(
                        select(func.count())
                        .select_from(ApiMutation)
                        .where(ApiMutation.scope == scope, ApiMutation.action == action)
                    )
                    or 0
                )

            usage = await work.subscription_budget.usage(self.case.run.id, self.script.child_id)
            quota = await work.quota.status(key)
            feedback_mutations = await mutation_count("subscription.task.feedback")
            control_mutations = await mutation_count("subscription.task.control")
            task_snapshot = {
                "state": scheduled.state,
                "version": task.version,
                "pause_requested": task.pause_requested or scheduled.pause_requested,
                "cancel_requested": task.cancel_requested or scheduled.cancel_requested,
                "owned_paths": list(scheduled.owned_paths),
            }
            provider_attempts_consumed = usage.consumed.provider_attempts
            provider_attempts_outstanding = usage.outstanding.provider_attempts
            quota_status = quota.status
        digest = hashlib.sha256()
        for path in sorted(("src/counter.py", "tests/test_counter.py")):
            digest.update(path.encode())
            digest.update((self.tree / path).read_bytes())
        return {
            "task": task_snapshot,
            "feedback_states": feedback_states,
            "attempt_count": attempt_count,
            "repair_debits": repair_debits,
            "feedback_mutations": feedback_mutations,
            "control_mutations": control_mutations,
            "provider_attempts_consumed": provider_attempts_consumed,
            "provider_attempts_outstanding": provider_attempts_outstanding,
            "quota_status": quota_status,
            "unsettled_effects": unsettled_effects,
            "partial_digest": digest.hexdigest(),
            "partial_write": self._partial_write(),
        }

    async def close(self) -> None:
        for operation in self.operations:
            if not operation.done():
                operation.cancel()
        if self.operations:
            await asyncio.gather(*self.operations, return_exceptions=True)
        await asyncio.gather(
            *(session.close() for session in self.script.sessions),
            return_exceptions=True,
        )
        await self.case.handlers.aclose()

    async def _finish(self, index: int) -> Any:
        async with asyncio.timeout(30):
            outcome = await asyncio.shield(self.operations[index])
        assert outcome.attempt.result.failure is SubscriptionFailure.INTERRUPTED
        assert outcome.application is None
        assert outcome.attempt.result.launch_proof.stop_confirmed
        self._assert_processes(index, ProcessIdentityStatus.GONE)
        return outcome

    def _assert_processes(self, index: int, expected: ProcessIdentityStatus) -> None:
        processes = (
            self.script.sessions[index].receipt,
            self.script.descendants[index],
        )
        assert all(
            ClientProcessSupervisor.identity_status(value) is expected for value in processes
        )

    def _partial_write(self) -> bool:
        return "value + 1" in (self.tree / "src/counter.py").read_text(encoding="utf-8")

    def _writer_route(self) -> Any:
        return next(
            request.task.route.effective
            for request in self.script.requests
            if request.task.purpose is SpecialistPurpose.ROUTINE_IMPLEMENTATION
        )


@pytest.mark.integration
async def test_active_specialist_feedback_and_controls_across_browser_tabs(
    test_database_url: str,
    session_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path.cwd()
    web_root = root / "apps" / "web"
    monkeypatch.syspath_prepend(str(root / "apps" / "orchestrator" / "tests" / "persistence"))
    from test_subscription_active_client_controls import ActiveClientScript
    from test_subscription_counter_acceptance import prepared_counter_case

    class BrowserActiveScript(ActiveClientScript):
        async def execute(self, request: Any, broker: Any) -> Any:
            pending = request.untrusted_context.get("pending_worker_feedback")
            if pending is not None:
                assert request.task.purpose is SpecialistPurpose.PRIMARY
                return ForwardFeedbackDecision(
                    run_id=request.task.run_id,
                    task_id=UUID(pending["task_id"]),
                    feedback_receipt_id=UUID(pending["receipt_id"]),
                    feedback_digest=pending["feedback_digest"],
                )
            return await super().execute(request, broker)

    node = shutil.which("node")
    assert node is not None, "Node 24 is required"
    runner = DefaultCommandRunner()
    version = runner.run([node, "--version"], timeout=5)
    assert version.returncode == 0 and version.stdout.startswith("v24.")
    next_cli = runner.run(
        [node, "-p", "require.resolve('next/dist/bin/next')"], cwd=web_root, timeout=5
    )
    assert next_cli.returncode == 0, "install locked web dependencies before acceptance"
    assert not list(web_root.glob(".env*")), "browser acceptance needs a web cwd without dotenv"

    script = BrowserActiveScript(tmp_path / "client")
    case = await prepared_counter_case(
        session_factory,
        tmp_path,
        script=script,
        primary_budget=TaskBudget(
            max_provider_attempts=64,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=2),
        ),
    )
    async with case.factory() as work:
        tree = Path((await work.runs.get(case.run.id)).worktree_path)
    controller = ActiveBrowserController(case, script, session_factory, tree)

    web_origin = f"http://127.0.0.1:{free_loopback_port()}"
    public_data = tmp_path / "public-data"
    public_data.mkdir()
    harness = ForgeProcessHarness(
        database_url=test_database_url,
        data_root=public_data,
        prompt_root=root / "agents",
        web_origin=web_origin,
        subscription_only=True,
    )
    loop = asyncio.get_running_loop()
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    web_process: Any | None = None
    evidence: dict[str, object] | None = None
    next_types_path = web_root / "next-env.d.ts"
    next_types_before = next_types_path.read_bytes()

    def on_loop(coroutine: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=60)
        except BaseException:
            future.cancel()
            raise

    main_error: BaseException | None = None
    try:
        await controller.start_first()
        await asyncio.to_thread(harness.start_api)
        await asyncio.to_thread(harness.start_worker)

        class Control(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/configuration":
                    self.reply(
                        {
                            "bootstrap": harness.bootstrap_token(),
                            "run_id": str(case.run.id),
                            "task_id": str(script.child_id),
                        }
                    )
                elif self.path == "/active/snapshot":
                    self.reply(on_loop(controller.snapshot()))
                else:
                    self.reply({}, status=404)

            def do_POST(self) -> None:
                try:
                    if self.path == "/active/first/finish":
                        body = on_loop(controller.finish_first())
                    elif self.path == "/active/feedback/forward":
                        body = on_loop(controller.forward_feedback())
                    elif self.path == "/active/quota/block":
                        body = on_loop(controller.block_quota())
                    elif self.path == "/active/quota/probe":
                        body = on_loop(controller.probe_quota())
                    elif self.path == "/active/second/start":
                        body = on_loop(controller.start_second())
                    elif self.path == "/active/second/finish":
                        body = on_loop(controller.finish_second())
                    elif self.path == "/worker/restart":
                        body = {"pid": harness.restart_worker()}
                        body.update(on_loop(controller.wait_for_settled_control()))
                    elif self.path == "/api/restart":
                        body = {"pid": harness.restart_api()}
                    else:
                        self.reply({}, status=404)
                        return
                    self.reply(body)
                except Exception as error:  # noqa: BLE001 - bounded test bridge diagnostics
                    self.reply({"error": type(error).__name__}, status=500)

            def reply(self, body: object, *, status: int = 200) -> None:
                content = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "LOCALAPPDATA",
                "APPDATA",
                "PROGRAMFILES",
                "PROGRAMFILES(X86)",
            }
        } | {
            "FORGE_WEB_ORIGIN": web_origin,
            "FORGE_API_INTERNAL_ORIGIN": harness.base_url,
            "FORGE_E2E_WEB_ORIGIN": web_origin,
            "FORGE_E2E_CONTROL_ORIGIN": f"http://127.0.0.1:{server.server_port}",
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        web_process = runner.spawn(
            "subscription-active-browser-web",
            [
                node,
                next_cli.stdout.strip(),
                "dev",
                "--hostname",
                "127.0.0.1",
                "--port",
                web_origin.rsplit(":", 1)[1],
            ],
            cwd=web_root,
            env=environment,
        )
        await asyncio.to_thread(_wait_web, web_process, web_origin)
        result = await asyncio.to_thread(
            runner.run,
            [node, str(root / "tests" / "acceptance" / "subscription_active_browser.mjs")],
            cwd=web_root,
            env=environment,
            timeout=300,
        )
        assert_credential_free(result.stdout + result.stderr)
        assert result.returncode == 0, result.stderr[-6000:]
        evidence = json.loads(result.stdout)
        assert evidence["provider_calls"] is False
        assert evidence["final"] == await controller.snapshot()
    except BaseException as error:
        main_error = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        try:
            await controller.close()
        except BaseException as error:  # noqa: BLE001 - continue all owned cleanup
            cleanup_errors.append(error)

        try:
            if web_process is not None:
                await asyncio.to_thread(web_process.kill_tree)
                deadline = time.monotonic() + 5
                while web_process.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                assert web_process.poll() is not None, "Next.js termination is unconfirmed"
        except BaseException as error:  # noqa: BLE001 - continue all owned cleanup
            cleanup_errors.append(error)

        try:
            current_types = next_types_path.read_bytes()
            if current_types != next_types_before:
                try:
                    expected = next_types_before.replace(b"\r\n", b"\n").replace(
                        b'"./.next/types/', b'"./.next/dev/types/'
                    )
                    assert current_types.replace(b"\r\n", b"\n") == expected
                finally:
                    next_types_path.write_bytes(next_types_before)
        except BaseException as error:  # noqa: BLE001 - continue all owned cleanup
            cleanup_errors.append(error)

        try:
            if server is not None:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=5)
                assert not server_thread.is_alive()
        except BaseException as error:  # noqa: BLE001 - continue all owned cleanup
            cleanup_errors.append(error)

        try:
            await asyncio.to_thread(harness.close)
        except BaseException as error:  # noqa: BLE001 - continue all owned cleanup
            cleanup_errors.append(error)

        if cleanup_errors:
            if main_error is not None:
                for err in cleanup_errors:
                    main_error.add_note(f"Cleanup failure: {type(err).__name__}: {err}")
            else:
                if len(cleanup_errors) == 1:
                    raise cleanup_errors[0]
                raise ExceptionGroup("Active browser cleanup failures", cleanup_errors)

    assert evidence is not None
    assert all(process.poll() is not None for process in harness._processes)
    evidence["terminal_public_processes"] = [
        {"pid": process.pid, "return_code": process.returncode} for process in harness._processes
    ]
    evidence["terminal_web_process"] = {
        "pid": web_process.pid,
        "return_code": web_process.poll(),
    }
    _retain_evidence(evidence)
