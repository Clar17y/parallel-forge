"""PR #142 repair probes adapted as maintained tests (no DB, no network)."""

from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.domain.direct import DirectLaunch, DirectResult, DirectRunRecord, parse_review_result
from forge.domain.policy import CommandSpec, ProjectPolicy, StepKind
from forge.settings import Settings
from forge.worker import direct, direct_process


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "probe@example.invalid")
    git(root, "config", "user.name", "Review Probe")
    (root / "tracked.txt").write_text("baseline\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "baseline")
    return root


def worker(tmp_path, **settings_kwargs):
    obj = direct.DirectRunWorker(None, Settings(data_root=tmp_path, direct_silence_timeout_seconds=1, direct_session_timeout_seconds=60, **settings_kwargs))
    obj._repo = SimpleNamespace(
        update_session=AsyncMock(), cancel_requested=AsyncMock(return_value=False),
        update_run=AsyncMock(side_effect=lambda run_id, **kw: SimpleNamespace(id=run_id)),
        create_review=AsyncMock(), create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
        sessions=AsyncMock(return_value=()),
    )
    return obj


def run_record():
    return DirectRunRecord(id=uuid4(), project_id=uuid4(), task_text="probe", client="fake", model="fake")


# --- F1 reviewer integrity -------------------------------------------------

@pytest.mark.asyncio
async def test_reviewer_commit_must_be_detected_and_restored(tmp_path):
    root = repository(tmp_path)
    before_head, before_status = await direct._snapshot_worktree(root)
    (root / "tracked.txt").write_text("reviewer commit\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "reviewer commit")
    modified, restored = await direct._revert_reviewer_changes(root, before_head, before_status)
    assert modified and restored
    assert git(root, "rev-parse", "HEAD") == before_head


@pytest.mark.asyncio
async def test_reviewer_staged_content_must_be_restored(tmp_path):
    root = repository(tmp_path)
    before_head, before = await direct._snapshot_worktree(root)
    (root / "tracked.txt").write_text("reviewer staged content\n")
    git(root, "add", "tracked.txt")
    modified, restored = await direct._revert_reviewer_changes(root, before_head, before)
    assert modified and restored
    assert (root / "tracked.txt").read_text() == "baseline\n"


@pytest.mark.asyncio
async def test_reviewer_unrecoverable_modification_reported(tmp_path):
    root = repository(tmp_path)
    (root / "tracked.txt").write_text("reviewer change\n")
    modified, restored = await direct._revert_reviewer_changes(root, "0" * 40, {})
    assert modified and not restored


# --- F2 commit failure ------------------------------------------------------

def _install_rejecting_hook(root: Path) -> None:
    git_dir = Path(git(root, "rev-parse", "--git-common-dir"))
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    hook = git_dir / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", newline="\n")
    hook.chmod(0o755)


@pytest.mark.asyncio
async def test_commit_hook_failure_must_stop_candidate(tmp_path):
    root = repository(tmp_path)
    head = git(root, "rev-parse", "HEAD")
    _install_rejecting_hook(root)
    (root / "tracked.txt").write_text("candidate\n")
    with pytest.raises(Exception, match="commit"):
        await direct._commit_worktree(root, head, "candidate", uuid4())
    assert git(root, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_commit_clean_and_agent_committed_trees_are_not_errors(tmp_path):
    root = repository(tmp_path)
    head = git(root, "rev-parse", "HEAD")
    assert await direct._commit_worktree(root, head, "candidate", uuid4()) == head
    (root / "tracked.txt").write_text("agent commit\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "agent commit")
    agent_head = git(root, "rev-parse", "HEAD")
    assert agent_head != head
    assert await direct._commit_worktree(root, head, "candidate", uuid4()) == agent_head


@pytest.mark.asyncio
async def test_commit_failure_settles_failed_before_checks_on_implement(tmp_path, monkeypatch):
    root = repository(tmp_path)
    _install_rejecting_hook(root)
    obj = worker(tmp_path)
    run = run_record()
    obj._repo.update_run = AsyncMock(side_effect=lambda run_id, **kw: run)
    obj._repo.project_for = AsyncMock(return_value=(str(root), "main"))
    monkeypatch.setattr(obj, "_resolve_installation", lambda run: object())
    monkeypatch.setattr(obj, "_resolve_review_installation", lambda run, installation: object())

    async def fake_session(run_arg, installation, *, kind, prompt, worktree, transcript_dir, task_id=None):
        (worktree / "changed.txt").write_text("changed\n")
        return DirectResult(status="done", summary="done")

    monkeypatch.setattr(obj, "_launch_and_settle_session", fake_session)
    checks = AsyncMock()
    monkeypatch.setattr(obj, "_run_checks", checks)
    publish = AsyncMock()
    monkeypatch.setattr(obj, "_publish_and_watch", publish)
    await obj._execute(run)
    # _execute creates a worktree from the canonical repo; the shared hook
    # rejects the candidate commit there as well.
    failed = [c for c in obj._repo.update_run.await_args_list if c.kwargs.get("state") == "FAILED"]
    assert failed and "commit" in (failed[-1].kwargs.get("failure_reason") or "")
    assert checks.await_count == 0 and publish.await_count == 0


@pytest.mark.asyncio
async def test_commit_failure_settles_failed_on_repair_step(tmp_path):
    root = repository(tmp_path)
    _install_rejecting_hook(root)
    (root / "tracked.txt").write_text("candidate\n")
    obj = worker(tmp_path)
    run = run_record()
    obj._launch_and_settle_session = AsyncMock(return_value=DirectResult(status="done", summary="repaired"))
    head = git(root, "rev-parse", "HEAD")
    repaired = await obj._execute_repair_step(
        run, object(), prompt="repair", worktree=root, transcript_dir=tmp_path, head_sha=head, summary="s"
    )
    assert repaired is None
    failed = [c for c in obj._repo.update_run.await_args_list if c.kwargs.get("state") == "FAILED"]
    assert failed and "commit" in (failed[-1].kwargs.get("failure_reason") or "")


# --- F3/F6/F8 session streaming, bounds, settlement -------------------------

@pytest.mark.asyncio
async def test_watchdog_still_applies_after_output_eof(tmp_path):
    script = tmp_path / "close_streams.py"
    script.write_text("import os,time\nos.close(1)\nos.close(2)\ntime.sleep(2.2)\n")
    obj = worker(tmp_path)
    run = run_record()
    started = monotonic()
    outcome = await asyncio.wait_for(obj._run_session(run, uuid4(), DirectLaunch(argv=(sys.executable, str(script))), tmp_path, tmp_path / "session.log"), timeout=10)
    elapsed = monotonic() - started
    assert outcome[0] == "timeout", f"one-second watchdog returned {outcome[0]!r} only after {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_large_jsonl_line_does_not_crash_session_runner(tmp_path):
    script = tmp_path / "large_line.py"
    script.write_text("import json\nprint(json.dumps({'type':'tool.output','text':'x'*70000}),flush=True)\n")
    obj = worker(tmp_path)
    run = run_record()
    outcome = await asyncio.wait_for(obj._run_session(run, uuid4(), DirectLaunch(argv=(sys.executable, str(script))), tmp_path, tmp_path / "session.log"), timeout=10)
    assert outcome[0] == "exited" and outcome[1] == 0


@pytest.mark.asyncio
async def test_multi_mib_single_line_session_survives_and_tail_retained(tmp_path):
    script = tmp_path / "huge_line.py"
    script.write_text(
        "print('x'*3000000,flush=True)\n"
        "print('```json',flush=True)\n"
        "print('{\"status\":\"done\",\"summary\":\"ok\",\"question\":null,\"options\":[]}',flush=True)\n"
        "print('```',flush=True)\n"
    )
    obj = worker(tmp_path)
    run = run_record()
    outcome = await asyncio.wait_for(obj._run_session(run, uuid4(), DirectLaunch(argv=(sys.executable, str(script))), tmp_path, tmp_path / "session.log"), timeout=20)
    assert outcome[0] == "exited" and outcome[1] == 0
    assert '"status": "done"' in outcome[2] or '"status":"done"' in outcome[2] or "ok" in outcome[2]
    assert len(outcome[2].encode()) <= 300_000


@pytest.mark.asyncio
async def test_transcript_cap_truncates_but_session_completes(tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text("for i in range(5000): print(f'line-{i}-'+'y'*40,flush=True)\n")
    obj = worker(tmp_path, direct_max_transcript_bytes=4096)
    run = run_record()
    transcript = tmp_path / "session.log"
    outcome = await asyncio.wait_for(obj._run_session(run, uuid4(), DirectLaunch(argv=(sys.executable, str(script))), tmp_path, transcript), timeout=20)
    assert outcome[0] == "exited" and outcome[1] == 0
    data = transcript.read_bytes()
    assert len(data) <= 8192
    assert b"truncated" in data.lower()


@pytest.mark.asyncio
async def test_worker_task_cancellation_settles_child_process(tmp_path, monkeypatch):
    script = tmp_path / "sleep.py"
    script.write_text("import time\nprint('started',flush=True)\ntime.sleep(30)\n")
    obj = worker(tmp_path)
    run = run_record()
    original_spawn = asyncio.create_subprocess_exec
    spawned = []

    async def capture_spawn(*args, **kwargs):
        child = await original_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    task = asyncio.create_task(obj._run_session(run, uuid4(), DirectLaunch(argv=(sys.executable, str(script))), tmp_path, tmp_path / "session.log"))
    while not spawned or not obj._repo.update_session.await_count:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        child = spawned[0]
        survived = child.returncode is None and direct._pid_alive(child.pid)
    finally:
        if spawned:
            await direct._kill_tree(spawned[0])
            await spawned[0].wait()
    assert not survived, "worker task cancellation left the CLI child running without a watchdog"


def _orphan_worker(tmp_path, pid):
    obj = worker(tmp_path)
    run = run_record()
    session = SimpleNamespace(id=uuid4(), state="RUNNING", pid=pid)
    obj._repo.running_runs = AsyncMock(return_value=(run,))
    obj._repo.sessions = AsyncMock(return_value=(session,))
    obj._repo.settle_orphan_run = AsyncMock(return_value=None)
    return obj


def _assert_stop_submitted(obj):
    [run] = obj._repo.running_runs.return_value
    [session] = obj._repo.sessions.return_value
    obj._repo.settle_orphan_run.assert_awaited_once_with(
        run.id, stopped_sessions={session.id: session.pid}
    )


@pytest.mark.asyncio
async def test_orphan_sweep_kills_live_recorded_pid(tmp_path):
    child = subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=(os.name != "nt"),
    )
    try:
        obj = _orphan_worker(tmp_path, child.pid)
        await obj._settle_orphans()
        assert child.wait(timeout=5) != 0
        _assert_stop_submitted(obj)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


linux_recovery = pytest.mark.skipif(sys.platform != "linux", reason="Linux process groups and procfs")


async def _wait_for_unreaped_exit(pid):
    status = await asyncio.wait_for(
        asyncio.to_thread(os.waitid, os.P_PID, pid, os.WEXITED | os.WNOWAIT), timeout=5
    )
    assert status is not None and status.si_pid == pid


@linux_recovery
@pytest.mark.asyncio
async def test_orphan_sweep_recognizes_exited_unreaped_zombie_session(tmp_path, monkeypatch):
    with subprocess.Popen([sys.executable, "-I", "-S", "-c", "pass"], start_new_session=True) as child:  # noqa: ASYNC220
        await _wait_for_unreaped_exit(child.pid)
        assert not direct._pid_alive(child.pid)
        kernel_killpg = os.killpg

        def probe_only(pgid, sig):
            assert sig == 0, "a zombie-only group must not be signalled"
            kernel_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", probe_only)
        obj = _orphan_worker(tmp_path, child.pid)
        await obj._settle_orphans()
        await _wait_for_unreaped_exit(child.pid)  # Recovery observed exit without reaping.
        _assert_stop_submitted(obj)


@linux_recovery
@pytest.mark.asyncio
async def test_orphan_sweep_stops_provider_with_exited_main_thread(tmp_path):
    # pthread_exit ends the group leader's main thread while the other thread
    # keeps running. Only the survivor announces that the leader is a zombie.
    script = """
import ctypes, os, threading, time
from pathlib import Path
def survivor():
    while Path('/proc/self/stat').read_bytes().rsplit(b') ', 1)[1].split()[0] != b'Z':
        time.sleep(.01)
    os.write(1, b'R')
    time.sleep(60)
threading.Thread(target=survivor, daemon=True).start()
ctypes.CDLL(None).pthread_exit(None)
"""
    with subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-I", "-c", script], stdout=subprocess.PIPE, start_new_session=True
    ) as provider:
        try:
            assert select.select([provider.stdout], [], [], 5)[0] and provider.stdout.read(1) == b"R"
            assert direct._pid_alive(provider.pid)
            obj = _orphan_worker(tmp_path, provider.pid)
            await obj._settle_orphans()
            assert provider.wait(timeout=5) != 0
            _assert_stop_submitted(obj)
        finally:
            if provider.poll() is None:
                provider.kill()


@linux_recovery
@pytest.mark.asyncio
@pytest.mark.parametrize("leader_exits", [False, True], ids=["live-tree", "survivor-ignores-term"])
async def test_orphan_sweep_stops_group_before_recording_failure(tmp_path, leader_exits):
    read_fd, write_fd = os.pipe()
    member_code = (
        "import os, signal, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if leader_exits else "")
        + f"os.write({write_fd}, b'R')\n"
        + "time.sleep(60)\n"
    )
    leader_code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', '-c', sys.argv[1]], pass_fds=({write_fd},))\n"
        + ("" if leader_exits else "time.sleep(60)\n")
    )
    with os.fdopen(read_fd, "rb", buffering=0) as witness, os.fdopen(write_fd, "wb") as writer:
        leader = subprocess.Popen(  # noqa: ASYNC220
            [sys.executable, "-I", "-c", leader_code, member_code],
            pass_fds=(write_fd,), start_new_session=True,
        )
        writer.close()
        try:
            assert select.select([witness], [], [], 5)[0] and witness.read(1) == b"R"
            if leader_exits:
                # The member is ready and the leader is dead but deliberately unreaped.
                await _wait_for_unreaped_exit(leader.pid)

            def require_stopped(run_id, *, stopped_sessions):
                # Both processes inherit the write end. EOF is independent of Forge's probe.
                assert select.select([witness], [], [], 0)[0] and witness.read(1) == b""
                assert list(stopped_sessions.values()) == [leader.pid]

            obj = _orphan_worker(tmp_path, leader.pid)
            obj._repo.settle_orphan_run.side_effect = require_stopped
            await obj._settle_orphans()
            _assert_stop_submitted(obj)
        finally:
            # Keep the leader unreaped until the group's final cleanup, pinning its PID.
            with suppress(ProcessLookupError):
                os.killpg(leader.pid, signal.SIGKILL)
            leader.wait(timeout=5)


@linux_recovery
@pytest.mark.asyncio
@pytest.mark.parametrize("group", [-1, 0], ids=["ordinary-child", "foreign-session"])
async def test_orphan_sweep_does_not_signal_unrelated_process(tmp_path, monkeypatch, group):
    kernel_killpg = os.killpg

    def probe_only(pgid, sig):
        assert sig == 0
        kernel_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", probe_only)
    child = subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"], process_group=group,
    )
    try:
        obj = _orphan_worker(tmp_path, child.pid)
        await obj._settle_orphans()
        assert child.poll() is None
        if group == 0:
            assert obj._repo.settle_orphan_run.await_args.kwargs["stopped_sessions"] == {}
    finally:
        child.kill()
        child.wait(timeout=5)


@linux_recovery
@pytest.mark.asyncio
async def test_orphan_sweep_refuses_own_process_group(monkeypatch):
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("probed the worker's own group"))
    assert await direct._kill_pid_tree(os.getpid())  # Worker PID reuse proves the prior group ended.
    for number in {os.getpgrp(), os.getsid(0)} - {os.getpid()}:
        assert not await direct._kill_pid_tree(number)


def _fixture_stat(pid, state, group, session):
    # /proc/PID/stat fields 3..20, including the thread count.
    fields = [state, 1, group, session, *([0] * 13), 1]
    return str(pid).encode() + b" (weird ) name\n\xff) " + " ".join(map(str, fields)).encode()


@linux_recovery
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member", "kernel_group", "stopped"),
    [
        ("unreadable", 42001, False), ("unreadable", 99999, True),
        ("malformed", 42001, False), ("foreign-session", 42001, False),
        ("unlisted", 42001, False), ("kernel-gone", 42001, True),
        ("hidden-members", 99999, False), ("other-pid-namespace", 99999, False),
    ],
)
async def test_orphan_group_requires_observable_session_members(
    tmp_path, monkeypatch, member, kernel_group, stopped,
):

    def probe_only(pgid, sig):
        assert pgid == 42001 and sig == 0
        if member == "kernel-gone":
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", probe_only)
    monkeypatch.setattr(os, "getpgid", lambda pid: kernel_group)
    own_pid = os.getpid()
    own_dir = tmp_path / str(own_pid)
    own_dir.mkdir()
    (tmp_path / "self").symlink_to(own_dir.name)
    (own_dir / "stat").write_bytes(_fixture_stat(own_pid, "S", 99999, 99999))
    namespace_pids = f"{own_pid}\t{own_pid}" if member == "other-pid-namespace" else str(own_pid)
    (own_dir / "status").write_bytes(f"NSpid:\t{namespace_pids}\n".encode())
    flags = "rw,hidepid=invisible" if member == "hidden-members" else "rw"
    (own_dir / "mounts").write_bytes(f"proc /proc proc {flags} 0 0\n".encode())
    if member not in ("unlisted", "kernel-gone"):
        leader = tmp_path / "42001"
        leader.mkdir()
        state = "S" if member == "other-pid-namespace" else "Z"
        (leader / "stat").write_bytes(_fixture_stat(42001, state, 42001, 42001))
        other = tmp_path / "42002"
        other.mkdir()
        if member == "unreadable":
            original = Path.read_bytes

            def read(path):
                if path.parent == other:
                    raise PermissionError
                return original(path)

            monkeypatch.setattr(Path, "read_bytes", read)
        elif member == "malformed":
            (other / "stat").write_bytes(b"42002 (bad) S\n")
        elif member == "foreign-session":
            (other / "stat").write_bytes(_fixture_stat(42002, "S", 42001, 41000))
    assert await direct_process.stop_linux_session_group(42001, proc_root=tmp_path) is stopped


@linux_recovery
@pytest.mark.asyncio
@pytest.mark.parametrize("new_session", [42001, 99999], ids=["related-fork", "unrelated-fork"])
async def test_orphan_sweep_rechecks_new_pids_before_settling(tmp_path, monkeypatch, new_session):
    for pid, state in ((42001, "Z"), (42002, "S")):
        entry = tmp_path / str(pid)
        entry.mkdir()
        (entry / "stat").write_bytes(_fixture_stat(pid, state, 42001, 42001))
    newcomer = tmp_path / "42003"
    original = Path.read_bytes

    def read(path):
        if path.parent.name == "42002":
            if not newcomer.exists():
                newcomer.mkdir()
                (newcomer / "stat").write_bytes(_fixture_stat(42003, "S", new_session, new_session))
            raise ProcessLookupError
        return original(path)

    sent = []
    def signal_group(pgid, sig):
        assert pgid == 42001
        if sig:
            sent.append(sig)
            (newcomer / "stat").write_bytes(_fixture_stat(42003, "Z", pgid, pgid))

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(direct_process, "_same_pid_namespace", lambda root: True)
    monkeypatch.setattr(direct_process, "_proc_lists_all_pids", lambda root: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 99999)
    monkeypatch.setattr(os, "killpg", signal_group)
    expected = direct_process._State.LIVE if new_session == 42001 else direct_process._State.STOPPED
    assert direct_process._group_state(42001, tmp_path) is expected
    assert await direct_process.stop_linux_session_group(42001, proc_root=tmp_path)
    assert sent == ([signal.SIGTERM] if new_session == 42001 else [])


@linux_recovery
@pytest.mark.asyncio
async def test_orphan_sweep_retries_uncertain_observation_after_signal(monkeypatch):
    states = direct_process._State
    observations = iter((states.LIVE, states.UNCONFIRMED, states.LIVE, states.STOPPED))
    monkeypatch.setattr(direct_process, "_group_state", lambda *_: next(observations))
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append(sig) if sig else None)
    assert await direct_process.stop_linux_session_group(42001)
    assert sent == [signal.SIGTERM]


@pytest.mark.asyncio
async def test_orphan_group_invalid_pids():
    for pid in (0, -1):
        assert await direct._kill_pid_tree(pid)
        assert not direct._pid_alive(pid)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill")
@pytest.mark.asyncio
async def test_orphan_group_windows_best_effort(monkeypatch):
    monkeypatch.setattr(direct, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(direct.asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError()))
    assert await direct._kill_pid_tree(12345)


# --- check execution: F6/F8/F3/F7 -------------------------------------------

def _command(argv, env_keys=()):
    return CommandSpec(kind=StepKind.TEST, name="smoke", argv=tuple(argv), timeout_seconds=60, environment_keys=tuple(env_keys))


def _policy(tmp_path, command):
    return ProjectPolicy(id=uuid4(), version=1, repository_path=str(tmp_path), github_repository="o/r", default_branch="main", commands=(command,))


@pytest.mark.asyncio
async def test_large_and_huge_check_lines_survive_with_truthful_verdict(tmp_path):
    script = tmp_path / "noisy_check.py"
    script.write_text(
        "import sys\nprint('x'*3000000,flush=True)\nprint('FAILED tests/test_smoke.py::test_it',flush=True)\nsys.exit(1)\n"
    )
    obj = worker(tmp_path)
    log_path = tmp_path / "checks.log"
    result = await obj._execute_check(run_record(), _command((sys.executable, str(script))), tmp_path, dict(os.environ), log_path)
    assert result is not None
    status, exit_code, _ms, stdout_text, _stderr, timed_out = result
    assert status == "FAILED" and exit_code == 1 and not timed_out
    assert "FAILED tests/test_smoke.py::test_it" in stdout_text
    assert len(stdout_text.encode()) <= 300_000


@pytest.mark.asyncio
async def test_check_output_cap_truncates_file_but_verdict_truthful(tmp_path):
    script = tmp_path / "cap_check.py"
    script.write_text(
        "import sys\nfor i in range(5000): print(f'line-{i}-'+'y'*40,flush=True)\nprint('FAILED tests/test_smoke.py::test_it',flush=True)\nsys.exit(1)\n"
    )
    obj = worker(tmp_path, direct_max_check_output_bytes=4096)
    log_path = tmp_path / "checks.log"
    result = await obj._execute_check(run_record(), _command((sys.executable, str(script))), tmp_path, dict(os.environ), log_path)
    assert result is not None
    status, exit_code, _ms, stdout_text, _stderr, _timed = result
    assert status == "FAILED" and exit_code == 1
    assert "FAILED tests/test_smoke.py::test_it" in stdout_text
    data = log_path.read_bytes()
    assert len(data) <= 8192
    assert b"truncated" in data.lower()


@pytest.mark.asyncio
async def test_check_task_cancellation_kills_child(tmp_path, monkeypatch):
    script = tmp_path / "sleep_check.py"
    script.write_text("import time\nprint('started',flush=True)\ntime.sleep(30)\n")
    obj = worker(tmp_path)
    original_spawn = asyncio.create_subprocess_exec
    spawned = []

    async def capture_spawn(*args, **kwargs):
        child = await original_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    task = asyncio.create_task(obj._execute_check(run_record(), _command((sys.executable, str(script))), tmp_path, dict(os.environ), tmp_path / "checks.log"))
    while not spawned:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        child = spawned[0]
        survived = child.returncode is None and direct._pid_alive(child.pid)
    finally:
        if spawned:
            await direct._kill_tree(spawned[0])
            await spawned[0].wait()
    assert not survived


def test_checks_receive_only_declared_environment_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_REVIEW_ONLY_SENTINEL", "harmless-probe-value")
    monkeypatch.setenv("FORGE_DECLARED_KEY", "declared-value")
    command = _command((sys.executable, "check.py"), env_keys=("FORGE_DECLARED_KEY",))
    env, missing = direct._check_env(command, _policy(tmp_path, command), tmp_path)
    assert missing is None
    assert "FORGE_REVIEW_ONLY_SENTINEL" not in env
    assert env.get("FORGE_DECLARED_KEY") == "declared-value"
    # Base platform keys survive (PATH at minimum on every supported host).
    assert "PATH" in env or "Path" in env


def test_checks_missing_declared_key_still_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_DECLARED_MISSING_XYZ", raising=False)
    command = _command((sys.executable, "check.py"), env_keys=("FORGE_DECLARED_MISSING_XYZ",))
    _env, missing = direct._check_env(command, _policy(tmp_path, command), tmp_path)
    assert missing == "FORGE_DECLARED_MISSING_XYZ"


def test_output_cap_settings_defaults():
    settings = Settings()
    assert settings.direct_max_check_output_bytes == 8388608
    assert settings.direct_max_transcript_bytes == 33554432


def test_pr_author_trust_rules():
    is_trusted = direct._is_trusted_pr_author
    assert is_trusted("o", "o/r", "chatgpt-codex-connector")
    assert is_trusted("O", "o/r", "chatgpt-codex-connector")
    assert is_trusted("ChatGPT-Codex-Connector", "o/r", "chatgpt-codex-connector")
    assert is_trusted("alice", "o/r", "bob, Alice")
    assert is_trusted("dependabot[bot]", "o/r", "")
    assert not is_trusted("stranger", "o/r", "chatgpt-codex-connector")
    assert not is_trusted("", "o/r", "chatgpt-codex-connector")
    assert not is_trusted("o", "", "chatgpt-codex-connector")


def test_new_owner_settings_defaults():
    settings = Settings()
    assert settings.direct_trusted_pr_authors == "chatgpt-codex-connector"
    assert settings.direct_review_policy == "cross_provider"
    assert settings.direct_max_sessions_per_run == 20
    assert settings.direct_max_total_session_seconds == 14400


# --- session budget caps ------------------------------------------------------

def _ended_session(seconds, *, naive=False):
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 1, 1, tzinfo=None if naive else UTC)
    return SimpleNamespace(started_at=start, ended_at=start + timedelta(seconds=seconds))


@pytest.mark.asyncio
async def test_session_count_budget_refuses_launch_and_settles_failed(tmp_path):
    obj = worker(tmp_path, direct_max_sessions_per_run=1)
    obj._repo.sessions = AsyncMock(return_value=(_ended_session(1),))
    run = run_record()
    result = await obj._launch_and_settle_session(
        run, object(), kind="implement", prompt="p", worktree=tmp_path, transcript_dir=tmp_path
    )
    assert result is None
    assert obj._repo.create_session.await_count == 0
    failed = [c for c in obj._repo.update_run.await_args_list if c.kwargs.get("state") == "FAILED"]
    assert len(failed) == 1
    assert failed[0].kwargs.get("failure_reason") == "direct run session budget exhausted"


@pytest.mark.asyncio
async def test_session_duration_budget_refuses_review_launch(tmp_path):
    root = repository(tmp_path)
    obj = worker(tmp_path, direct_max_total_session_seconds=10)
    obj._repo.sessions = AsyncMock(return_value=(_ended_session(100),))
    run = run_record()
    result = await obj._launch_and_settle_review(
        run, object(), prompt="review", worktree=root, transcript_dir=tmp_path, round_no=0
    )
    assert result is None
    assert obj._repo.create_session.await_count == 0
    assert obj._repo.create_review.await_count == 0
    failed = [c for c in obj._repo.update_run.await_args_list if c.kwargs.get("state") == "FAILED"]
    assert len(failed) == 1
    assert failed[0].kwargs.get("failure_reason") == "direct run session budget exhausted"


@pytest.mark.asyncio
async def test_session_duration_ignores_missing_and_mixed_timestamps(tmp_path):
    from datetime import UTC, datetime, timedelta

    aware_start = datetime(2026, 1, 1, tzinfo=UTC)
    mixed = SimpleNamespace(started_at=aware_start.replace(tzinfo=None), ended_at=aware_start + timedelta(seconds=100))
    obj = worker(tmp_path, direct_max_sessions_per_run=20, direct_max_total_session_seconds=10)
    obj._repo.sessions = AsyncMock(return_value=(
        SimpleNamespace(started_at=None, ended_at=None),
        mixed,
        _ended_session(5),
    ))
    run = run_record()
    obj._run_session = AsyncMock(return_value=(
        "exited", 0,
        '```json\n{"status":"done","summary":"ok","question":null,"options":[]}\n```',
    ))
    result = await obj._launch_and_settle_session(
        run, SimpleNamespace(client="muse_code", executable="muse", model="fake", effort=None),
        kind="implement", prompt="p", worktree=tmp_path, transcript_dir=tmp_path,
    )
    assert result is not None and result.summary == "ok"
    assert obj._repo.create_session.await_count == 1


# --- F4 publisher ------------------------------------------------------------

@pytest.mark.asyncio
async def test_github_inline_reply_requires_numeric_rest_id(monkeypatch):
    calls = []

    async def fake_argv(argv, cwd=None):
        calls.append(argv)
        if "graphql" in argv:
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_example", "isResolved": False, "comments": {"nodes": [{"id": "PRRC_example", "databaseId": 12345, "body": "fix", "author": {"login": "reviewer"}, "path": "x.py", "createdAt": "2026-10-22T10:00:00Z"}]}}]}, "reviews": {"nodes": []}}}}})
        if "view" in argv:
            return json.dumps({"state": "OPEN", "mergeable": "MERGEABLE", "statusCheckRollup": []})
        return ""

    monkeypatch.setattr(direct, "_run_publisher_argv", fake_argv)
    publisher = direct.GhPublisher()
    snapshot = await publisher.poll(repo="owner/repo", number=42)
    assert snapshot.unresolved_threads[0].comment_id == "12345"
    assert snapshot.unresolved_threads[0].thread_id == "PRRT_example"
    await publisher.reply(repo="owner/repo", number=42, feedback=snapshot.unresolved_threads[0], body="fixed")
    endpoint = calls[-1][2]
    assert endpoint.split("/")[-2].isdigit(), f"GraphQL ID sent to numeric REST endpoint: {endpoint}"


@pytest.mark.asyncio
async def test_github_poll_without_database_id_leaves_comment_id_none(monkeypatch):
    async def fake_argv(argv, cwd=None):
        if "graphql" in argv:
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_example", "isResolved": False, "comments": {"nodes": [{"id": "PRRC_example", "body": "fix", "author": {"login": "reviewer"}, "path": "x.py", "createdAt": "2026-10-22T10:00:00Z"}]}}]}, "reviews": {"nodes": []}}}}})
        if "view" in argv:
            return json.dumps({"state": "OPEN", "mergeable": "MERGEABLE", "statusCheckRollup": []})
        return ""

    monkeypatch.setattr(direct, "_run_publisher_argv", fake_argv)
    snapshot = await direct.GhPublisher().poll(repo="owner/repo", number=42)
    assert snapshot.unresolved_threads[0].comment_id is None


# --- F9 review schema ----------------------------------------------------------

@pytest.mark.asyncio
async def test_review_uses_a_review_schema_for_muse(tmp_path, monkeypatch):
    obj = worker(tmp_path)
    observed = {}

    async def fake_session(run, session_id, launch, worktree, transcript_path):
        schema_index = launch.argv.index("--output-schema") + 1
        observed.update(json.loads(Path(launch.argv[schema_index]).read_text()))
        return "exited", 0, ""

    monkeypatch.setattr(obj, "_run_session", fake_session)
    run = run_record()
    reviewer = SimpleNamespace(client="muse_code", executable="muse", model="fake", effort=None)
    await obj._launch_raw_session(run, reviewer, kind="review", prompt="review", worktree=tmp_path, transcript_dir=tmp_path)
    assert "verdict" in observed["properties"], "review session is constrained to an implementation status schema"


# --- F5 blocking verdict --------------------------------------------------------

@pytest.mark.asyncio
async def test_blocking_verdict_without_findings_synthesizes_blocking(tmp_path):
    obj = worker(tmp_path)
    run = run_record()
    final_file = tmp_path / "missing.final"
    text = '```json\n{"verdict":"blocking","summary":"must not publish","findings":[]}\n```'
    review = await obj._settle_review(run, uuid4(), ("exited", 0, text), final_file, reviewer=SimpleNamespace(client="c", model="m"), round_no=0, modified=False)
    assert review is not None and review.verdict == "blocking"
    blocking = [f for f in review.findings if f.severity == "blocking"]
    assert len(blocking) == 1 and "must not publish" in blocking[0].detail


@pytest.mark.asyncio
async def test_blocking_verdict_with_dropped_invalid_findings_synthesizes(tmp_path):
    obj = worker(tmp_path)
    run = run_record()
    final_file = tmp_path / "missing.final"
    text = '```json\n{"verdict":"blocking","summary":"","findings":[{"severity":"critical","detail":"dropped"}]}\n```'
    review = await obj._settle_review(run, uuid4(), ("exited", 0, text), final_file, reviewer=SimpleNamespace(client="c", model="m"), round_no=0, modified=False)
    assert review is not None
    blocking = [f for f in review.findings if f.severity == "blocking"]
    assert len(blocking) == 1 and blocking[0].detail


@pytest.mark.asyncio
async def test_explicit_blocking_verdict_never_publishes(tmp_path, monkeypatch):
    root = repository(tmp_path)
    head = git(root, "rev-parse", "HEAD")
    obj = worker(tmp_path)
    run = run_record()
    obj._repo.update_run = AsyncMock(side_effect=lambda run_id, **kw: run)
    obj._repo.project_for = AsyncMock(return_value=(str(root), "main"))
    monkeypatch.setattr(obj, "_resolve_installation", lambda run: object())
    monkeypatch.setattr(obj, "_resolve_review_installation", lambda run, installation: object())
    monkeypatch.setattr(obj, "_launch_and_settle_session", AsyncMock(return_value=DirectResult(status="done", summary="done")))
    monkeypatch.setattr(obj, "_commit_and_record", AsyncMock(return_value=head))
    monkeypatch.setattr(obj, "_run_checks", AsyncMock(return_value=SimpleNamespace(outcome="passed", note=None)))
    blocking = parse_review_result('{"verdict":"blocking","summary":"must not publish","findings":[]}')
    monkeypatch.setattr(obj, "_launch_and_settle_review", AsyncMock(return_value=blocking))
    monkeypatch.setattr(obj, "_execute_repair_step", AsyncMock(return_value=None))
    publish = AsyncMock()
    monkeypatch.setattr(obj, "_publish_and_watch", publish)
    await obj._execute(run)
    assert publish.await_count == 0, "explicit blocking review entered publication"
