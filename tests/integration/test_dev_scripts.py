from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dev import (
    CommandResult,
    DevSupervisor,
    ManagedProcess,
    PrerequisiteError,
    SupervisorError,
)


class FakeCommandRunner:
    """Deterministic command runner for testing supervisor lifecycle."""

    def __init__(
        self,
        responses: dict[str, CommandResult] | None = None,
        default_result: CommandResult | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.responses = responses or {}
        self.default_result = default_result or CommandResult(returncode=0, stdout="", stderr="")
        self.spawned: list[FakeManagedProcess] = []
        self.spawn_plans: list[FakeManagedProcess] = []

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> CommandResult:
        self.calls.append((cmd, {"cwd": cwd, "env": env, "timeout": timeout, "stop_event": stop_event}))
        cmd_str = " ".join(cmd)
        for key, res in self.responses.items():
            if key in cmd_str:
                return res
        return self.default_result

    def spawn(
        self,
        name: str,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ManagedProcess:
        self.calls.append((cmd, {"name": name, "cwd": cwd, "env": env}))
        if self.spawn_plans:
            proc = self.spawn_plans.pop(0)
            proc.name = name
            proc.cmd = cmd
            proc.env = env
            self.spawned.append(proc)
            return proc
        proc = FakeManagedProcess(name=name, cmd=cmd, env=env)
        self.spawned.append(proc)
        return proc


class FakeManagedProcess(ManagedProcess):
    def __init__(
        self,
        name: str = "proc",
        cmd: list[str] | None = None,
        env: dict[str, str] | None = None,
        exit_code: int | None = None,
    ) -> None:
        self.name = name
        self.cmd = cmd or []
        self.env = env
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.pid = 99999

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        self._exit_code = 0 if self._exit_code is None else self._exit_code

    def kill_tree(self) -> None:
        self.killed = True
        self._exit_code = -9 if self._exit_code is None else self._exit_code


def _default_prereq_responses() -> dict[str, CommandResult]:
    return {
        "uv --version": CommandResult(0, "uv 0.6.5\n", ""),
        "node --version": CommandResult(0, "v24.20.0\n", ""),
        "npm --version": CommandResult(0, "12.0.2\n", ""),
        "git --version": CommandResult(0, "git version 2.48.1\n", ""),
        "docker --version": CommandResult(0, "Docker version 28.0.0\n", ""),
        "docker info": CommandResult(0, "Client: Docker Engine\n", ""),
        "docker compose ps": CommandResult(
            0,
            '[{"Service":"postgres","State":"running","Health":"healthy"}]\n',
            "",
        ),
        "docker inspect": CommandResult(0, "healthy\n", ""),
    }


def test_verify_python_version_rejects_non_314() -> None:
    supervisor = DevSupervisor(repo_root=REPO_ROOT)
    with pytest.raises(PrerequisiteError, match="Python 3.14"):
        supervisor.verify_python_version(version_info=(3, 13, 0))
    with pytest.raises(PrerequisiteError, match="Python 3.14"):
        supervisor.verify_python_version(version_info=(3, 15, 0))
    supervisor.verify_python_version(version_info=(3, 14, 2))


def test_verify_node_version_rejects_non_24() -> None:
    runner = FakeCommandRunner(
        responses={"node --version": CommandResult(0, "v22.10.0\n", "")}
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    with pytest.raises(PrerequisiteError, match="Node 24"):
        supervisor.verify_node_version()


def test_verify_prerequisites_all_healthy() -> None:
    runner = FakeCommandRunner(responses=_default_prereq_responses())
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    supervisor.verify_prerequisites()
    probes = ["uv --version", "node --version", "npm --version", "git --version", "docker info"]
    cmd_strings = [" ".join(call[0]) for call in runner.calls]
    for probe in probes:
        assert any(probe in cmd for cmd in cmd_strings), f"Missing probe for {probe}"


def test_verify_postgres_health_fails_when_unhealthy() -> None:
    runner = FakeCommandRunner(
        responses={
            "docker compose ps": CommandResult(
                0,
                '[{"Service":"postgres","State":"running","Health":"unhealthy"}]\n',
                "",
            ),
            "docker inspect": CommandResult(0, "unhealthy\n", ""),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    with pytest.raises(PrerequisiteError, match="[Pp]ostgres"):
        supervisor.verify_postgres_health()


def test_sync_dependencies_runs_uv_and_npm() -> None:
    runner = FakeCommandRunner()
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    supervisor.sync_dependencies()
    cmds = [call[0] for call in runner.calls]
    assert ["uv", "sync", "--frozen", "--extra", "dev"] in cmds
    assert ["npm", "ci"] in cmds


def test_migrations_runs_alembic_upgrade_head() -> None:
    runner = FakeCommandRunner()
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    supervisor.run_migrations()
    cmds = [call[0] for call in runner.calls]
    assert any("alembic" in cmd and "upgrade" in cmd and "head" in cmd for cmd in cmds)


def test_runner_image_build_and_inspect() -> None:
    digest = "sha256:" + "a" * 64
    runner = FakeCommandRunner(
        responses={
            "docker inspect": CommandResult(0, f"{digest}\n", ""),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    image_id = supervisor.build_and_inspect_runner_image()
    assert image_id == digest
    cmds = [call[0] for call in runner.calls]
    assert any(cmd[:2] == ["docker", "build"] for cmd in cmds)
    assert any(cmd[:2] == ["docker", "inspect"] for cmd in cmds)


def test_runner_image_rejects_mutable_or_invalid_digest() -> None:
    runner = FakeCommandRunner(
        responses={
            "docker inspect": CommandResult(0, "latest\n", ""),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    with pytest.raises(SupervisorError, match="immutable digest"):
        supervisor.build_and_inspect_runner_image()


def test_operator_rotate_prints_url_once_and_never_persists(capsys: pytest.CaptureFixture[str]) -> None:
    token = "test-token-12345"
    expected_url = f"http://127.0.0.1:3000/#bootstrap={token}"
    runner = FakeCommandRunner(
        responses={
            "forge operator rotate": CommandResult(0, f"{expected_url}\n", ""),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    url = supervisor.issue_operator_bootstrap()
    assert url == expected_url

    captured = capsys.readouterr()
    assert expected_url in captured.out

    # Verify token is not persisted to any repo file
    for p in REPO_ROOT.glob("*.env*"):
        assert token not in p.read_text(encoding="utf-8", errors="ignore")


def test_lifecycle_exact_sequence_and_worker_environment() -> None:
    digest = "sha256:" + "b" * 64
    token = "secret-token-xyz"
    url = f"http://127.0.0.1:3000/#bootstrap={token}"

    responses = _default_prereq_responses()
    responses.update(
        {
            "docker inspect": CommandResult(0, f"{digest}\n", ""),
            "forge operator rotate": CommandResult(0, f"{url}\n", ""),
        }
    )
    runner = FakeCommandRunner(responses=responses)
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)

    api_proc = FakeManagedProcess(name="api")
    worker_proc = FakeManagedProcess(name="worker")
    web_proc = FakeManagedProcess(name="web")
    runner.spawn_plans = [api_proc, worker_proc, web_proc]

    supervisor.verify_python_version((3, 14, 0))
    supervisor.verify_prerequisites()
    supervisor.sync_dependencies()
    supervisor.run_migrations()
    inspected_image = supervisor.build_and_inspect_runner_image()
    bootstrap_url = supervisor.issue_operator_bootstrap()
    assert inspected_image == digest
    assert bootstrap_url == url

    procs = supervisor.start_processes(runner_image=inspected_image)
    assert len(procs) == 3
    assert worker_proc.env is not None
    assert worker_proc.env.get("FORGE_RUNNER_IMAGE") == digest


def test_early_step_failure_aborts_subsequent_steps() -> None:
    # If uv sync fails, migrations and subsequent steps must not run
    runner = FakeCommandRunner(
        responses={
            "uv sync": CommandResult(1, "", "uv sync failed"),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    with pytest.raises(SupervisorError, match="uv sync failed"):
        supervisor.sync_dependencies()

    cmd_strings = [" ".join(call[0]) for call in runner.calls]
    assert not any("alembic" in cmd for cmd in cmd_strings)
    assert not any("docker" in cmd for cmd in cmd_strings)


def test_fail_fast_on_api_nonzero_exit() -> None:
    runner = FakeCommandRunner()
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)

    api_proc = FakeManagedProcess(name="api", exit_code=2)
    worker_proc = FakeManagedProcess(name="worker", exit_code=None)
    web_proc = FakeManagedProcess(name="web", exit_code=None)

    processes = [api_proc, worker_proc, web_proc]
    exit_code = supervisor.supervise(processes, poll_interval=0.01)

    assert exit_code == 2
    assert worker_proc.terminated or worker_proc.killed
    assert web_proc.terminated or web_proc.killed


def test_fail_fast_on_worker_unexpected_zero_exit() -> None:
    runner = FakeCommandRunner()
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)

    api_proc = FakeManagedProcess(name="api", exit_code=None)
    worker_proc = FakeManagedProcess(name="worker", exit_code=0)
    web_proc = FakeManagedProcess(name="web", exit_code=None)

    processes = [api_proc, worker_proc, web_proc]
    exit_code = supervisor.supervise(processes, poll_interval=0.01)

    assert exit_code != 0
    assert api_proc.terminated or api_proc.killed
    assert web_proc.terminated or web_proc.killed


def test_fail_fast_on_web_exit() -> None:
    runner = FakeCommandRunner()
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)

    api_proc = FakeManagedProcess(name="api", exit_code=None)
    worker_proc = FakeManagedProcess(name="worker", exit_code=None)
    web_proc = FakeManagedProcess(name="web", exit_code=1)

    processes = [api_proc, worker_proc, web_proc]
    exit_code = supervisor.supervise(processes, poll_interval=0.01)

    assert exit_code == 1
    assert api_proc.terminated or api_proc.killed
    assert worker_proc.terminated or worker_proc.killed


def test_ctrl_c_graceful_shutdown() -> None:
    runner = FakeCommandRunner()
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)

    api_proc = FakeManagedProcess(name="api")
    worker_proc = FakeManagedProcess(name="worker")
    web_proc = FakeManagedProcess(name="web")

    processes = [api_proc, worker_proc, web_proc]

    def trigger_interrupt() -> None:
        import time
        time.sleep(0.05)
        supervisor.stop_event.set()

    thread = threading.Thread(target=trigger_interrupt)
    thread.start()
    exit_code = supervisor.supervise(processes, poll_interval=0.01)
    thread.join()

    assert exit_code == 0
    assert api_proc.terminated or api_proc.killed
    assert worker_proc.terminated or worker_proc.killed
    assert web_proc.terminated or web_proc.killed


def test_package_json_scripts_contract() -> None:
    package_json_path = REPO_ROOT / "package.json"
    assert package_json_path.exists()
    pkg = json.loads(package_json_path.read_text(encoding="utf-8"))
    scripts = pkg.get("scripts", {})

    required_scripts = [
        "dev",
        "test",
        "lint",
        "typecheck",
        "build",
        "test:integration",
        "verify",
    ]
    for script_name in required_scripts:
        assert script_name in scripts, f"package.json missing '{script_name}' script"

    # Preserved existing scripts
    existing_scripts = [
        "dev:web",
        "build:web",
        "lint:web",
        "typecheck:web",
        "test:web",
        "api:generate",
        "api:check",
    ]
    for script_name in existing_scripts:
        assert script_name in scripts, f"package.json missing preserved script '{script_name}'"

    # verify must exclude live provider / GitHub tests
    verify_cmd = scripts["verify"]
    assert "not live_provider" in verify_cmd
    assert "not live_github" in verify_cmd


def test_wrapper_scripts_invoke_dev_py() -> None:
    ps1_path = REPO_ROOT / "scripts" / "dev.ps1"
    sh_path = REPO_ROOT / "scripts" / "dev.sh"

    assert ps1_path.exists()
    assert sh_path.exists()

    ps1_content = ps1_path.read_text(encoding="utf-8")
    sh_content = sh_path.read_text(encoding="utf-8")

    assert "dev.py" in ps1_content
    assert "dev.py" in sh_content
    assert "set -euo pipefail" in sh_content


def _is_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            process_query_limited_info = 0x1000
            handle = kernel32.OpenProcess(process_query_limited_info, False, pid)
            if not handle:
                return False
            exit_code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return exit_code.value == 259  # STILL_ACTIVE
        except (ImportError, AttributeError, OSError):
            return False
    else:
        try:
            import os
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False


def test_subprocess_managed_process_kill_tree_kills_descendants(tmp_path: Path) -> None:
    import time

    from scripts.dev import DefaultCommandRunner

    pid_file = tmp_path / "pids.txt"
    pid_file_str = str(pid_file).replace("\\", "\\\\")
    child_code = (
        "import sys, os, subprocess, time, pathlib; "
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path(r'{pid_file_str}').write_text(f'{{os.getpid()}},{{grandchild.pid}}'); "
        "grandchild.wait()"
    )
    runner = DefaultCommandRunner()
    managed = runner.spawn("parent", [sys.executable, "-c", child_code])

    for _ in range(50):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        time.sleep(0.1)

    assert pid_file.exists(), "Child process failed to start and write pids"
    pids = [int(p) for p in pid_file.read_text().strip().split(",")]
    parent_pid, grandchild_pid = pids[0], pids[1]

    managed.kill_tree()
    managed.process.wait(timeout=5)

    for _ in range(50):
        if not _is_pid_alive(grandchild_pid):
            break
        time.sleep(0.1)

    assert not _is_pid_alive(parent_pid), f"Parent {parent_pid} is still alive"
    assert not _is_pid_alive(grandchild_pid), f"Grandchild {grandchild_pid} was leaked!"


def test_cleanup_terminates_descendants_when_parent_already_exited(tmp_path: Path) -> None:
    import time

    from scripts.dev import DefaultCommandRunner

    pid_file = tmp_path / "pids_exited_parent.txt"
    pid_file_str = str(pid_file).replace("\\", "\\\\")
    child_code = (
        "import sys, os, subprocess, time, pathlib; "
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path(r'{pid_file_str}').write_text(f'{{os.getpid()}},{{grandchild.pid}}'); "
        "time.sleep(0.2); sys.exit(0)"
    )
    runner = DefaultCommandRunner()
    managed = runner.spawn("parent", [sys.executable, "-c", child_code])

    for _ in range(50):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        time.sleep(0.1)

    assert pid_file.exists(), "Child process failed to start and write pids"
    pids = [int(p) for p in pid_file.read_text().strip().split(",")]
    parent_pid, grandchild_pid = pids[0], pids[1]

    # Wait for the direct parent to exit cleanly
    managed.process.wait(timeout=5)
    assert managed.poll() == 0, "Parent did not exit with 0 as expected"
    assert not _is_pid_alive(parent_pid), "Parent should be dead"

    # Grandchild is still running in background
    assert _is_pid_alive(grandchild_pid), "Grandchild should be alive before supervisor.cleanup"

    # Now run supervisor cleanup
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    supervisor.cleanup([managed])

    # Grandchild MUST be terminated by cleanup()
    for _ in range(50):
        if not _is_pid_alive(grandchild_pid):
            break
        time.sleep(0.1)

    assert not _is_pid_alive(grandchild_pid), f"Grandchild {grandchild_pid} was leaked after cleanup!"


def test_partial_startup_failure_cleans_up_already_spawned_processes() -> None:
    class FailingRunner(FakeCommandRunner):
        def spawn(
            self,
            name: str,
            cmd: list[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
        ) -> ManagedProcess:
            if name == "worker":
                raise RuntimeError("Failed to spawn worker")
            return super().spawn(name, cmd, cwd=cwd, env=env)

    runner = FailingRunner()
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)

    with pytest.raises(RuntimeError, match="Failed to spawn worker"):
        supervisor.start_processes(runner_image="sha256:" + "a" * 64)

    assert len(runner.spawned) == 1
    api_proc = runner.spawned[0]
    assert api_proc.name == "api"
    assert api_proc.terminated or api_proc.killed


def test_cancellation_during_bootstrap_aborts_without_starting_services() -> None:
    responses = _default_prereq_responses()
    runner = FakeCommandRunner(responses=responses)
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)

    supervisor.stop_event.set()
    exit_code = supervisor.run()

    assert exit_code == 0
    assert len(runner.spawned) == 0
    cmds = [call[0] for call in runner.calls]
    assert not any("alembic" in cmd for cmd in cmds)
    assert not any("operator" in cmd for cmd in cmds)


def test_cancellation_during_active_bootstrap_command() -> None:
    responses = _default_prereq_responses()

    class CancellingRunner(FakeCommandRunner):
        def run(
            self,
            cmd: list[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            timeout: float | None = None,
            stop_event: threading.Event | None = None,
        ) -> CommandResult:
            if "alembic" in cmd and stop_event is not None:
                stop_event.set()
            return super().run(cmd, cwd=cwd, env=env, timeout=timeout, stop_event=stop_event)

    runner = CancellingRunner(responses=responses)
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    exit_code = supervisor.run()

    assert exit_code == 0
    assert len(runner.spawned) == 0
    cmds = [call[0] for call in runner.calls]
    assert not any("operator" in cmd for cmd in cmds)


def test_windows_handle_64bit_full_width_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.dev import _assign_process_to_job, _create_windows_job_object

    if sys.platform != "win32":
        assert _create_windows_job_object() is None
        return

    import ctypes
    kernel32 = ctypes.windll.kernel32

    class DummyProc:
        _handle = 0x7FFFFFFF00000000
        pid = 12345

    received_handles: list[Any] = []

    def mock_assign(job: Any, h_proc: Any) -> int:
        received_handles.append(h_proc)
        return 1

    monkeypatch.setattr(kernel32, "AssignProcessToJobObject", mock_assign)

    job = _create_windows_job_object()
    try:
        assert job is not None
        _assign_process_to_job(job, DummyProc())  # type: ignore[arg-type]
        assert len(received_handles) == 1
        h_val = received_handles[0].value if hasattr(received_handles[0], "value") else received_handles[0]
        assert h_val == 0x7FFFFFFF00000000

        monkeypatch.setattr(kernel32, "AssignProcessToJobObject", lambda j, h: 0)
        with pytest.raises(SupervisorError, match="AssignProcessToJobObject failed"):
            _assign_process_to_job(job, DummyProc())  # type: ignore[arg-type]
    finally:
        if job:
            kernel32.CloseHandle(job)


def test_create_windows_job_closes_handle_on_config_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        return

    import ctypes

    from scripts.dev import _create_windows_job_object

    kernel32 = ctypes.windll.kernel32
    closed_handles: list[Any] = []
    original_close = kernel32.CloseHandle

    def mock_close(h: Any) -> int:
        closed_handles.append(h)
        return original_close(h)

    monkeypatch.setattr(kernel32, "SetInformationJobObject", lambda j, c, p, s: 0)
    monkeypatch.setattr(kernel32, "CloseHandle", mock_close)

    with pytest.raises(SupervisorError, match="SetInformationJobObject failed"):
        _create_windows_job_object()

    assert len(closed_handles) == 1, "Job handle was leaked on config failure!"


def test_postgres_health_ambiguity_and_scoping() -> None:
    # 1. Running without health -> must raise PrerequisiteError
    runner = FakeCommandRunner(
        responses={
            "docker compose ps": CommandResult(
                0,
                '[{"Service":"postgres","State":"running","Health":"","Status":"Up 10 seconds"}]\n',
                "",
            ),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    with pytest.raises(PrerequisiteError, match="PostgreSQL container is not healthy"):
        supervisor.verify_postgres_health()

    # 2. Starting health -> must raise PrerequisiteError
    runner = FakeCommandRunner(
        responses={
            "docker compose ps": CommandResult(
                0,
                '[{"Service":"postgres","State":"running","Health":"starting","Status":"Up 5s"}]\n',
                "",
            ),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    with pytest.raises(PrerequisiteError, match="PostgreSQL container is not healthy"):
        supervisor.verify_postgres_health()

    # 3. Verify cwd scoping
    runner = FakeCommandRunner(
        responses={
            "docker compose ps": CommandResult(
                0,
                '[{"Service":"postgres","State":"running","Health":"healthy"}]\n',
                "",
            ),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    supervisor.verify_postgres_health()

    compose_call = next(c for c in runner.calls if "compose" in " ".join(c[0]))
    assert compose_call[1]["cwd"] == REPO_ROOT

    # Verify no hardcoded container name was queried
    all_cmds = [" ".join(c[0]) for c in runner.calls]
    assert not any("parallel-forge-postgres-1" in cmd for cmd in all_cmds)


def test_operator_rotate_failure_redacts_secrets() -> None:
    token = "sensitive-super-secret-token-12345"
    runner = FakeCommandRunner(
        responses={
            "forge operator rotate": CommandResult(
                1,
                "",
                f"Error: failed to rotate with http://127.0.0.1:3000/#bootstrap={token} and token={token}",
            ),
        }
    )
    supervisor = DevSupervisor(repo_root=REPO_ROOT, runner=runner)
    with pytest.raises(SupervisorError) as exc_info:
        supervisor.issue_operator_bootstrap()

    err_str = str(exc_info.value)
    assert token not in err_str, f"Secret token was leaked in error message: {err_str}"
    assert "bootstrap=[REDACTED]" in err_str
    assert "token=[REDACTED]" in err_str


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job handles")
def test_spawn_failure_closes_new_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import dev

    closed: list[int] = []
    monkeypatch.setattr(dev, "_create_windows_job_object", lambda: 123)
    monkeypatch.setattr(dev.kernel32, "CloseHandle", lambda handle: closed.append(handle))

    def fail_spawn(*args: Any, **kwargs: Any) -> Any:
        raise OSError("spawn failed")

    monkeypatch.setattr(dev.subprocess, "Popen", fail_spawn)
    with pytest.raises(OSError, match="spawn failed"):
        dev.DefaultCommandRunner().spawn("fixture", ["missing-fixture-executable"])
    assert closed == [123]


def test_windows_npm_resolution_and_quoting() -> None:
    from scripts.dev import _resolve_cmd

    resolved = _resolve_cmd(["npm", "run", "dev:web"])
    if sys.platform == "win32":
        assert isinstance(resolved, list)
        assert len(resolved) == 3
        assert resolved[0].lower().endswith((".cmd", ".exe", ".bat"))
        assert resolved[1:] == ["run", "dev:web"]
    else:
        assert resolved == ["npm", "run", "dev:web"]
