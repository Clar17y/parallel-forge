"""Crash observation must allow owner expiry while remaining bounded and exact."""

from types import SimpleNamespace

import pytest

from tests.recovery_process import harness as harness_module
from tests.recovery_process.harness import RecoveryProcessHarness
from tests.recovery_process.worker_crash_hook import CRASH_EXIT_CODE


def _clocked_harness(monkeypatch, tmp_path, *, exit_after, exit_code):
    clock = [0.0]

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(harness_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(harness_module, "asyncio", SimpleNamespace(sleep=sleep))
    harness = object.__new__(RecoveryProcessHarness)
    harness._data_root = tmp_path
    harness._last_crash_point = "file_write"
    harness._worker_process = SimpleNamespace(
        poll=lambda: exit_code if clock[0] >= exit_after else None
    )
    return harness


async def test_crash_wait_allows_owner_expiry_and_worker_startup(monkeypatch, tmp_path):
    # Worker startup can wait for the previous owner's 30-second lease, then
    # still needs time to prepare and execute the instrumented operation.
    harness = _clocked_harness(
        monkeypatch, tmp_path, exit_after=31, exit_code=CRASH_EXIT_CODE
    )
    assert await harness.wait_for_worker_crash() == CRASH_EXIT_CODE


async def test_crash_wait_rejects_wrong_exit_after_recovery(monkeypatch, tmp_path):
    harness = _clocked_harness(monkeypatch, tmp_path, exit_after=31, exit_code=1)
    with pytest.raises(AssertionError, match="unexpected return code 1"):
        await harness.wait_for_worker_crash()


async def test_crash_wait_still_fails_when_worker_never_reaches_boundary(monkeypatch, tmp_path):
    harness = _clocked_harness(monkeypatch, tmp_path, exit_after=1000, exit_code=CRASH_EXIT_CODE)
    (tmp_path / "worker.log").write_text(
        "worker waiting; postgresql://operator:pw@localhost/fixture", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="within 0.1s") as error:
        await harness.wait_for_worker_crash(timeout=0.1)
    assert "worker waiting" in str(error.value)
    assert ":pw@" not in str(error.value)
    assert "[REDACTED]" in str(error.value)
