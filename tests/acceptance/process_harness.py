"""Small, fail-closed harness for loopback Forge process acceptance tests.

The harness deliberately starts the public API entry point in a child process.
It does not substitute ASGI transports, and its diagnostics are derived from
durable run events so a timeout remains useful after a worker has exited.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

import httpx
from forge.observability.redaction import Redactor
from forge.tools.secrets import LocalSecretStore, SecretAlreadyExistsError
from sqlalchemy import select

from tests.acceptance.fake_github_service import FakeGitHubServer, FakeGitHubState


def free_loopback_port() -> int:
    """Reserve no port; return one currently available only on loopback."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class ForgeProcessHarness:
    """Own API child lifetime and provide bounded HTTP/event observations."""

    def __init__(
        self,
        *,
        database_url: str,
        data_root: Path,
        prompt_root: Path,
        bare_remote_path: Path | None = None,
        api_port: int | None = None,
        web_origin: str | None = None,
    ) -> None:
        self.port = api_port or free_loopback_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._database_url = database_url
        self._data_root = data_root
        self._prompt_root = prompt_root
        self._bare_remote_path = bare_remote_path
        self._processes: list[subprocess.Popen[str]] = []
        self._log_handles: list[object] = []
        self._api_process: subprocess.Popen[str] | None = None
        self._worker_process: subprocess.Popen[str] | None = None

        self.fake_github_server = FakeGitHubServer(bare_remote_path=bare_remote_path)
        self.fake_github_server.start()
        self.fake_github_url = self.fake_github_server.url

        self._env = {
            **os.environ,
            "FORGE_DATABASE_URL": database_url,
            "FORGE_DATA_ROOT": str(data_root),
            "FORGE_PROMPT_ROOT": str(prompt_root),
            "FORGE_BIND_HOST": "127.0.0.1",
            "FORGE_API_PORT": str(self.port),
            "FORGE_WEB_ORIGIN": web_origin or self.base_url,
            "FORGE_RUNNER_IMAGE": "sha256:" + "1" * 64,
            "FORGE_PROVIDER_SECRET_REFERENCE": "secret://forge/acceptance-provider",
            "FORGE_ACCEPTANCE_DB_ADMIN": database_url,
            "FORGE_FAKE_GITHUB_URL": self.fake_github_url,
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(Path.cwd() / "apps" / "orchestrator" / "src")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        if bare_remote_path is not None:
            self._env["FORGE_BARE_REMOTE_PATH"] = str(bare_remote_path)

        catalog = data_root / "acceptance-pricing.json"
        catalog.write_text(
            json.dumps(
                {
                    "version": "acceptance-v1",
                    "entries": {
                        "google:gemini-2.5-pro": {
                            "input_per_million": "1",
                            "output_per_million": "1",
                            "cached_input_per_million": "1",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self._env["FORGE_PRICING_CATALOG_PATH"] = str(catalog)

        store = LocalSecretStore(data_root)
        with suppress(SecretAlreadyExistsError):
            store.create("acceptance-provider", b"test-only")

    @property
    def fake_github(self) -> FakeGitHubState:
        return self.fake_github_server.state

    @property
    def api_pid(self) -> int:
        if self._api_process is None or self._api_process.pid is None:
            raise AssertionError("API process has not been started")
        return self._api_process.pid

    @property
    def worker_pid(self) -> int:
        if self._worker_process is None or self._worker_process.pid is None:
            raise AssertionError("Worker process has not been started")
        return self._worker_process.pid

    def assert_process_identities(self) -> None:
        """Assert API, worker, and test runner execute in distinct processes."""
        current_pid = os.getpid()
        api_pid = self.api_pid
        worker_pid = self.worker_pid
        assert api_pid != current_pid, f"API PID {api_pid} equals test PID"
        assert worker_pid != current_pid, f"Worker PID {worker_pid} equals test PID"
        assert api_pid != worker_pid, f"API PID {api_pid} equals Worker PID {worker_pid}"

    def start_api(self) -> None:
        api_log = open(self._data_root / "api.log", "a", encoding="utf-8")  # noqa: SIM115 - child owns handle
        self._log_handles.append(api_log)
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", "from forge.api.main import run; run()"],
            cwd=Path.cwd(),
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=api_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._processes.append(process)
        self._api_process = process
        self.wait_ready()

    def start_worker(self) -> None:
        """Launch production ``run_worker`` with only its provider boundary scripted."""
        worker_log = open(self._data_root / "worker.log", "a", encoding="utf-8")  # noqa: SIM115 - child owns handle
        self._log_handles.append(worker_log)
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "tests.acceptance.worker_process"],
            cwd=Path.cwd(),
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._processes.append(process)
        self._worker_process = process
        time.sleep(0.3)
        if process.poll() is not None:
            output = (self._data_root / "worker.log").read_text(encoding="utf-8")
            raise AssertionError(
                f"worker process exited early ({process.returncode}): {output[-2000:]}"
            )

    def stop_worker(self) -> None:
        if self._worker_process is not None and self._worker_process.poll() is None:
            self._worker_process.terminate()
            with suppress(subprocess.TimeoutExpired):
                self._worker_process.wait(timeout=5)
            if self._worker_process.poll() is None:
                self._worker_process.kill()
                self._worker_process.wait(timeout=5)

    def restart_worker(self) -> int:
        """Restart worker process, asserting distinct child process identity."""
        old_pid = self.worker_pid
        self.stop_worker()
        self.start_worker()
        new_pid = self.worker_pid
        assert new_pid != old_pid, f"Restarted worker PID {new_pid} equals old PID {old_pid}"
        assert (
            new_pid != self.api_pid
        ), f"Restarted worker PID {new_pid} equals API PID {self.api_pid}"
        return new_pid

    def wait_ready(self, *, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            process = self._processes[-1]
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise AssertionError(
                    f"API process exited early ({process.returncode}): {output[-2000:]}"
                )
            try:
                response = httpx.get(f"{self.base_url}/api/health", timeout=0.5)
                if response.status_code == 200 and response.json() == {
                    "status": "ok",
                    "role": "api",
                }:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("API did not become ready within bounded timeout")

    def bootstrap_token(self) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "forge.cli.main", "operator", "rotate"],
            cwd=Path.cwd(),
            env=self._env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return result.stdout.strip().rsplit("#bootstrap=", 1)[1]

    async def expedite_commands(self, factory, run_id: UUID) -> None:
        """Set available_at in the past for queued commands to claim immediately."""
        from datetime import UTC, datetime, timedelta

        from forge.persistence.models import RunCommand
        from sqlalchemy import update

        async with factory() as session, session.begin():
            await session.execute(
                update(RunCommand)
                .where(RunCommand.run_id == run_id)
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )

    async def wait_for_state(
        self, factory, run_id: UUID, state: str, *, timeout: float = 60
    ) -> object:
        """Wait for a durable state and include redacted persisted events on failure."""

        from forge.persistence.models import Run, RunEvent

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with factory() as session:
                run = await session.get(Run, run_id)
                if run is not None and run.state == state:
                    return run
            await _sleep()
        async with factory() as session:
            events = list(
                await session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence.desc())
                    .limit(12)
                )
            )
        redactor = Redactor()
        evidence = [
            (event.sequence, event.event_type, redactor.redact(event.payload))
            for event in reversed(events)
        ]
        worker_tail = ""
        worker_log = self._data_root / "worker.log"
        if worker_log.exists():
            raw_tail = worker_log.read_text(encoding="utf-8")[-2500:]
            worker_tail = str(redactor.redact(raw_tail))
        raise AssertionError(
            f"run {run_id} did not reach {state}; persisted events={evidence!r}; worker_log={worker_tail!r}"
        )

    def close(self) -> None:
        for process in reversed(self._processes):
            if process.poll() is None:
                process.terminate()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for handle in self._log_handles:
            handle.close()
        self._log_handles.clear()
        self.fake_github_server.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.05)


def idempotency_key() -> str:
    return str(uuid4())


@contextmanager
def managed_harness(**kwargs: object) -> Iterator[ForgeProcessHarness]:
    harness = ForgeProcessHarness(**kwargs)
    try:
        yield harness
    finally:
        harness.close()
