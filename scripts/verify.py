"""Reproducible local verification harness for Forge.

Promotes the ad-hoc Linux/PostgreSQL verification recorded during v0.2 into a
maintained entry point that runs from a checkout, next to ``scripts/dev.py``.

What one run does
-----------------
* runs exactly one named selection, or an explicit ``--focused`` subset of it;
* never expands a focused selection, and never starts the broad backend
  selection without an explicit ``--full`` acknowledgement;
* executes Linux/PostgreSQL selections in a controller container started with
  ``--init`` (so descendant termination is reaped instead of hidden as zombies)
  and verified at run time to use UID/GID 1000, distinct from the sandbox
  identity 10001 in Dockerfile.runner;
* attaches an ephemeral PostgreSQL instance that shares only the controller's
  network namespace. The fixtures' fixed loopback endpoint (``127.0.0.1:5435``)
  stays valid, while the controller keeps no outbound network and no Docker
  socket;
* resolves frozen dependencies from ``uv.lock`` at image build time;
* records candidate/input identity, exact argv, step outcomes, JUnit output and
  owned-resource cleanup under ``.llm-output/local-verification/<run-id>/``.

What one run never does
-----------------------
* dispatch GitHub Actions; hosted CI stays the authority for CI verification;
* call a live provider, probe provider quota or reserve budget;
* retry a failed step automatically;
* forward host credentials: child processes receive a filtered environment, the
  controller container inherits no host environment at all, and only the *names*
  of filtered variables are recorded.

Run ``python scripts/verify.py list`` for the available selections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # The harness also runs as ``python scripts/verify.py``, where the sibling
    # supervisor module is not yet importable as ``scripts.dev``.
    sys.path.insert(0, str(REPO_ROOT))

# Imported after the repository root above is placed on sys.path.
from scripts.dev import (
    CommandResult,
    CommandRunner,
    ConfigurationError,
    DefaultCommandRunner,
    PrerequisiteError,
    SupervisorCancelled,
    SupervisorError,
)

EVIDENCE_RELATIVE_ROOT = Path(".llm-output") / "local-verification"
CONTROLLER_IMAGE_REPOSITORY = "forge-local-verification"
CONTROLLER_IMAGE_INPUTS = ("Dockerfile.verify", "pyproject.toml", "uv.lock")
CONTROLLER_USER = "1000:1000"
CONTROLLER_UID = 1000
SANDBOX_UID = 10001
CONTROLLER_INIT_PROCESS_NAMES = frozenset({"docker-init", "tini"})
CONTROLLER_MOUNT_TARGET = "/workspace"
CONTROLLER_PYTHONPATH = f"{CONTROLLER_MOUNT_TARGET}/apps/orchestrator/src"
POSTGRES_IMAGE = "postgres:17"
POSTGRES_PORT = 5435
POSTGRES_DATABASE = "forge"
POSTGRES_USER = "forge"
# Throwaway loopback database inside the controller-only network namespace. The
# same literal is already published in docker-compose.yml and hardcoded by the
# persistence fixtures, so it is a fixture constant rather than a credential.
POSTGRES_PASSWORD = "forge"
POSTGRES_READY_TIMEOUT_SECONDS = 120.0
CONTROLLER_MARKERS = "not live_provider and not live_github and not docker"
HOST_MARKERS = "not integration and not live_provider and not live_github and not docker"
PYTEST_CACHE_OFF = ("-p", "no:cacheprovider")
PYTEST_NO_TESTS_COLLECTED = 5

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_CONFIGURATION = 2
EXIT_ENVIRONMENT = 3
EXIT_CANCELLED = 130

_RESERVED_ENVIRONMENT_NAMES = frozenset(
    {"DATABASE_URL", "GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK"}
)
_RESERVED_ENVIRONMENT_PREFIXES = (
    "FORGE_",
    "ANTHROPIC_",
    "OPENAI_",
    "GEMINI_",
    "GOOGLE_",
    "DEEPSEEK_",
    "CLAUDE_",
    "AZURE_",
    "AWS_",
)
_ALLOWED_ENVIRONMENT_PREFIXES = ("FORGE_E2E_",)
_SECRET_ENVIRONMENT_PATTERN = re.compile(
    r"(API_?KEYS?|TOKEN|SECRET|PASSWORD|CREDENTIAL|PASSWD)", re.IGNORECASE
)
_IMMUTABLE_IMAGE_PATTERN = re.compile(r"\Asha256:[0-9a-f]{64}\Z", re.ASCII)
_CONTROLLER_NAME_PATTERN = re.compile(r"\Aforge-verify-[a-z0-9.-]+\Z", re.ASCII)

# Executed inside the controller container. It proves the identity, the PID 1
# reaper and the resolved frozen dependency set before any test runs.
CONTROLLER_PROBE_CODE = (
    "import importlib.metadata as md, os, sys\n"
    "print('uid=%d' % os.getuid())\n"
    "print('gid=%d' % os.getgid())\n"
    "with open('/proc/1/comm') as handle:\n"
    "    print('init=' + handle.read().strip())\n"
    "print('python=' + '.'.join(str(part) for part in sys.version_info[:3]))\n"
    "print('pytest=' + md.version('pytest'))\n"
    "print('uv_lock_present=%s' % os.path.exists('/workspace/uv.lock'))\n"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _first_line(result: CommandResult) -> str:
    for stream in (result.stderr, result.stdout):
        for line in stream.splitlines():
            if line.strip():
                return line.strip()
    return f"exit code {result.returncode}"


def slugify(value: str) -> str:
    """Return a filesystem- and container-name-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def is_reserved_environment_name(name: str) -> bool:
    """Return True when a host variable must not reach a verification child."""
    if name in _RESERVED_ENVIRONMENT_NAMES:
        return True
    if name.startswith(_ALLOWED_ENVIRONMENT_PREFIXES):
        return False
    if name.startswith(_RESERVED_ENVIRONMENT_PREFIXES):
        return True
    return bool(_SECRET_ENVIRONMENT_PATTERN.search(name))


@dataclass(frozen=True)
class Step:
    """A fixed command executed verbatim."""

    name: str
    argv: tuple[str, ...]
    timeout: float


@dataclass(frozen=True)
class PytestStep:
    """A pytest invocation whose targets depend on the requested selection."""

    name: str
    prefix: tuple[str, ...]
    suffix: tuple[str, ...]
    targets: tuple[str, ...]
    timeout: float


@dataclass(frozen=True)
class Selection:
    """One named, bounded verification selection."""

    name: str
    summary: str
    container: bool
    broad: bool
    steps: tuple[Step, ...] = ()
    pytest_step: PytestStep | None = None


@dataclass(frozen=True)
class RunRequest:
    """A validated request for exactly one selection."""

    selection: str
    focused: tuple[str, ...] = ()
    full: bool = False
    timeout: float | None = None
    image: str | None = None


UNIT_TARGETS = (
    "apps/orchestrator/tests/domain",
    "apps/orchestrator/tests/agents",
    "apps/orchestrator/tests/observability",
    "apps/orchestrator/tests/artifacts",
    "tests/integration/test_worktree_scripts.py",
)
RECOVERY_TARGETS = (
    "tests/recovery_process/test_harness_waits.py",
    "tests/integration/test_worker_restart.py::test_worker_crash_and_restart_during_development",
)
WEB_STEPS = (
    Step("Sync locked Python dependencies", ("uv", "sync", "--frozen", "--extra", "dev"), 900.0),
    Step(
        "Verify backend OpenAPI contract",
        ("uv", "run", "--frozen", "python", "scripts/export-openapi.py"),
        900.0,
    ),
    Step("Install locked Node dependencies", ("npm", "ci"), 1800.0),
    Step("Verify generated API types", ("npm", "run", "api:check"), 600.0),
    Step("Run web unit tests", ("npm", "run", "test:web", "--", "--run"), 1800.0),
    Step("Type check the web application", ("npm", "run", "typecheck:web"), 600.0),
    Step("Lint the web application", ("npm", "run", "lint:web"), 600.0),
    Step("Build the web application", ("npm", "run", "build:web"), 1800.0),
    Step(
        "Verify tracked generated contracts unchanged",
        (
            "git",
            "diff",
            "--exit-code",
            "--",
            "apps/web/openapi.json",
            "apps/web/src/lib/api/schema.d.ts",
            "apps/web/next-env.d.ts",
        ),
        300.0,
    ),
)

SELECTIONS: dict[str, Selection] = {
    "unit": Selection(
        name="unit",
        summary=(
            "Domain, agent, observability, artifact and host-wrapper contracts "
            "on the host (no PostgreSQL)."
        ),
        container=False,
        broad=False,
        steps=(
            Step(
                "Sync locked Python dependencies",
                ("uv", "sync", "--frozen", "--extra", "dev"),
                900.0,
            ),
        ),
        pytest_step=PytestStep(
            name="Unit and host wrapper contracts",
            prefix=("uv", "run", "--frozen", "python", "-m", "pytest"),
            suffix=("-q", "-ra", "-m", HOST_MARKERS, *PYTEST_CACHE_OFF),
            targets=UNIT_TARGETS,
            timeout=1800.0,
        ),
    ),
    "web": Selection(
        name="web",
        summary="Web unit, type, lint, build and generated-contract checks on the host.",
        container=False,
        broad=False,
        steps=WEB_STEPS,
        pytest_step=None,
    ),
    "backend": Selection(
        name="backend",
        summary=(
            "Deterministic Linux backend selection against an isolated PostgreSQL "
            "instance; broad, roughly 80 minutes."
        ),
        container=True,
        broad=True,
        pytest_step=PytestStep(
            name="Deterministic backend selection",
            prefix=("python", "-m", "pytest"),
            suffix=("-q", "-ra", "-m", CONTROLLER_MARKERS, *PYTEST_CACHE_OFF),
            targets=(),
            timeout=5400.0,
        ),
    ),
    "recovery": Selection(
        name="recovery",
        summary=(
            "Worker crash/restart and harness-wait boundaries against an isolated "
            "PostgreSQL instance."
        ),
        container=True,
        broad=False,
        pytest_step=PytestStep(
            name="Focused recovery boundaries",
            prefix=("python", "-m", "pytest"),
            suffix=("-q", "-ra", "-m", CONTROLLER_MARKERS, *PYTEST_CACHE_OFF),
            targets=RECOVERY_TARGETS,
            timeout=1200.0,
        ),
    ),
}


def _validate_target(target: str) -> str:
    """Reject any focused target that could escape the mounted workspace."""
    if not target.strip():
        raise ConfigurationError("focused targets must not be empty")
    if target.startswith("-"):
        raise ConfigurationError(f"focused target {target!r} must not look like an option")
    probe = target.split("::", 1)[0]
    posix = PurePosixPath(probe)
    windows = PureWindowsPath(probe)
    if posix.is_absolute() or windows.is_absolute() or ".." in posix.parts or ".." in windows.parts:
        raise ConfigurationError(
            f"focused target {target!r} must be a repository-relative path without '..'"
        )
    return target


def build_parser() -> argparse.ArgumentParser:
    """Build the command line interface."""
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description=(
            "Run one explicit Forge verification selection locally and record its "
            "identity, commands, outcomes and cleanup."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list the available selections")
    run = subparsers.add_parser("run", help="run one selection")
    run.add_argument("selection", help="selection name, for example 'backend'")
    run.add_argument(
        "--focused",
        action="append",
        nargs="+",
        metavar="TARGET",
        help=(
            "run only these repository-relative pytest targets; repeatable. Replaces "
            "the selection's default targets."
        ),
    )
    run.add_argument(
        "--full",
        action="store_true",
        help="acknowledge a broad selection, for example the roughly 80-minute backend suite",
    )
    run.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        help="override the selection's default step timeout",
    )
    run.add_argument(
        "--image",
        metavar="REF",
        help="use an existing controller image instead of building the pinned one",
    )
    return parser


def build_request(namespace: argparse.Namespace) -> RunRequest:
    """Validate parsed arguments into one bounded request."""
    selection = SELECTIONS.get(namespace.selection)
    if selection is None:
        raise ConfigurationError(
            f"unknown selection {namespace.selection!r}; choose one of "
            f"{', '.join(sorted(SELECTIONS))}"
        )
    focused = tuple(_validate_target(item) for group in (namespace.focused or ()) for item in group)
    if focused and namespace.full:
        raise ConfigurationError("--focused and --full are mutually exclusive")
    if focused and selection.pytest_step is None:
        raise ConfigurationError(f"selection {selection.name!r} has no focusable pytest step")
    if not focused and selection.broad and not namespace.full:
        raise ConfigurationError(
            f"selection {selection.name!r} is broad; pass --focused TARGET or acknowledge "
            "it with --full"
        )
    if namespace.full and not selection.broad:
        raise ConfigurationError(f"selection {selection.name!r} is bounded; --full is not valid")
    if namespace.image and not selection.container:
        raise ConfigurationError(
            f"selection {selection.name!r} runs on the host; --image is not valid"
        )
    if namespace.timeout is not None and namespace.timeout <= 0:
        raise ConfigurationError("--timeout must be a positive number of seconds")
    return RunRequest(
        selection=selection.name,
        focused=focused,
        full=bool(namespace.full),
        timeout=namespace.timeout,
        image=namespace.image,
    )


class LocalVerificationHarness:
    """Runs bounded selections and records reproducible evidence."""

    def __init__(
        self,
        repo_root: Path | None = None,
        *,
        runner: CommandRunner | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        evidence_root: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root is not None else REPO_ROOT
        self.runner = runner or DefaultCommandRunner()
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.evidence_root = (
            Path(evidence_root)
            if evidence_root is not None
            else self.repo_root / EVIDENCE_RELATIVE_ROOT
        )
        self.stop_event = threading.Event()

    def log(self, message: str) -> None:
        print(f"[verify] {message}", file=self.stdout, flush=True)

    def _check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise SupervisorCancelled("Verification harness received a shutdown signal.")

    def run_cmd(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run one command, honouring cancellation on both sides of the call."""
        self._check_cancelled()
        try:
            result = self.runner.run(
                argv,
                cwd=cwd,
                env=env,
                timeout=timeout,
                stop_event=self.stop_event,
            )
        except TypeError:
            result = self.runner.run(argv, cwd=cwd, env=env, timeout=timeout)
        self._check_cancelled()
        return result

    def run_teardown_cmd(self, argv: list[str], *, timeout: float) -> CommandResult:
        """Run an owned-resource teardown command that must survive cancellation."""
        try:
            return self.runner.run(
                argv,
                cwd=self.repo_root,
                env=None,
                timeout=timeout,
                stop_event=None,
            )
        except TypeError:
            return self.runner.run(argv, cwd=self.repo_root, env=None, timeout=timeout)

    def _git(self, args: tuple[str, ...]) -> str:
        result = self.run_cmd(["git", *args], cwd=self.repo_root, timeout=120)
        if result.returncode != 0:
            raise SupervisorError(f"git {' '.join(args)} failed: {_first_line(result)}")
        return result.stdout

    def candidate_identity(self) -> dict[str, Any]:
        """Record the candidate commit plus the aggregate working-tree input state."""
        tracked = self._git(("ls-files", "-s"))
        worktree = self._git(("status", "--porcelain=v1", "-z", "--untracked-files=all"))
        return {
            "head": self._git(("rev-parse", "HEAD")).strip(),
            "branch": self._git(("rev-parse", "--abbrev-ref", "HEAD")).strip(),
            "dirty": bool(worktree.strip("\0").strip()),
            "tracked_tree_digest": _digest(tracked),
            "worktree_digest": _digest(worktree),
            "tracked_file_count": len([line for line in tracked.splitlines() if line.strip()]),
        }

    def child_environment(self) -> tuple[dict[str, str], list[str]]:
        """Return a credential-filtered environment and the names removed from it."""
        child = {
            name: value
            for name, value in os.environ.items()
            if not is_reserved_environment_name(name)
        }
        filtered = sorted(set(os.environ) - set(child))
        return child, filtered

    def _required_version(self, argv: list[str], label: str) -> str:
        result = self.run_cmd(argv, timeout=120)
        if result.returncode != 0:
            raise PrerequisiteError(f"{label} is not available: {_first_line(result)}")
        return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""

    def host_toolchain(self, *, require_docker: bool) -> dict[str, Any]:
        """Verify the required host toolchain for a selection."""
        if sys.version_info[:2] != (3, 14):
            raise PrerequisiteError(
                f"Python 3.14 is required. Found Python {sys.version_info[0]}.{sys.version_info[1]}."
            )
        toolchain: dict[str, Any] = {
            "platform": sys.platform,
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "node": self._required_version(["node", "--version"], "Node.js"),
            "npm": self._required_version(["npm", "--version"], "npm"),
            "uv": self._required_version(["uv", "--version"], "uv"),
        }
        if not toolchain["node"].startswith("v24."):
            raise PrerequisiteError(f"Node 24 is required. Found Node {toolchain['node']}.")
        if require_docker:
            toolchain["docker_client"] = self._required_version(
                ["docker", "--version"], "the Docker CLI"
            )
            server = self.run_cmd(
                [
                    "docker",
                    "version",
                    "--format",
                    "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}",
                ],
                timeout=60,
            )
            if server.returncode != 0:
                raise PrerequisiteError(
                    f"the Docker daemon is not available: {_first_line(server)}"
                )
            toolchain["docker_server"] = server.stdout.strip()
            if "linux" not in toolchain["docker_server"]:
                raise PrerequisiteError(
                    "container selections require a Linux Docker engine; found "
                    f"{toolchain['docker_server']}"
                )
        return toolchain

    def image_inputs_digest(self) -> str:
        """Digest the exact build inputs that produce the controller image."""
        payload = "".join(
            f"{name}\0{(self.repo_root / name).read_text(encoding='utf-8')}\0"
            for name in CONTROLLER_IMAGE_INPUTS
        )
        return _digest(payload)

    def resolve_image_id(self, reference: str) -> str:
        """Return an immutable image ID, or an empty string when it is absent."""
        result = self.run_cmd(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            cwd=self.repo_root,
            timeout=120,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def ensure_controller_image(self, override: str | None) -> dict[str, Any]:
        """Resolve or build the controller image and bind it to its build inputs."""
        if override:
            image_id = self.resolve_image_id(override)
            if not _IMMUTABLE_IMAGE_PATTERN.match(image_id):
                raise PrerequisiteError(
                    f"--image {override!r} did not resolve to an immutable image ID"
                )
            return {"reference": override, "id": image_id, "built": False, "inputs_digest": None}

        inputs_digest = self.image_inputs_digest()
        reference = f"{CONTROLLER_IMAGE_REPOSITORY}:{inputs_digest[:16]}"
        image_id = self.resolve_image_id(reference)
        built = False
        if not _IMMUTABLE_IMAGE_PATTERN.match(image_id):
            self.log(f"building controller image {reference} from Dockerfile.verify...")
            build = self.run_cmd(
                [
                    "docker",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "-f",
                    "Dockerfile.verify",
                    "-t",
                    reference,
                    ".",
                ],
                cwd=self.repo_root,
                timeout=3600,
            )
            if build.returncode != 0:
                raise SupervisorError(f"controller image build failed: {_first_line(build)}")
            image_id = self.resolve_image_id(reference)
            built = True
        if not _IMMUTABLE_IMAGE_PATTERN.match(image_id):
            raise SupervisorError(
                "controller image inspection must return an immutable sha256 image ID, got "
                f"{image_id or '<empty>'}"
            )
        return {
            "reference": reference,
            "id": image_id,
            "built": built,
            "inputs_digest": inputs_digest,
        }

    def container_prefix(self, run_id: str) -> str:
        return f"forge-verify-{slugify(run_id)}"

    def start_postgres(self, run_id: str) -> tuple[dict[str, Any], list[str]]:
        """Start the ephemeral PostgreSQL instance the controller shares a namespace with."""
        name = f"{self.container_prefix(run_id)}-postgres"
        argv = [
            "docker",
            "run",
            "--detach",
            "--init",
            "--name",
            name,
            "--network",
            "none",
            "--env",
            f"POSTGRES_DB={POSTGRES_DATABASE}",
            "--env",
            f"POSTGRES_USER={POSTGRES_USER}",
            "--env",
            f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
            POSTGRES_IMAGE,
            "postgres",
            "-c",
            f"port={POSTGRES_PORT}",
        ]
        result = self.run_cmd(argv, cwd=self.repo_root, timeout=300)
        if result.returncode != 0:
            raise SupervisorError(f"PostgreSQL container failed to start: {_first_line(result)}")
        return {"name": name, "container_id": result.stdout.strip()[:12]}, argv

    def wait_for_postgres(
        self, name: str, *, timeout: float = POSTGRES_READY_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        """Poll readiness with a bounded, recorded number of attempts."""
        started = time.monotonic()
        deadline = started + timeout
        attempts = 0
        while True:
            self._check_cancelled()
            attempts += 1
            ready = self.run_cmd(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-U",
                    POSTGRES_USER,
                    "-d",
                    POSTGRES_DATABASE,
                    "-p",
                    str(POSTGRES_PORT),
                    "-q",
                ],
                cwd=self.repo_root,
                timeout=60,
            )
            if ready.returncode == 0:
                return {
                    "attempts": attempts,
                    "seconds": round(time.monotonic() - started, 3),
                }
            if time.monotonic() >= deadline:
                raise SupervisorError(
                    f"PostgreSQL was not ready after {attempts} checks in {timeout:.0f}s"
                )
            time.sleep(1.0)

    def controller_argv(
        self,
        controller_name: str,
        postgres_name: str,
        image_id: str,
        argv: list[str],
    ) -> list[str]:
        """Build the controller container invocation for one command."""
        return [
            "docker",
            "run",
            "--init",
            "--name",
            controller_name,
            "--network",
            f"container:{postgres_name}",
            "--user",
            CONTROLLER_USER,
            "--volume",
            f"{self.repo_root}:{CONTROLLER_MOUNT_TARGET}",
            "--workdir",
            CONTROLLER_MOUNT_TARGET,
            "--env",
            f"PYTHONPATH={CONTROLLER_PYTHONPATH}",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "NEXT_TELEMETRY_DISABLED=1",
            image_id,
            *argv,
        ]

    def _container_evidence_path(self, evidence_dir: Path) -> str | None:
        """Map an evidence directory into the controller mount, if it is inside it."""
        try:
            relative = evidence_dir.resolve().relative_to(self.repo_root.resolve())
        except ValueError:
            return None
        return f"{CONTROLLER_MOUNT_TARGET}/{relative.as_posix()}"

    def _record_controller_identity(
        self,
        record: dict[str, Any],
        *,
        controller_name: str,
        postgres_name: str,
        image_id: str,
        log_dir: Path,
    ) -> dict[str, Any]:
        argv = self.controller_argv(
            controller_name,
            postgres_name,
            image_id,
            ["python3", "-c", CONTROLLER_PROBE_CODE],
        )
        result = self.execute_step(
            record,
            log_dir=log_dir,
            name="Controller identity probe",
            argv=argv,
            cwd=self.repo_root,
            env=None,
            timeout=300.0,
            container={"image": image_id, "user": CONTROLLER_USER, "init": True},
        )
        if result.returncode != 0:
            raise SupervisorError(f"controller identity probe failed: {_first_line(result)}")
        values: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip()
        uid = int(values.get("uid", "-1"))
        gid = int(values.get("gid", "-1"))
        init_process = values.get("init", "")
        python = values.get("python", "")
        if uid != CONTROLLER_UID or gid != CONTROLLER_UID:
            raise SupervisorError(
                f"controller must run as UID/GID {CONTROLLER_UID}, observed {uid}/{gid}"
            )
        if uid == SANDBOX_UID:
            raise SupervisorError(f"controller identity must differ from sandbox UID {SANDBOX_UID}")
        if init_process not in CONTROLLER_INIT_PROCESS_NAMES:
            raise SupervisorError(
                "controller must start an init reaper as PID 1, observed "
                f"{init_process or '<empty>'!r}"
            )
        if not python.startswith("3.14."):
            raise SupervisorError(
                f"controller must run Python 3.14, observed {python or '<empty>'}"
            )
        if values.get("uv_lock_present") != "True":
            raise SupervisorError("controller must see the repository uv.lock at /workspace")
        return {
            "uid": uid,
            "gid": gid,
            "sandbox_uid": SANDBOX_UID,
            "distinct_from_sandbox": uid != SANDBOX_UID,
            "init_process": init_process,
            "python": python,
            "pytest": values.get("pytest", ""),
        }

    def cleanup(self, run_id: str) -> dict[str, Any]:
        """Remove every owned container and verify that none survived.

        Names are run-scoped and re-derived here rather than taken from earlier
        steps, so a run that failed before starting a container still reclaims
        anything that exists, including containers created by a step this
        harness version no longer expects.
        """
        prefix = self.container_prefix(run_id)
        removed: list[str] = []
        errors: list[str] = []
        listed = self.run_teardown_cmd(
            ["docker", "ps", "--all", "--format", "{{.Names}}"], timeout=120
        )
        if listed.returncode != 0:
            errors.append(f"docker ps failed: {_first_line(listed)}")
            present: list[str] = []
        else:
            present = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        owned = sorted(
            {
                f"{prefix}-controller",
                f"{prefix}-controller-probe",
                f"{prefix}-postgres",
                *(name for name in present if name.startswith(prefix)),
            }
        )
        removals: list[list[str]] = []
        for name in owned:
            if not _CONTROLLER_NAME_PATTERN.match(name):
                errors.append(f"refusing to remove unexpected container name {name!r}")
                continue
            argv = ["docker", "rm", "--force", "--volumes", name]
            removals.append(argv)
            result = self.run_teardown_cmd(argv, timeout=300)
            if result.returncode == 0:
                removed.append(name)
            elif "No such container" not in f"{result.stderr}{result.stdout}":
                errors.append(f"docker rm {name} failed: {_first_line(result)}")
        verify_argv = ["docker", "ps", "--all", "--format", "{{.Names}}"]
        verified = self.run_teardown_cmd(verify_argv, timeout=120)
        if verified.returncode != 0:
            errors.append(f"docker ps failed after removal: {_first_line(verified)}")
            names: list[str] = []
        else:
            names = [line.strip() for line in verified.stdout.splitlines() if line.strip()]
        leftovers = sorted(name for name in names if name.startswith(prefix))
        return {
            "attempted": True,
            "containers_removed": removed,
            "leftovers": leftovers,
            "errors": errors,
            "removal_argv": removals,
            "verification_argv": verify_argv,
            "verified_absent": not leftovers and not errors,
        }

    def execute_step(
        self,
        record: dict[str, Any],
        *,
        log_dir: Path,
        name: str,
        argv: list[str],
        cwd: Path | None,
        env: dict[str, str] | None,
        timeout: float,
        container: dict[str, Any] | None = None,
    ) -> CommandResult:
        """Run one recorded step, retaining raw output as evidence."""
        index = len(record["steps"]) + 1
        stem = f"{index:02d}-{slugify(name)}"
        stdout_path = log_dir / f"{stem}.stdout.log"
        stderr_path = log_dir / f"{stem}.stderr.log"
        self.log(f"{name} (timeout {timeout:.0f}s)")
        started = time.monotonic()
        result = self.run_cmd(argv, cwd=cwd, env=env, timeout=timeout)
        duration = round(time.monotonic() - started, 3)
        stdout_path.write_text(result.stdout, encoding="utf-8", errors="replace")
        stderr_path.write_text(result.stderr, encoding="utf-8", errors="replace")
        step: dict[str, Any] = {
            "name": name,
            "argv": list(argv),
            "exit_code": result.returncode,
            "duration_seconds": duration,
            "timeout_seconds": timeout,
            "stdout_log": f"logs/{stdout_path.name}",
            "stderr_log": f"logs/{stderr_path.name}",
        }
        if container is not None:
            step["container"] = container
        record["steps"].append(step)
        self.log(f"{name}: exit {result.returncode} in {duration:.1f}s")
        return result

    def run_selection(self, request: RunRequest) -> int:
        """Run one validated selection and always write its evidence record."""
        selection = SELECTIONS[request.selection]
        started = _utc_now()
        run_id = f"{started:%Y%m%dT%H%M%SZ}-{selection.name}-{uuid.uuid4().hex[:8]}".lower()
        evidence_dir = self.evidence_root / run_id
        log_dir = evidence_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        targets = request.focused or (
            selection.pytest_step.targets if selection.pytest_step else ()
        )
        record: dict[str, Any] = {
            "schema": "forge.local-verification/1",
            "run": {
                "id": run_id,
                "selection": selection.name,
                "summary": selection.summary,
                "mode": "focused" if request.focused else "full",
                "targets": list(targets),
                "started_at": _timestamp(started),
                "evidence_dir": str(evidence_dir),
            },
            "policy": {
                "live_providers": False,
                "github_actions": False,
                "automatic_retries": False,
                "full_suite_expansion": False,
                "host_environment_filtered": True,
                "container_environment_inherited": False,
                "redacted_environment_names": [],
            },
            "steps": [],
            "cleanup": {
                "attempted": False,
                "containers_removed": [],
                "leftovers": [],
                "errors": [],
                "verified_absent": False,
            },
        }
        outcome = "environment_error"
        exit_code = EXIT_ENVIRONMENT
        controller_name = f"{self.container_prefix(run_id)}-controller"
        try:
            child_env, filtered_names = self.child_environment()
            record["policy"]["redacted_environment_names"] = filtered_names
            record["candidate"] = self.candidate_identity()
            record["environment"] = {
                "host": self.host_toolchain(require_docker=selection.container)
            }

            if selection.container:
                assert selection.pytest_step is not None
                image = self.ensure_controller_image(request.image)
                record["environment"]["controller_image"] = image
                postgres, postgres_argv = self.start_postgres(run_id)
                record["environment"]["postgres"] = {
                    "image": POSTGRES_IMAGE,
                    "image_id": self.resolve_image_id(POSTGRES_IMAGE),
                    "argv": postgres_argv,
                    "name": postgres["name"],
                    "container_id": postgres["container_id"],
                    "network": "none",
                    "endpoint": f"127.0.0.1:{POSTGRES_PORT}",
                    "database": POSTGRES_DATABASE,
                }
                record["environment"]["postgres"]["readiness"] = self.wait_for_postgres(
                    postgres["name"]
                )
                record["environment"]["controller"] = self._record_controller_identity(
                    record,
                    # The probe keeps its own name: each controller step is an
                    # explicit, separately reclaimed container.
                    controller_name=f"{self.container_prefix(run_id)}-controller-probe",
                    postgres_name=postgres["name"],
                    image_id=image["id"],
                    log_dir=log_dir,
                )
                step = selection.pytest_step
                junit_name = f"junit-{slugify(step.name)}.xml"
                pytest_argv = [*step.prefix, *targets, *step.suffix]
                # JUnit output is written through the mounted workspace so the host
                # keeps the structured per-test outcomes next to the raw logs.
                container_evidence = self._container_evidence_path(evidence_dir)
                if container_evidence is not None:
                    pytest_argv.append(f"--junitxml={container_evidence}/{junit_name}")
                timeout = request.timeout or step.timeout
                result = self.execute_step(
                    record,
                    log_dir=log_dir,
                    name=step.name,
                    argv=self.controller_argv(
                        controller_name, postgres["name"], image["id"], pytest_argv
                    ),
                    cwd=self.repo_root,
                    env=None,
                    timeout=timeout,
                    container={
                        "image": image["id"],
                        "user": CONTROLLER_USER,
                        "init": True,
                        "network": f"container:{postgres['name']}",
                        "mount": f"{self.repo_root}:{CONTROLLER_MOUNT_TARGET}",
                    },
                )
                record["run"]["pytest_argv"] = pytest_argv
                record["run"]["junit_artifact"] = (
                    junit_name if container_evidence is not None else None
                )
                if result.returncode == PYTEST_NO_TESTS_COLLECTED:
                    raise ConfigurationError(
                        "the requested selection matched no tests; check the focused targets"
                    )
                if result.returncode == 0:
                    outcome, exit_code = "passed", EXIT_PASSED
                else:
                    outcome, exit_code = "failed", EXIT_FAILED
            else:
                failed = False
                for step in selection.steps:
                    result = self.execute_step(
                        record,
                        log_dir=log_dir,
                        name=step.name,
                        argv=list(step.argv),
                        cwd=self.repo_root,
                        env=child_env,
                        timeout=request.timeout or step.timeout,
                    )
                    if result.returncode != 0:
                        failed = True
                        break
                if not failed and selection.pytest_step is not None:
                    step = selection.pytest_step
                    junit_name = f"junit-{slugify(step.name)}.xml"
                    pytest_argv = [
                        *step.prefix,
                        *targets,
                        *step.suffix,
                        f"--junitxml={evidence_dir / junit_name}",
                    ]
                    result = self.execute_step(
                        record,
                        log_dir=log_dir,
                        name=step.name,
                        argv=pytest_argv,
                        cwd=self.repo_root,
                        env=child_env,
                        timeout=request.timeout or step.timeout,
                    )
                    record["run"]["pytest_argv"] = pytest_argv
                    record["run"]["junit_artifact"] = junit_name
                    failed = result.returncode != 0
                outcome, exit_code = ("failed", EXIT_FAILED) if failed else ("passed", EXIT_PASSED)
        except ConfigurationError as error:
            outcome, exit_code = "configuration_error", EXIT_CONFIGURATION
            record["error"] = {"kind": "configuration", "message": str(error)}
            print(f"[verify] {error}", file=self.stderr, flush=True)
        except SupervisorCancelled as error:
            outcome, exit_code = "cancelled", EXIT_CANCELLED
            record["error"] = {"kind": "cancelled", "message": str(error)}
            print(f"[verify] cancelled: {error}", file=self.stderr, flush=True)
        except PrerequisiteError as error:
            outcome, exit_code = "environment_error", EXIT_ENVIRONMENT
            record["error"] = {"kind": "prerequisite", "message": str(error)}
            print(f"[verify] prerequisite error: {error}", file=self.stderr, flush=True)
        except SupervisorError as error:
            outcome, exit_code = "environment_error", EXIT_ENVIRONMENT
            record["error"] = {"kind": "environment", "message": str(error)}
            print(f"[verify] harness error: {error}", file=self.stderr, flush=True)
        finally:
            if selection.container:
                cleanup = self.cleanup(run_id)
                record["cleanup"] = cleanup
                if not cleanup["verified_absent"]:
                    exit_code = EXIT_ENVIRONMENT
                    outcome = "cleanup_failed"
                    print(
                        "[verify] owned containers survived cleanup: "
                        f"{cleanup['leftovers'] or cleanup['errors']}",
                        file=self.stderr,
                        flush=True,
                    )
            if self.stop_event.is_set() and outcome != "cancelled":
                outcome, exit_code = "cancelled", EXIT_CANCELLED
            finished = _utc_now()
            record["run"]["finished_at"] = _timestamp(finished)
            record["run"]["duration_seconds"] = round((finished - started).total_seconds(), 3)
            record["run"]["outcome"] = outcome
            record["run"]["exit_code"] = exit_code
            (evidence_dir / "run.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
        self.log(
            f"{outcome} (exit {exit_code}); candidate {record.get('candidate', {}).get('head', '?')[:12]}; "
            f"evidence {evidence_dir}"
        )
        return exit_code


def print_selections(stream: TextIO) -> None:
    """Print the selection catalogue deterministically."""
    for name in sorted(SELECTIONS):
        selection = SELECTIONS[name]
        kind = "container" if selection.container else "host"
        scope = "broad" if selection.broad else "bounded"
        targets = selection.pytest_step.targets if selection.pytest_step else ()
        print(f"{name} [{kind}, {scope}]", file=stream)
        print(f"  {selection.summary}", file=stream)
        if targets:
            print(f"  targets: {' '.join(targets)}", file=stream)
        elif selection.pytest_step is not None:
            print("  targets: whole configured testpaths (acknowledge with --full)", file=stream)
        for step in selection.steps:
            print(f"  step: {step.name}", file=stream)


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        print_selections(sys.stdout)
        return EXIT_PASSED

    harness = LocalVerificationHarness()

    def handle_signal(signum: int, frame: Any) -> None:
        del signum, frame
        harness.stop_event.set()

    with suppress(ValueError):
        signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        with suppress(ValueError):
            signal.signal(signal.SIGTERM, handle_signal)

    try:
        request = build_request(args)
    except ConfigurationError as error:
        print(f"[verify] configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    return harness.run_selection(request)


if __name__ == "__main__":
    sys.exit(main())
