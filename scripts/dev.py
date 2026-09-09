"""One-command local development supervisor for Forge."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO


class PrerequisiteError(RuntimeError):
    """Raised when a development prerequisite is not satisfied."""


class SupervisorError(RuntimeError):
    """Raised when a supervisor step fails."""


class SupervisorCancelled(SupervisorError):
    """Raised when supervisor execution is cancelled via signal or stop_event."""


@dataclass
class CommandResult:
    """Execution output of a completed command."""

    returncode: int
    stdout: str
    stderr: str


class ManagedProcess(Protocol):
    """Protocol for a supervisor-managed child process."""

    name: str
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill_tree(self) -> None: ...


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL

    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL

    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_ulong

    class IOCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JobBasicLimitInfo(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JobExtendedLimitInfo(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobBasicLimitInfo),
            ("IoInfo", IOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]


def _create_windows_job_object() -> Any:
    """Create a Windows Job Object configured to kill child processes on close."""
    if sys.platform != "win32":
        return None
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            err = kernel32.GetLastError()
            raise SupervisorError(f"CreateJobObjectW failed with error {err}")

        info = JobExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        job_info_class = 9  # JobObjectExtendedLimitInformation

        ok = kernel32.SetInformationJobObject(
            job,
            job_info_class,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            err = kernel32.GetLastError()
            kernel32.CloseHandle(job)
            raise SupervisorError(f"SetInformationJobObject failed with error {err}")
        return job
    except (ImportError, AttributeError, OSError):
        return None


def _assign_process_to_job(job: Any, proc: subprocess.Popen[Any]) -> None:
    if job is None or sys.platform != "win32":
        return
    handle = getattr(proc, "_handle", None)
    if handle is None:
        raise SupervisorError("Child process handle is None on Windows")
    h_proc = wintypes.HANDLE(int(handle))
    ok = kernel32.AssignProcessToJobObject(job, h_proc)
    if not ok:
        err = kernel32.GetLastError()
        raise SupervisorError(f"AssignProcessToJobObject failed with error {err}")


def _resolve_cmd(cmd: list[str]) -> list[str]:
    """Resolve command executable on Windows if needed, preserving argv without shell."""
    if not cmd:
        return cmd
    if sys.platform == "win32":
        exe = cmd[0]
        resolved = shutil.which(exe)
        if resolved:
            return [resolved, *cmd[1:]]
    return list(cmd)


def redact_secrets(text: str) -> str:
    """Redact sensitive bootstrap tokens and credentials from output/errors."""
    text = re.sub(
        r"(bootstrap=)[A-Za-z0-9._~+/-]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(token=)[A-Za-z0-9._~+/-]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(bearer\s+)[A-Za-z0-9._~+/-]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text


class SubprocessManagedProcess:
    """Subprocess-backed implementation of ManagedProcess with process tree cleanup."""

    def __init__(
        self,
        name: str,
        process: subprocess.Popen[str],
        job: Any = None,
        pgid: int | None = None,
    ) -> None:
        self.name = name
        self.process = process
        self.pid = process.pid
        self.job = job
        self.pgid = pgid if pgid is not None else process.pid
        self._cleaned_up = False

    def poll(self) -> int | None:
        return self.process.poll()

    def terminate(self) -> None:
        if sys.platform != "win32":
            with suppress(OSError):
                os.killpg(self.pgid, signal.SIGTERM)
        with suppress(OSError):
            self.process.terminate()

    def kill_tree(self) -> None:
        """Bounded, recursive force-kill of the owned process tree."""
        if self._cleaned_up:
            return
        self._cleaned_up = True

        if sys.platform == "win32":
            if self.job is not None:
                with suppress(Exception):
                    kernel32.TerminateJobObject(self.job, 1)
                with suppress(Exception):
                    kernel32.CloseHandle(self.job)
                self.job = None

            with suppress(subprocess.SubprocessError, OSError):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.pid)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
        else:
            with suppress(OSError):
                os.killpg(self.pgid, signal.SIGKILL)
            with suppress(OSError):
                os.kill(self.pid, signal.SIGKILL)

        with suppress(OSError):
            self.process.kill()


class CommandRunner(Protocol):
    """Protocol for executing and spawning commands."""

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> CommandResult: ...

    def spawn(
        self,
        name: str,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ManagedProcess: ...


class DefaultCommandRunner:
    """Default runner using standard subprocess with job object and cancellation support."""

    def __init__(self) -> None:
        self.job: Any = None

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> CommandResult:
        resolved_cmd = _resolve_cmd(cmd)
        job: Any = None
        creationflags = 0
        kwargs: dict[str, Any] = {
            "cwd": str(cwd) if cwd else None,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }

        if sys.platform == "win32":
            job = _create_windows_job_object()
            if job is not None:
                creationflags |= 0x4  # CREATE_SUSPENDED
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(resolved_cmd, **kwargs)
        except OSError as exc:
            if job is not None and sys.platform == "win32":
                with suppress(Exception):
                    kernel32.CloseHandle(job)
            return CommandResult(returncode=127, stdout="", stderr=str(exc))

        managed = SubprocessManagedProcess(
            "_run",
            proc,
            job=job,
            pgid=proc.pid if sys.platform != "win32" else None,
        )

        if sys.platform == "win32" and job is not None:
            try:
                _assign_process_to_job(job, proc)
                status = ntdll.NtResumeProcess(wintypes.HANDLE(int(proc._handle)))
                if status != 0:
                    managed.kill_tree()
                    return CommandResult(
                        returncode=-1,
                        stdout="",
                        stderr=f"NtResumeProcess failed with status {status:#x}",
                    )
            except (OSError, SupervisorError, RuntimeError) as exc:
                managed.kill_tree()
                return CommandResult(returncode=-1, stdout="", stderr=str(exc))

        deadline = (time.time() + timeout) if timeout is not None else None
        stdout = ""
        stderr = ""

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    managed.kill_tree()
                    return CommandResult(returncode=-2, stdout="", stderr="Command cancelled")

                slice_timeout = 0.1
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        managed.kill_tree()
                        return CommandResult(returncode=-1, stdout="", stderr="Command timed out")
                    slice_timeout = min(slice_timeout, remaining)

                try:
                    stdout, stderr = proc.communicate(timeout=slice_timeout)
                    break
                except subprocess.TimeoutExpired:
                    continue

            return CommandResult(
                returncode=proc.returncode if proc.returncode is not None else 0,
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            managed.kill_tree()

    def spawn(
        self,
        name: str,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ManagedProcess:
        resolved_cmd = _resolve_cmd(cmd)
        job: Any = None
        creationflags = 0
        kwargs: dict[str, Any] = {
            "cwd": str(cwd) if cwd else None,
            "env": env,
            "text": True,
        }

        if sys.platform == "win32":
            job = _create_windows_job_object()
            if job is not None:
                creationflags |= 0x4  # CREATE_SUSPENDED
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(resolved_cmd, **kwargs)
        except BaseException:
            if job is not None and sys.platform == "win32":
                kernel32.CloseHandle(job)
            raise
        managed = SubprocessManagedProcess(
            name,
            proc,
            job=job,
            pgid=proc.pid if sys.platform != "win32" else None,
        )

        if sys.platform == "win32" and job is not None:
            try:
                _assign_process_to_job(job, proc)
                status = ntdll.NtResumeProcess(wintypes.HANDLE(int(proc._handle)))
                if status != 0:
                    managed.kill_tree()
                    raise SupervisorError(f"NtResumeProcess failed with status {status:#x}")
            except BaseException:
                managed.kill_tree()
                raise

        return managed


class DevSupervisor:
    """Orchestrates local development prerequisites, builds, and services."""

    def __init__(
        self,
        repo_root: Path | None = None,
        *,
        runner: CommandRunner | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.repo_root = repo_root or Path(__file__).resolve().parents[1]
        self.runner = runner or DefaultCommandRunner()
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.stop_event = threading.Event()

    def log(self, message: str) -> None:
        print(f"[dev] {message}", file=self.stdout, flush=True)

    def _check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise SupervisorCancelled("Supervisor received shutdown signal.")

    def run_cmd(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command with cancellation check before and after."""
        self._check_cancelled()
        try:
            res = self.runner.run(
                cmd,
                cwd=cwd,
                env=env,
                timeout=timeout,
                stop_event=self.stop_event,  # type: ignore[call-arg]
            )
        except TypeError:
            res = self.runner.run(cmd, cwd=cwd, env=env, timeout=timeout)
        self._check_cancelled()
        return res

    def verify_python_version(self, version_info: tuple[int, ...] | None = None) -> None:
        """Verify Python 3.14 runtime."""
        info = version_info or sys.version_info
        if (info[0], info[1]) != (3, 14):
            raise PrerequisiteError(
                f"Python 3.14 is required. Found Python {info[0]}.{info[1]}."
            )

    def verify_node_version(self) -> None:
        """Verify Node 24 runtime."""
        res = self.run_cmd(["node", "--version"])
        if res.returncode != 0:
            raise PrerequisiteError(f"Node.js is not available: {res.stderr.strip()}")
        version = res.stdout.strip()
        if not version.startswith("v24."):
            raise PrerequisiteError(
                f"Node 24 is required. Found Node {version}."
            )

    def verify_postgres_health(self) -> None:
        """Verify PostgreSQL container health scoped to repository compose file."""
        res = self.run_cmd(
            ["docker", "compose", "ps", "--format", "json", "postgres"],
            cwd=self.repo_root,
        )
        if res.returncode != 0:
            raise PrerequisiteError(
                f"docker compose ps failed: {res.stderr.strip() or res.stdout.strip()}"
            )

        output = res.stdout.strip()
        if not output:
            raise PrerequisiteError(
                "PostgreSQL container is not running. Run 'docker compose up -d postgres' to start it."
            )

        try:
            if output.startswith("["):
                items = json.loads(output)
            else:
                items = [json.loads(line) for line in output.splitlines() if line.strip()]
        except (json.JSONDecodeError, ValueError):
            raise PrerequisiteError(f"Failed to parse docker compose ps output: {output}")

        if not items:
            raise PrerequisiteError(
                "PostgreSQL container is not running. Run 'docker compose up -d postgres' to start it."
            )

        is_healthy = False
        for item in items:
            health = str(item.get("Health", "")).strip().lower()
            status = str(item.get("Status", "")).strip().lower()

            if health == "healthy":
                is_healthy = True
                break
            if "(healthy)" in status:
                is_healthy = True
                break

        if not is_healthy:
            raise PrerequisiteError(
                "PostgreSQL container is not healthy. Run 'docker compose up -d postgres' to start it."
            )

    def verify_prerequisites(self) -> None:
        """Verify all development prerequisites."""
        self.log("Verifying prerequisites...")
        self.verify_python_version()

        res = self.run_cmd(["uv", "--version"])
        if res.returncode != 0:
            raise PrerequisiteError(f"uv is not available: {res.stderr.strip()}")

        self.verify_node_version()

        res = self.run_cmd(["npm", "--version"])
        if res.returncode != 0:
            raise PrerequisiteError(f"npm is not available: {res.stderr.strip()}")

        res = self.run_cmd(["git", "--version"])
        if res.returncode != 0:
            raise PrerequisiteError(f"git is not available: {res.stderr.strip()}")

        res = self.run_cmd(["docker", "--version"])
        if res.returncode != 0:
            raise PrerequisiteError(f"docker CLI is not available: {res.stderr.strip()}")

        res_info = self.run_cmd(["docker", "info"], timeout=15)
        if res_info.returncode != 0:
            raise PrerequisiteError(
                f"Docker daemon is not running or accessible: {res_info.stderr.strip()}"
            )

        self.verify_postgres_health()
        self.log("All prerequisites satisfied.")

    def sync_dependencies(self) -> None:
        """Sync Python and Node locked dependencies."""
        self.log("Syncing Python dependencies with uv sync --frozen --extra dev...")
        res = self.run_cmd(
            ["uv", "sync", "--frozen", "--extra", "dev"],
            cwd=self.repo_root,
        )
        if res.returncode != 0:
            raise SupervisorError(f"uv sync failed: {res.stderr.strip() or res.stdout.strip()}")

        self.log("Installing Node dependencies with npm ci...")
        res = self.run_cmd(["npm", "ci"], cwd=self.repo_root)
        if res.returncode != 0:
            raise SupervisorError(f"npm ci failed: {res.stderr.strip() or res.stdout.strip()}")

    def run_migrations(self) -> None:
        """Apply database migrations via Alembic."""
        self.log("Applying database migrations with Alembic...")
        res = self.run_cmd(
            ["uv", "run", "--frozen", "alembic", "upgrade", "head"],
            cwd=self.repo_root,
        )
        if res.returncode != 0:
            raise SupervisorError(f"Alembic migration failed: {res.stderr.strip() or res.stdout.strip()}")

    def build_and_inspect_runner_image(self) -> str:
        """Build the Docker runner image and inspect its immutable digest."""
        self.log("Building Docker runner image from Dockerfile.runner...")
        build_res = self.run_cmd(
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "-f",
                "Dockerfile.runner",
                "-t",
                "parallel-forge-runner:latest",
                ".",
            ],
            cwd=self.repo_root,
            timeout=600,
        )
        if build_res.returncode != 0:
            raise SupervisorError(
                f"Docker runner image build failed: {build_res.stderr.strip() or build_res.stdout.strip()}"
            )

        inspect_res = self.run_cmd(
            ["docker", "inspect", "--format", "{{.Id}}", "parallel-forge-runner:latest"]
        )
        if inspect_res.returncode != 0:
            raise SupervisorError(f"Docker inspect failed: {inspect_res.stderr.strip()}")

        image_id = inspect_res.stdout.strip()
        digest_pattern = re.compile(r"\Asha256:[0-9a-f]{64}\Z", re.ASCII)
        if not digest_pattern.match(image_id):
            raise SupervisorError(
                f"Runner image inspect must return an immutable digest (sha256:hex), got: {image_id}"
            )

        self.log(f"Runner image built with immutable ID: {image_id}")
        return image_id

    def issue_operator_bootstrap(self) -> str:
        """Rotate operator credentials and print bootstrap URL directly once."""
        self.log("Rotating operator credentials and generating bootstrap URL...")
        res = self.run_cmd(
            ["uv", "run", "--frozen", "forge", "operator", "rotate"],
            cwd=self.repo_root,
        )
        if res.returncode != 0:
            raw_err = res.stderr.strip() or res.stdout.strip()
            redacted_err = redact_secrets(raw_err)
            raise SupervisorError(f"Operator rotate failed: {redacted_err}")

        output = res.stdout.strip()
        url = ""
        for line in output.splitlines():
            line_s = line.strip()
            if "bootstrap=" in line_s:
                url = line_s
                break

        if not url:
            url = output

        print(f"\nForge Operator Bootstrap URL:\n{url}\n", file=self.stdout, flush=True)
        return url

    def start_processes(self, runner_image: str) -> list[ManagedProcess]:
        """Spawn separate forge-api, forge-worker, and Next.js processes."""
        self.log("Starting Forge processes...")
        api_cmd = ["uv", "run", "--frozen", "forge-api"]
        worker_cmd = ["uv", "run", "--frozen", "forge-worker"]
        web_cmd = ["npm", "run", "dev:web"]

        worker_env = os.environ.copy()
        worker_env["FORGE_RUNNER_IMAGE"] = runner_image

        spawned: list[ManagedProcess] = []
        try:
            self._check_cancelled()
            spawned.append(self.runner.spawn("api", api_cmd, cwd=self.repo_root))
            self._check_cancelled()
            spawned.append(
                self.runner.spawn("worker", worker_cmd, cwd=self.repo_root, env=worker_env)
            )
            self._check_cancelled()
            spawned.append(self.runner.spawn("web", web_cmd, cwd=self.repo_root))
            self.log("All processes started (forge-api, forge-worker, web).")
            return spawned
        except BaseException:
            self.cleanup(spawned)
            raise

    def cleanup(self, processes: list[ManagedProcess], timeout: float = 5.0) -> None:
        """Gracefully terminate processes and boundedly force kill process trees."""
        self.log("Shutting down processes...")
        for p in processes:
            p.terminate()

        deadline = time.time() + timeout
        for p in processes:
            remaining = max(0.0, deadline - time.time())
            while p.poll() is None and remaining > 0:
                time.sleep(0.05)
                remaining = max(0.0, deadline - time.time())

        for p in processes:
            p.kill_tree()

        self.log("Process cleanup complete.")

    def supervise(
        self,
        processes: list[ManagedProcess],
        poll_interval: float = 0.5,
    ) -> int:
        """Monitor running processes. Fail-fast if any required process exits."""
        try:
            while not self.stop_event.is_set():
                for p in processes:
                    exit_code = p.poll()
                    if exit_code is not None:
                        self.log(
                            f"Required process '{p.name}' exited unexpectedly with code {exit_code}."
                        )
                        self.cleanup(processes)
                        return exit_code if exit_code != 0 else 1

                time.sleep(poll_interval)

            self.cleanup(processes)
            return 0
        except KeyboardInterrupt:
            self.log("Received Ctrl+C interrupt.")
            self.cleanup(processes)
            return 0

    def run(self) -> int:
        """Execute complete local development workflow."""
        processes: list[ManagedProcess] = []
        try:
            self._check_cancelled()
            self.verify_prerequisites()
            self._check_cancelled()
            self.sync_dependencies()
            self._check_cancelled()
            self.run_migrations()
            self._check_cancelled()
            runner_image = self.build_and_inspect_runner_image()
            self._check_cancelled()
            self.issue_operator_bootstrap()
            self._check_cancelled()
            processes = self.start_processes(runner_image=runner_image)
            return self.supervise(processes)
        except (SupervisorCancelled, KeyboardInterrupt):
            self.log("Supervisor cancelled.")
            if processes:
                self.cleanup(processes)
            return 0
        except BaseException:
            if processes:
                self.cleanup(processes)
            raise


def main() -> int:
    """CLI entry point for scripts/dev.py."""
    supervisor = DevSupervisor()

    def handle_signal(signum: int, frame: Any) -> None:
        del signum, frame
        supervisor.stop_event.set()

    with suppress(ValueError):
        signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        with suppress(ValueError):
            signal.signal(signal.SIGTERM, handle_signal)

    try:
        return supervisor.run()
    except PrerequisiteError as err:
        print(f"[dev error] Prerequisite check failed: {err}", file=sys.stderr)
        return 1
    except SupervisorError as err:
        print(f"[dev error] Supervisor failed: {err}", file=sys.stderr)
        return 1
    except (SupervisorCancelled, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    sys.exit(main())
