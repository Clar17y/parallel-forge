"""Opt-in recovery proof across Chromium, the public API, and disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.evaluations.credentials import assert_credential_free
from forge.persistence.models.auth import OperatorSession
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_recovery import (
    SubscriptionContractRevision,
    SubscriptionRecoveryReceipt,
    SubscriptionRecoveryWorker,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, select, update

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
        reason="requires explicit local Chromium and disposable PostgreSQL acceptance setup",
    ),
]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def _wait_web(process: Any, origin: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert process.poll() is None, "Next.js exited before recovery browser proof"
        try:
            if httpx.get(origin, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("Next.js did not become ready for recovery browser proof")


def _case_id(case: Any, name: str) -> Any:
    return case[name] if isinstance(case, dict) else getattr(case, name)


@pytest.mark.integration
async def test_primary_recovery_survives_lost_response_stale_tab_and_api_restart(
    test_database_url: str,
    session_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path.cwd()
    web_root = root / "apps" / "web"
    evidence_root = root / ".llm-output" / "recovery-browser"
    evidence_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FORGE_ACCEPTANCE_OUTPUT_ROOT", str(evidence_root))
    monkeypatch.syspath_prepend(str(root / "apps" / "orchestrator" / "tests" / "persistence"))
    from test_subscription_recovery_service import recovery_case

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

    case = await recovery_case(session_factory, tmp_path, defer_settlement=True)
    run_id = _case_id(case, "run_id")
    task_id = _case_id(case, "task_id")
    attempt_id = _case_id(case, "attempt_id")
    renewal_root = tmp_path / "session-renewal"
    renewal_root.mkdir()
    renewal_case = await recovery_case(session_factory, renewal_root, defer_settlement=True)
    renewal_run_id = _case_id(renewal_case, "run_id")
    renewal_task_id = _case_id(renewal_case, "task_id")
    renewal_attempt_id = _case_id(renewal_case, "attempt_id")

    async def advertise_fixture_worker() -> None:
        # Keep only the fixture's simulated compatibility heartbeat current.
        async with session_factory() as session, session.begin():
            await session.execute(
                update(SubscriptionRecoveryWorker)
                .where(SubscriptionRecoveryWorker.worker_id.like("fixture-%"))
                .values(observed_at=datetime.now(UTC))
            )

    async def expire_operator_session() -> str:
        async with session_factory() as session, session.begin():
            actors = (
                await session.scalars(
                    select(OperatorSession.actor_id).where(
                        OperatorSession.credential_kind == "session",
                        OperatorSession.revoked_at.is_(None),
                    )
                )
            ).all()
            assert len(actors) == 1 and actors[0] is not None
            await session.execute(
                update(OperatorSession)
                .where(OperatorSession.credential_kind == "session", OperatorSession.actor_id == actors[0])
                .values(idle_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            return str(actors[0])

    async def snapshot(run: Any, task: Any, attempt: Any) -> dict[str, object]:
        async with session_factory() as session:
            result = await session.get(SubscriptionAttemptResult, attempt)
            assert result is not None, "fixture must retain its original provider result"
            receipts = int(
                await session.scalar(
                    select(func.count()).select_from(SubscriptionRecoveryReceipt).where(
                        SubscriptionRecoveryReceipt.run_id == run,
                        SubscriptionRecoveryReceipt.task_id == task,
                    )
                )
                or 0
            )
            revisions = int(
                await session.scalar(
                    select(func.count()).select_from(SubscriptionContractRevision).where(
                        SubscriptionContractRevision.run_id == run,
                        SubscriptionContractRevision.task_id == task,
                    )
                )
                or 0
            )
            debits = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SubscriptionRepairDebit)
                    .join(SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionRepairDebit.attempt_id)
                    .where(SubscriptionAttempt.run_id == run, SubscriptionAttempt.task_row_id == task)
                )
                or 0
            )
            attempts = int(
                await session.scalar(
                    select(func.count()).select_from(SubscriptionAttempt).where(
                        SubscriptionAttempt.run_id == run,
                        SubscriptionAttempt.task_row_id == task,
                    )
                )
                or 0
            )
            result_digest = result.result_digest
            result_payload = result.result_payload
        async with PostgresUnitOfWork(session_factory) as work:
            usage = await work.subscription_budget.usage(run, task)
        return {
            "receipt_count": receipts,
            "contract_revision_count": revisions,
            "repair_debits": debits,
            "attempt_count": attempts,
            "provider_attempts_consumed": usage.consumed.provider_attempts,
            "result_digest": result_digest,
            "result_payload": result_payload,
        }

    web_origin = f"http://127.0.0.1:{free_loopback_port()}"
    data_root = tmp_path / "public-data"
    data_root.mkdir()
    harness = ForgeProcessHarness(
        database_url=test_database_url,
        data_root=data_root,
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
            return future.result(timeout=30)
        except BaseException:
            future.cancel()
            raise

    main_error: BaseException | None = None
    try:
        await asyncio.to_thread(harness.start_api)

        class Control(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/configuration":
                    self.reply({
                        "bootstrap": harness.bootstrap_token(),
                        "run_id": str(run_id),
                        "task_id": str(task_id),
                        "attempt_id": str(attempt_id),
                        "renewal_run_id": str(renewal_run_id),
                        "renewal_task_id": str(renewal_task_id),
                        "renewal_attempt_id": str(renewal_attempt_id),
                    })
                elif self.path == "/snapshot":
                    self.reply(on_loop(snapshot(run_id, task_id, attempt_id)))
                elif self.path == "/snapshot-renewal":
                    self.reply(on_loop(snapshot(renewal_run_id, renewal_task_id, renewal_attempt_id)))
                else:
                    self.reply({}, status=404)

            def do_POST(self) -> None:
                if self.path == "/api/restart":
                    self.reply({"pid": harness.restart_api()})
                elif self.path == "/diagnostic/settle":
                    settlement = on_loop(_case_id(case, "settle")())
                    self.reply({"disposition": settlement.disposition})
                elif self.path == "/diagnostic/settle-renewal":
                    settlement = on_loop(_case_id(renewal_case, "settle")())
                    self.reply({"disposition": settlement.disposition})
                elif self.path == "/auth/expire":
                    self.reply({"actor_id": on_loop(expire_operator_session())})
                elif self.path == "/auth/bootstrap":
                    self.reply({"bootstrap": harness.bootstrap_token()})
                elif self.path == "/worker/heartbeat":
                    on_loop(advertise_fixture_worker())
                    self.reply({"ok": True})
                else:
                    self.reply({}, status=404)

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
            if key.upper() in {
                "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME",
                "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            }
        } | {
            "FORGE_WEB_ORIGIN": web_origin,
            "FORGE_API_INTERNAL_ORIGIN": harness.base_url,
            "FORGE_E2E_WEB_ORIGIN": web_origin,
            "FORGE_E2E_CONTROL_ORIGIN": f"http://127.0.0.1:{server.server_port}",
            "FORGE_E2E_EVIDENCE_ROOT": str(evidence_root),
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        web_process = runner.spawn(
            "workflow-recovery-browser-web",
            [node, next_cli.stdout.strip(), "dev", "--hostname", "127.0.0.1", "--port", web_origin.rsplit(":", 1)[1]],
            cwd=web_root,
            env=environment,
        )
        await asyncio.to_thread(_wait_web, web_process, web_origin)
        result = await asyncio.to_thread(
            runner.run,
            [node, str(root / "tests" / "acceptance" / "workflow_recovery_browser.mjs")],
            cwd=web_root,
            env=environment,
            timeout=240,
        )
        assert_credential_free(result.stdout + result.stderr)
        (evidence_root / "browser.stdout.json").write_text(result.stdout, encoding="utf-8")
        (evidence_root / "browser.stderr.txt").write_text(result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stderr[-6000:]
        evidence = json.loads(result.stdout)
        assert evidence["provider_calls"] is False
        assert evidence["renewal_old_actor"] != evidence["renewal_new_actor"]
        assert evidence["renewal_post_count"] == 1
        durable = await snapshot(run_id, task_id, attempt_id)
        assert evidence["after"] == {
            key: value for key, value in durable.items() if key != "result_payload"
        }
        renewal_durable = await snapshot(renewal_run_id, renewal_task_id, renewal_attempt_id)
        assert evidence["renewal_after"] == {
            key: value for key, value in renewal_durable.items() if key != "result_payload"
        }
    except BaseException as error:
        main_error = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
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
                expected = next_types_before.replace(b"\r\n", b"\n").replace(
                    b'"./.next/types/', b'"./.next/dev/types/'
                )
                assert current_types.replace(b"\r\n", b"\n") == expected
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
        api_log = data_root / "api.log"
        if api_log.exists():
            log_text = api_log.read_text(encoding="utf-8")
            assert_credential_free(log_text)
            (evidence_root / "api.log").write_text(log_text, encoding="utf-8")
        if cleanup_errors:
            if main_error is not None:
                for err in cleanup_errors:
                    main_error.add_note(f"Cleanup failure: {type(err).__name__}: {err}")
            else:
                raise ExceptionGroup("Recovery browser cleanup failures", cleanup_errors)

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
