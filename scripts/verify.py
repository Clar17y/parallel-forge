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
from collections.abc import Callable
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
    IMMUTABLE_IMAGE_DIGEST_PATTERN,
    CommandResult,
    CommandRunner,
    ConfigurationError,
    DefaultCommandRunner,
    PrerequisiteError,
    SupervisorCancelled,
    SupervisorError,
    redact_secrets,
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
# Pinned by repository digest: the harness claims reproducibility, so the
# database the tests actually run against must not float with a tag. Refresh
# with `docker image inspect postgres:17 --format '{{index .RepoDigests 0}}'`.
POSTGRES_IMAGE = "postgres@sha256:67f41722b7a8cbdb868a44a4995c846eddfdc2973bccb291ce937dce88ad5675"
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
# DefaultCommandRunner overloads returncode with non-exit sentinels: -1 timeout
# or launch failure, -2 cancellation, 127 launch failure. They must never be
# recorded as a test outcome, because nothing is known about the tests then.
RUNNER_TIMEOUT_RETURN_CODE = -1
RUNNER_CANCELLED_RETURN_CODE = -2
RUNNER_LAUNCH_FAILURE_RETURN_CODE = 127
# `docker run` reports its own failures on these codes; only 125/126 are
# unambiguous here because this harness always requests a pytest entry point.
DOCKER_RUN_FAILURE_RETURN_CODES = frozenset({125, 126})

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
    "print('dotenv_empty=%s' % (not os.path.exists('/workspace/.env') or os.path.getsize('/workspace/.env') == 0))\n"
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
                return redact_secrets(line.strip())
    return f"exit code {result.returncode}"


def slugify(value: str) -> str:
    """Return a filesystem- and container-name-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def classify_runner_exit(returncode: int) -> str | None:
    """Name a runner sentinel, or return None for a real process exit code."""
    if returncode == RUNNER_TIMEOUT_RETURN_CODE:
        return "timed_out"
    if returncode == RUNNER_CANCELLED_RETURN_CODE:
        return "cancelled"
    if returncode == RUNNER_LAUNCH_FAILURE_RETURN_CODE:
        return "launch_failed"
    return None


def parse_probe_integer(values: dict[str, Any], key: str) -> int:
    """Read one integer probe field, failing closed on malformed output."""
    raw = values.get(key)
    try:
        return int(str(raw))
    except TypeError, ValueError:
        raise SupervisorError(
            f"controller identity probe returned a malformed {key}: {raw!r}"
        ) from None


def is_reserved_environment_name(name: str) -> bool:
    """Return True when a host variable must not reach a verification child."""
    if name in _RESERVED_ENVIRONMENT_NAMES:
        return True
    if name.startswith(_ALLOWED_ENVIRONMENT_PREFIXES):
        return False
    if name.startswith(_RESERVED_ENVIRONMENT_PREFIXES):
        return True
    return bool(_SECRET_ENVIRONMENT_PATTERN.search(name))


def is_root_dotenv_secret(name: str) -> bool:
    """Return True when a repository-root entry name is .env or a secret variant.

    Excludes repository-root .env and other root .env.* secret variants from
    projection into controller containers, while preserving the tracked
    .env.example fixture.
    """
    if name == ".env":
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    return bool(name.startswith((".env-", ".env_")))


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
    # Container selections run inside an image that ships its own Node, so only the
    # host selections that actually invoke Node require it on this machine.
    requires_node: bool = True
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
        requires_node=False,
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
        requires_node=False,
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
    """Reject any focused target that could escape or silently widen the selection."""
    if not target.strip():
        raise ConfigurationError("focused targets must not be empty")
    if target.startswith("-"):
        raise ConfigurationError(f"focused target {target!r} must not look like an option")
    probe = target.split("::", 1)[0]
    posix = PurePosixPath(probe)
    windows = PureWindowsPath(probe)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        # A drive-relative Windows path (`\Windows\x`) is not "absolute" but still
        # resolves outside the repository.
        or windows.root
        or windows.drive
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise ConfigurationError(
            f"focused target {target!r} must be a repository-relative path without '..'"
        )
    if not [part for part in posix.parts if part not in {".", ""}]:
        raise ConfigurationError(
            f"focused target {target!r} must name a path inside the repository; "
            "a repository-root target would run the whole selection"
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
        help="override the selection's pytest step timeout; not valid without one",
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
    # A focused run replaces targets, never the environment the targets need, so a
    # selection that declares both a container and fixed setup steps is invalid
    # rather than silently running the tests against an unprepared controller.
    if selection.container and selection.steps:
        raise ConfigurationError(
            f"selection {selection.name!r} combines a controller container with fixed setup "
            "steps, which this harness does not execute"
        )
    # The override tunes the pytest step; applying it to unrelated fixed steps (for
    # example a production web build) turns a legitimate run into a timeout, and it
    # means nothing for selections that have no pytest step at all.
    if namespace.timeout is not None and selection.pytest_step is None:
        raise ConfigurationError(
            f"selection {selection.name!r} has no pytest step; --timeout is not valid"
        )
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
        result = self.runner.run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            stop_event=self.stop_event,
        )
        # Cancellation is checked before the next command and by the recorded step
        # that observes the runner's cancelled sentinel. Re-checking here would
        # discard a completed result that the run record must keep.
        return result

    def run_teardown_cmd(self, argv: list[str], *, timeout: float) -> CommandResult:
        """Run an owned-resource teardown command that must survive cancellation."""
        return self.runner.run(
            argv,
            cwd=self.repo_root,
            env=None,
            timeout=timeout,
            stop_event=None,
        )

    def _git(self, args: tuple[str, ...]) -> str:
        result = self.run_cmd(["git", *args], cwd=self.repo_root, timeout=120)
        if result.returncode != 0:
            raise SupervisorError(f"git {' '.join(args)} failed: {_first_line(result)}")
        return result.stdout

    def _file_content_digest(self, relative_path: str, *, status: str = "") -> str:
        """Hash one working-tree path's raw bytes safely without persisting contents."""
        target = self.repo_root / relative_path
        if not target.is_symlink() and not target.exists():
            if "D" in status:
                return "missing"
            raise SupervisorError(
                f"working-tree path {relative_path!r} is missing, raced away, or unreadable"
            )
        if target.is_symlink():
            try:
                dest = os.readlink(target)
                return f"symlink:{dest}"
            except OSError as error:
                raise SupervisorError(f"could not read symlink {relative_path}: {error}") from None
        if target.is_dir():
            return "directory"
        hasher = hashlib.sha256()
        try:
            with target.open("rb") as handle:
                while chunk := handle.read(65536):
                    hasher.update(chunk)
        except OSError as error:
            raise SupervisorError(
                f"could not read working-tree file {relative_path}: {error}"
            ) from None
        return hasher.hexdigest()

    def _parse_worktree_status(self, raw_status: str) -> list[tuple[str, str, str | None]]:
        """Parse git status --porcelain=v1 -z entries safely."""
        tokens = raw_status.split("\0")
        entries: list[tuple[str, str, str | None]] = []
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if not token:
                idx += 1
                continue
            if len(token) < 4 or token[2] != " ":
                raise SupervisorError(f"malformed git status entry: {token!r}")
            status = token[:2]
            path = token[3:]
            orig_path: str | None = None
            if "R" in status or "C" in status:
                idx += 1
                if idx < len(tokens):
                    orig_path = tokens[idx]
            entries.append((status, path, orig_path))
            idx += 1
        return entries

    def _compute_worktree_digest(self, raw_status: str) -> str:
        """Digest working-tree status and actual file bytes without leaking secrets."""
        entries = self._parse_worktree_status(raw_status)
        if not entries:
            return _digest("")
        entries.sort(key=lambda item: item[1])
        hasher = hashlib.sha256()
        for status, path, orig_path in entries:
            if re.search(r"\\x[0-9a-fA-F]{2}", path) or (
                orig_path and re.search(r"\\x[0-9a-fA-F]{2}", orig_path)
            ):
                raise SupervisorError(
                    f"working-tree path {path!r} contains decode-mangled bytes; "
                    "refusing to compute candidate identity"
                )
            file_digest = self._file_content_digest(path, status=status)
            hasher.update(status.encode("utf-8", errors="surrogateescape"))
            hasher.update(b"\0")
            hasher.update(path.encode("utf-8", errors="surrogateescape"))
            hasher.update(b"\0")
            hasher.update((orig_path or "").encode("utf-8", errors="surrogateescape"))
            hasher.update(b"\0")
            hasher.update(file_digest.encode("utf-8"))
            hasher.update(b"\0")
        return hasher.hexdigest()

    def candidate_identity(self) -> dict[str, Any]:
        """Record the candidate commit plus the aggregate working-tree input state."""
        tracked = self._git(("ls-files", "-s"))
        worktree = self._git(("status", "--porcelain=v1", "-z", "--untracked-files=all"))
        return {
            "head": self._git(("rev-parse", "HEAD")).strip(),
            "branch": self._git(("rev-parse", "--abbrev-ref", "HEAD")).strip(),
            "dirty": bool(worktree.strip("\0").strip()),
            "tracked_tree_digest": _digest(tracked),
            "worktree_digest": self._compute_worktree_digest(worktree),
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

    def host_toolchain(self, *, require_docker: bool, require_node: bool) -> dict[str, Any]:
        """Verify the required host toolchain for a selection."""
        if sys.version_info[:2] != (3, 14):
            raise PrerequisiteError(
                f"Python 3.14 is required. Found Python {sys.version_info[0]}.{sys.version_info[1]}."
            )
        toolchain: dict[str, Any] = {
            "platform": sys.platform,
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "uv": self._required_version(["uv", "--version"], "uv"),
            "node": None,
            "npm": None,
        }
        if require_node:
            toolchain["node"] = self._required_version(["node", "--version"], "Node.js")
            if not toolchain["node"].startswith("v24."):
                raise PrerequisiteError(f"Node 24 is required. Found Node {toolchain['node']}.")
            toolchain["npm"] = self._required_version(["npm", "--version"], "npm")
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
        parts: list[str] = []
        for name in CONTROLLER_IMAGE_INPUTS:
            try:
                content = (self.repo_root / name).read_text(encoding="utf-8")
            except OSError as error:
                raise PrerequisiteError(
                    f"controller image input {name} is unreadable: {error}"
                ) from None
            parts.append(f"{name}\0{content}\0")
        return _digest("".join(parts))

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
            if not IMMUTABLE_IMAGE_DIGEST_PATTERN.match(image_id):
                raise PrerequisiteError(
                    f"--image {override!r} did not resolve to an immutable image ID"
                )
            return {"reference": override, "id": image_id, "built": False, "inputs_digest": None}

        inputs_digest = self.image_inputs_digest()
        reference = f"{CONTROLLER_IMAGE_REPOSITORY}:{inputs_digest[:16]}"
        image_id = self.resolve_image_id(reference)
        built = False
        if not IMMUTABLE_IMAGE_DIGEST_PATTERN.match(image_id):
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
        if not IMMUTABLE_IMAGE_DIGEST_PATTERN.match(image_id):
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
        # The executed argv keeps the fixture password; the recorded one does not,
        # so the record never publishes a value its own environment filter reserves.
        recorded_argv = [
            argument.replace(f"PASSWORD={POSTGRES_PASSWORD}", "PASSWORD=[REDACTED]")
            for argument in argv
        ]
        return {"name": name, "container_id": result.stdout.strip()[:12]}, recorded_argv

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

    def empty_dotenv_path(self) -> Path:
        """Return the path to a guaranteed empty regular file used to mask .env in containers."""
        path = self.evidence_root / ".empty-dotenv"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise SupervisorError(f"dotenv mask at {path} is a symlink; refusing to mount it")
        if path.exists() and not path.is_file():
            raise SupervisorError(
                f"dotenv mask at {path} is not a regular file; refusing to mount it"
            )
        try:
            path.write_bytes(b"")
        except OSError as error:
            raise SupervisorError(f"could not create empty dotenv mask: {error}") from None
        if path.is_symlink() or not path.is_file() or path.stat().st_size != 0:
            raise SupervisorError(f"dotenv mask at {path} is not a zero-byte regular file")
        return path

    def workspace_projection(self) -> list[tuple[Path, str]]:
        """Build a deterministic workspace projection from Git candidate inputs.

        Binds existing top-level candidate entries to /workspace/<name> paths so
        /workspace itself remains container-owned, ensuring a nested file mask at
        /workspace/.env cannot create or alter a host repo_root/.env.
        """
        tracked_raw = self._git(("ls-files", "-z"))
        status_raw = self._git(("status", "--porcelain=v1", "-z", "--untracked-files=all"))

        tracked_paths = [p for p in tracked_raw.split("\0") if p]
        status_entries = self._parse_worktree_status(status_raw)

        for path in tracked_paths:
            if re.search(r"\\x[0-9a-fA-F]{2}", path):
                raise SupervisorError(
                    f"tracked path {path!r} contains decode-mangled bytes; "
                    "refusing to build workspace projection"
                )
        for status, path, orig_path in status_entries:
            if re.search(r"\\x[0-9a-fA-F]{2}", path) or (
                orig_path and re.search(r"\\x[0-9a-fA-F]{2}", orig_path)
            ):
                raise SupervisorError(
                    f"working-tree path {path!r} contains decode-mangled bytes; "
                    "refusing to build workspace projection"
                )

        deleted_paths: set[str] = set()
        working_paths: list[str] = []
        for status, path, orig_path in status_entries:
            if "D" in status:
                deleted_paths.add(path)
                if orig_path:
                    deleted_paths.add(orig_path)
            if status == "??" or status[1] in ("R", "C"):
                working_paths.append(path)
            if status[1] == "R" and orig_path:
                deleted_paths.add(orig_path)

        all_candidate_paths = sorted(set(tracked_paths) | set(working_paths))

        for path in all_candidate_paths:
            posix_path = PurePosixPath(path.replace("\\", "/"))
            if posix_path.is_absolute() or ".." in posix_path.parts:
                raise SupervisorError(
                    f"candidate path {path!r} is absolute or escapes repository root; "
                    "refusing to build workspace projection"
                )
            win_path = PureWindowsPath(path)
            if win_path.is_absolute() or win_path.drive:
                raise SupervisorError(
                    f"candidate path {path!r} is absolute or specifies a drive; "
                    "refusing to build workspace projection"
                )

            host_file = self.repo_root / path
            if not host_file.exists() and not host_file.is_symlink() and path not in deleted_paths:
                raise SupervisorError(
                    f"candidate source entry {path!r} is missing from the working tree and not explained by deletion"
                )

        top_candidates: set[str] = set()
        for path in all_candidate_paths:
            posix_path = PurePosixPath(path.replace("\\", "/"))
            if not posix_path.parts:
                continue
            top_candidates.add(posix_path.parts[0])

        projected: list[tuple[Path, str]] = []
        for top_name in sorted(top_candidates):
            if top_name in (".git", ".llm-output"):
                continue
            if is_root_dotenv_secret(top_name):
                continue

            host_entry = self.repo_root / top_name
            if not host_entry.exists() and not host_entry.is_symlink():
                # Top-level entry does not exist on disk because all entries under it were deleted
                continue

            if host_entry.is_symlink():
                try:
                    resolved = host_entry.resolve()
                except (OSError, RuntimeError) as error:
                    raise SupervisorError(
                        f"top-level symlink {top_name!r} could not be resolved: {error}"
                    ) from None

                try:
                    relative_resolved = resolved.relative_to(self.repo_root.resolve())
                except ValueError:
                    raise SupervisorError(
                        f"top-level symlink {top_name!r} resolves outside repository root to {resolved}"
                    ) from None

                if not relative_resolved.parts:
                    raise SupervisorError(
                        f"top-level symlink {top_name!r} resolves to the repository root"
                    )
                rel_parts = PurePosixPath(relative_resolved.as_posix()).parts
                if rel_parts:
                    target_top = rel_parts[0]
                    if (
                        target_top in (".git", ".llm-output")
                        or is_root_dotenv_secret(target_top)
                        or target_top not in top_candidates
                    ):
                        raise SupervisorError(
                            f"top-level symlink {top_name!r} resolves outside candidate entries to {relative_resolved}"
                        )

                if not resolved.exists():
                    raise SupervisorError(
                        f"top-level symlink {top_name!r} points to non-existent target {resolved}"
                    )
                mount_source = resolved
            else:
                mount_source = host_entry.resolve()

            if not (mount_source.is_dir() or mount_source.is_file()):
                raise SupervisorError(
                    f"top-level candidate entry {top_name!r} is not a regular file or directory"
                )
            container_mount_path = f"{CONTROLLER_MOUNT_TARGET}/{top_name}"
            projected.append((mount_source, container_mount_path))

        return projected

    def controller_argv(
        self,
        controller_name: str,
        postgres_name: str,
        image_id: str,
        argv: list[str],
        *,
        projection: list[tuple[Path, str]] | None = None,
    ) -> list[str]:
        """Build the controller container invocation for one command."""
        empty_dotenv = self.empty_dotenv_path()
        if (
            empty_dotenv.is_symlink()
            or not empty_dotenv.is_file()
            or empty_dotenv.stat().st_size != 0
        ):
            raise SupervisorError(
                f"dotenv mask at {empty_dotenv} is not a zero-byte regular file; refusing to mount it"
            )
        empty_dotenv_resolved = empty_dotenv.resolve()
        active_projection = self.workspace_projection() if projection is None else projection
        mount_args: list[str] = []
        for host_path, container_path in active_projection:
            mount_args.extend(["--volume", f"{host_path}:{container_path}"])
        mount_args.extend(
            ["--volume", f"{empty_dotenv_resolved}:{CONTROLLER_MOUNT_TARGET}/.env:ro"]
        )
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
            *mount_args,
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

    def _extract_container_junit(
        self,
        *,
        record: dict[str, Any],
        log_dir: Path,
        controller_name: str,
        container_junit_path: str,
        host_junit_path: Path,
        junit_name: str,
        step_result: CommandResult,
    ) -> None:
        """Extract recorded JUnit artifact from the controller to the host evidence path."""
        if (
            step_result.returncode
            in (
                RUNNER_TIMEOUT_RETURN_CODE,
                RUNNER_CANCELLED_RETURN_CODE,
                RUNNER_LAUNCH_FAILURE_RETURN_CODE,
            )
            or step_result.returncode in DOCKER_RUN_FAILURE_RETURN_CODES
        ):
            return

        if host_junit_path.is_symlink() or host_junit_path.exists():
            raise SupervisorError(f"pre-existing destination for JUnit artifact: {host_junit_path}")

        cp_result = self.execute_step(
            record,
            log_dir=log_dir,
            name="Extract controller JUnit artifact",
            argv=[
                "docker",
                "cp",
                f"{controller_name}:{container_junit_path}",
                str(host_junit_path),
            ],
            cwd=self.repo_root,
            env=None,
            timeout=120.0,
        )

        if cp_result.returncode != 0:
            raise SupervisorError(
                "failed to extract recorded JUnit artifact from controller: "
                f"{_first_line(cp_result)}"
            )

        if (
            host_junit_path.is_symlink()
            or not host_junit_path.is_file()
            or host_junit_path.stat().st_size == 0
        ):
            raise SupervisorError(
                "extracted JUnit artifact is missing, empty, or not a regular file"
            )

        record["run"]["junit_artifact"] = junit_name

    def _record_controller_identity(
        self,
        record: dict[str, Any],
        *,
        controller_name: str,
        postgres_name: str,
        image_id: str,
        log_dir: Path,
        projection: list[tuple[Path, str]],
    ) -> dict[str, Any]:
        if CONTROLLER_UID == SANDBOX_UID:
            raise SupervisorError(
                f"the controller identity must differ from the sandbox identity ({CONTROLLER_UID})"
            )
        empty_dotenv_resolved = self.empty_dotenv_path().resolve()
        argv = self.controller_argv(
            controller_name,
            postgres_name,
            image_id,
            ["python3", "-c", CONTROLLER_PROBE_CODE],
            projection=projection,
        )
        mounts = [f"{host_path}:{container_path}" for host_path, container_path in projection] + [
            f"{empty_dotenv_resolved}:{CONTROLLER_MOUNT_TARGET}/.env:ro"
        ]
        result = self.execute_step(
            record,
            log_dir=log_dir,
            name="Controller identity probe",
            argv=argv,
            cwd=self.repo_root,
            env=None,
            timeout=300.0,
            container={
                "image": image_id,
                "user": CONTROLLER_USER,
                "init": True,
                "network": f"container:{postgres_name}",
                "mounts": mounts,
            },
        )
        if result.returncode != 0:
            raise SupervisorError(f"controller identity probe failed: {_first_line(result)}")
        values: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip()
        uid = parse_probe_integer(values, "uid")
        gid = parse_probe_integer(values, "gid")
        init_process = values.get("init", "")
        python = values.get("python", "")
        if uid != CONTROLLER_UID or gid != CONTROLLER_UID:
            raise SupervisorError(
                f"controller must run as UID/GID {CONTROLLER_UID}, observed {uid}/{gid}"
            )
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
        if values.get("dotenv_empty") != "True":
            raise SupervisorError(
                "controller must see a disabled or empty .env file at /workspace/.env"
            )
        return {
            "uid": uid,
            "gid": gid,
            "sandbox_uid": SANDBOX_UID,
            "distinct_from_sandbox": uid != SANDBOX_UID,
            "init_process": init_process,
            "python": python,
            "pytest": values.get("pytest", ""),
            "dotenv_isolated": True,
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
        rm_notes: list[str] = []
        for name in owned:
            if not _CONTROLLER_NAME_PATTERN.match(name):
                errors.append(f"refusing to remove unexpected container name {name!r}")
                continue
            argv = ["docker", "rm", "--force", "--volumes", name]
            removals.append(argv)
            result = self.run_teardown_cmd(argv, timeout=300)
            if result.returncode == 0:
                removed.append(name)
            else:
                # Docker's error text is not a stable interface to match on, and the
                # sweep below is what actually proves absence. A container that
                # survived still appears as a leftover and fails the run.
                rm_notes.append(f"docker rm {name}: {_first_line(result)}")
        verify_argv = ["docker", "ps", "--all", "--format", "{{.Names}}"]
        verified = self.run_teardown_cmd(verify_argv, timeout=120)
        if verified.returncode != 0:
            errors.append(f"docker ps failed after removal: {_first_line(verified)}")
            names: list[str] = []
        else:
            names = [line.strip() for line in verified.stdout.splitlines() if line.strip()]
        leftovers = sorted(name for name in names if name.startswith(prefix))
        return {
            "applicable": True,
            "attempted": True,
            "containers_removed": removed,
            "rm_notes": rm_notes,
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
        interrupted = False
        try:
            result = self.run_cmd(argv, cwd=cwd, env=env, timeout=timeout)
        except SupervisorCancelled:
            # The interrupted step is usually the long one and therefore the most
            # interesting; record it before propagating the shutdown signal.
            result = CommandResult(
                returncode=RUNNER_CANCELLED_RETURN_CODE,
                stdout="",
                stderr="step interrupted by the verification harness shutdown signal",
            )
            interrupted = True
        duration = round(time.monotonic() - started, 3)
        stdout_path.write_text(redact_secrets(result.stdout), encoding="utf-8", errors="replace")
        stderr_path.write_text(redact_secrets(result.stderr), encoding="utf-8", errors="replace")
        step: dict[str, Any] = {
            "name": name,
            "argv": list(argv),
            "exit_code": result.returncode,
            "duration_seconds": duration,
            "timeout_seconds": timeout,
            "stdout_log": f"logs/{stdout_path.name}",
            "stderr_log": f"logs/{stderr_path.name}",
        }
        sentinel = classify_runner_exit(result.returncode)
        if sentinel == "timed_out" and duration < timeout:
            # The runner also uses -1 for a launch failure that never started the
            # command, which is an environment error rather than a timeout.
            sentinel = "launch_failed"
        if container is not None and result.returncode in DOCKER_RUN_FAILURE_RETURN_CODES:
            # `docker run` refused the container: no test process ever started.
            sentinel = "launch_failed"
        if sentinel is not None:
            step["outcome"] = sentinel
        if container is not None:
            step["container"] = container
        record["steps"].append(step)
        self.log(f"{name}: exit {result.returncode} in {duration:.1f}s")
        if interrupted or sentinel == "cancelled":
            raise SupervisorCancelled(f"{name} was interrupted by the shutdown signal")
        return result

    @staticmethod
    def _step_failure(result: CommandResult, step: dict[str, Any]) -> tuple[str, int] | None:
        """Classify a step result without ever reporting a runner sentinel as a pass."""
        if result.returncode == 0:
            return None
        recorded = step.get("outcome")
        if recorded == "timed_out":
            return ("timed_out", EXIT_ENVIRONMENT)
        if recorded is not None:
            return ("environment_error", EXIT_ENVIRONMENT)
        return ("failed", EXIT_FAILED)

    @staticmethod
    def _record_pytest_step(
        record: dict[str, Any],
        *,
        pytest_argv: list[str],
        junit_artifact: str | None,
        result: CommandResult,
        check_no_tests: bool = True,
    ) -> None:
        """Share the pytest bookkeeping both selection paths must agree on."""
        record["run"]["pytest_argv"] = pytest_argv
        record["run"]["junit_artifact"] = junit_artifact
        if check_no_tests and result.returncode == PYTEST_NO_TESTS_COLLECTED:
            raise ConfigurationError(
                "the requested selection matched no tests; check the focused targets"
            )

    def _run_pytest_step(
        self,
        record: dict[str, Any],
        *,
        log_dir: Path,
        step: PytestStep,
        targets: tuple[str, ...],
        timeout: float,
        env: dict[str, str] | None,
        junit_artifact: str | None,
        junit_argument: str | None,
        wrap_argv: Callable[[list[str]], list[str]],
        container: dict[str, Any] | None,
        check_no_tests: bool = True,
    ) -> CommandResult:
        """Run one pytest step; the host and container paths differ only in wrapping."""
        pytest_argv = [*step.prefix, *targets, *step.suffix]
        if junit_argument is not None:
            pytest_argv.append(f"--junitxml={junit_argument}")
        result = self.execute_step(
            record,
            log_dir=log_dir,
            name=step.name,
            argv=wrap_argv(pytest_argv),
            cwd=self.repo_root,
            env=env,
            timeout=timeout,
            container=container,
        )
        self._record_pytest_step(
            record,
            pytest_argv=pytest_argv,
            junit_artifact=junit_artifact,
            result=result,
            check_no_tests=check_no_tests,
        )
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
                "mode": "focused" if request.focused else "default",
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
                "applicable": selection.container,
                "attempted": False,
                "containers_removed": [],
                "rm_notes": [],
                "leftovers": [],
                "errors": [],
                "removal_argv": [],
                "verification_argv": [],
                # Nothing to reclaim for a host selection, so absence is not a claim
                # that cleanup ran; `applicable` carries that distinction.
                "verified_absent": not selection.container,
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
                "host": self.host_toolchain(
                    require_docker=selection.container,
                    require_node=selection.requires_node,
                )
            }

            if selection.container:
                if selection.pytest_step is None:
                    raise SupervisorError(f"selection {selection.name!r} has no pytest step")
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
                projection = self.workspace_projection()
                record["environment"]["controller"] = self._record_controller_identity(
                    record,
                    # The probe keeps its own name: each controller step is an
                    # explicit, separately reclaimed container.
                    controller_name=f"{self.container_prefix(run_id)}-controller-probe",
                    postgres_name=postgres["name"],
                    image_id=image["id"],
                    log_dir=log_dir,
                    projection=projection,
                )
                step = selection.pytest_step
                junit_name = f"junit-{slugify(step.name)}.xml"
                container_junit_path = f"/tmp/{junit_name}"
                host_junit_path = evidence_dir / junit_name
                empty_dotenv_resolved = self.empty_dotenv_path().resolve()
                mounts = [
                    f"{host_path}:{container_path}" for host_path, container_path in projection
                ] + [f"{empty_dotenv_resolved}:{CONTROLLER_MOUNT_TARGET}/.env:ro"]
                result = self._run_pytest_step(
                    record,
                    log_dir=log_dir,
                    step=step,
                    targets=targets,
                    timeout=request.timeout or step.timeout,
                    env=None,
                    junit_artifact=None,
                    junit_argument=container_junit_path,
                    wrap_argv=lambda argv: self.controller_argv(
                        controller_name,
                        postgres["name"],
                        image["id"],
                        argv,
                        projection=projection,
                    ),
                    container={
                        "image": image["id"],
                        "user": CONTROLLER_USER,
                        "init": True,
                        "network": f"container:{postgres['name']}",
                        "mounts": mounts,
                    },
                    check_no_tests=False,
                )
                pytest_step_record = record["steps"][-1]
                self._extract_container_junit(
                    record=record,
                    log_dir=log_dir,
                    controller_name=controller_name,
                    container_junit_path=container_junit_path,
                    host_junit_path=host_junit_path,
                    junit_name=junit_name,
                    step_result=result,
                )
                if result.returncode == PYTEST_NO_TESTS_COLLECTED:
                    raise ConfigurationError(
                        "the requested selection matched no tests; check the focused targets"
                    )
                outcome, exit_code = self._step_failure(result, pytest_step_record) or (
                    "passed",
                    EXIT_PASSED,
                )
            else:
                failure: tuple[str, int] | None = None
                for step in selection.steps:
                    result = self.execute_step(
                        record,
                        log_dir=log_dir,
                        name=step.name,
                        argv=list(step.argv),
                        cwd=self.repo_root,
                        env=child_env,
                        timeout=step.timeout,
                    )
                    failure = self._step_failure(result, record["steps"][-1])
                    if failure is not None:
                        break
                if failure is None and selection.pytest_step is not None:
                    step = selection.pytest_step
                    junit_name = f"junit-{slugify(step.name)}.xml"
                    result = self._run_pytest_step(
                        record,
                        log_dir=log_dir,
                        step=step,
                        targets=targets,
                        timeout=request.timeout or step.timeout,
                        env=child_env,
                        junit_artifact=junit_name,
                        junit_argument=str(evidence_dir / junit_name),
                        wrap_argv=lambda argv: argv,
                        container=None,
                    )
                    failure = self._step_failure(result, record["steps"][-1])
                outcome, exit_code = failure or ("passed", EXIT_PASSED)
        except ConfigurationError as error:
            outcome, exit_code = "configuration_error", EXIT_CONFIGURATION
            record["error"] = {"kind": "configuration", "message": str(error)}
            print(f"[verify] configuration error: {error}", file=self.stderr, flush=True)
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
        except Exception as error:  # noqa: BLE001 - a run must always leave a record
            # The harness contract is that a run always leaves a record. An
            # unexpected defect is recorded as an environment error rather than
            # escaping as a traceback with no evidence.
            outcome, exit_code = "internal_error", EXIT_ENVIRONMENT
            record["error"] = {
                "kind": "internal",
                "message": f"{type(error).__name__}: {error}",
            }
            print(
                f"[verify] internal harness error: {type(error).__name__}: {error}",
                file=self.stderr,
                flush=True,
            )
        finally:
            if self.stop_event.is_set() and outcome != "cancelled":
                outcome, exit_code = "cancelled", EXIT_CANCELLED
            if selection.container:
                try:
                    cleanup = self.cleanup(run_id)
                except Exception as error:  # noqa: BLE001 - a run must leave a record
                    # Teardown runs after every other handler, so an unexpected
                    # failure here must not be the one path that writes nothing.
                    cleanup = {
                        "applicable": True,
                        "attempted": True,
                        "containers_removed": [],
                        "rm_notes": [],
                        "leftovers": [],
                        "errors": [f"cleanup raised {type(error).__name__}: {error}"],
                        "removal_argv": [],
                        "verification_argv": [],
                        "verified_absent": False,
                    }
                record["cleanup"] = cleanup
                if not cleanup["verified_absent"]:
                    # A surviving owned container is the diagnosis the operator
                    # needs, so it outranks the cancellation verdict above.
                    exit_code = EXIT_ENVIRONMENT
                    outcome = "cleanup_failed"
                    print(
                        "[verify] owned containers survived cleanup: "
                        f"{cleanup['leftovers'] or cleanup['errors']}",
                        file=self.stderr,
                        flush=True,
                    )
            finished = _utc_now()
            record["run"]["finished_at"] = _timestamp(finished)
            record["run"]["duration_seconds"] = round((finished - started).total_seconds(), 3)
            record["run"]["outcome"] = outcome
            record["run"]["exit_code"] = exit_code
            try:
                (evidence_dir / "run.json").write_text(
                    json.dumps(record, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as error:
                print(
                    f"[verify] could not write the evidence record: {error}",
                    file=self.stderr,
                    flush=True,
                )
                if exit_code == EXIT_PASSED:
                    exit_code = EXIT_ENVIRONMENT
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
