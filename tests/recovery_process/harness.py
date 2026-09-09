"""Process harness for crash testing and recovery acceptance."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from forge.observability.redaction import Redactor
from forge.persistence.models import RunCommand
from sqlalchemy import update

from tests.acceptance.process_harness import ForgeProcessHarness
from tests.recovery_process.worker_crash_hook import CRASH_EXIT_CODE


class RecoveryProcessHarness(ForgeProcessHarness):
    """Subclass of ForgeProcessHarness providing instrumented worker crash control."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._last_crash_point: str | None = None

    def start_worker(self, *, crash_point: str | None = None) -> None:
        """Launch worker child process with an optional explicit crash point."""
        self._last_crash_point = crash_point
        worker_log = open(self._data_root / "worker.log", "a", encoding="utf-8")  # noqa: SIM115 - child owns handle
        self._log_handles.append(worker_log)

        env = dict(self._env)
        if crash_point:
            env["FORGE_TEST_CRASH_POINT"] = crash_point
            if crash_point == "file_write":
                env["FORGE_TEST_CRASH_AFTER_FILE_WRITE"] = "1"
        else:
            env.pop("FORGE_TEST_CRASH_POINT", None)
            env.pop("FORGE_TEST_CRASH_AFTER_FILE_WRITE", None)

        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "tests.recovery_process.worker_process"],
            cwd=Path.cwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._processes.append(process)
        self._worker_process = process

        time.sleep(0.3)
        if crash_point is None and process.poll() is not None:
            output = (self._data_root / "worker.log").read_text(encoding="utf-8")
            raise AssertionError(
                f"worker process exited early ({process.returncode}): {output[-2000:]}"
            )

    async def wait_for_worker_crash(self, *, expected_code: int = CRASH_EXIT_CODE, timeout: float = 90) -> int:
        """Allow owner expiry and startup before observing the exact crash exit.

        Production command/owner leases last 30 seconds. A restarted worker may
        need that entire interval before it can prepare and execute the fixture.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._worker_process is not None:
                code = self._worker_process.poll()
                if code is not None:
                    assert code == expected_code, (
                        f"worker terminated with unexpected return code {code}, expected {expected_code}"
                    )
                    return code
            await asyncio.sleep(0.05)
        worker_log = self._data_root / "worker.log"
        worker_tail = ""
        if worker_log.exists():
            worker_tail = str(Redactor().redact(worker_log.read_text(encoding="utf-8")[-2500:]))
        raise AssertionError(
            f"Worker did not terminate at crash point '{self._last_crash_point}' within {timeout}s; "
            f"worker_log={worker_tail!r}"
        )

    def restart_worker(self, *, crash_point: str | None = None) -> int:
        """Restart the worker asserting a distinct child process identity."""
        old_pid = self.worker_pid
        self.stop_worker()
        self.start_worker(crash_point=crash_point)
        new_pid = self.worker_pid
        assert new_pid != old_pid, f"Restarted worker PID {new_pid} equals old PID {old_pid}"
        assert (
            new_pid != self.api_pid
        ), f"Restarted worker PID {new_pid} equals API PID {self.api_pid}"
        return new_pid

    async def expedite_commands(self, factory, run_id: UUID) -> None:
        """Expire existing leases and make queued commands immediately claimable."""
        past = datetime.now(UTC) - timedelta(seconds=10)
        async with factory() as session, session.begin():
            await session.execute(
                update(RunCommand)
                .where(RunCommand.run_id == run_id, RunCommand.status == "LEASED")
                .values(lease_expires_at=past)
            )
            await session.execute(
                update(RunCommand)
                .where(RunCommand.run_id == run_id, RunCommand.status == "PENDING")
                .values(available_at=past)
            )


@contextmanager
def managed_recovery_harness(**kwargs: object) -> Iterator[RecoveryProcessHarness]:
    harness = RecoveryProcessHarness(**kwargs)
    try:
        yield harness
    finally:
        harness.close()
