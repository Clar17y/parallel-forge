"""Focused argument, evidence and cleanup contract checks for scripts/verify.py."""

from __future__ import annotations

import json
import sys
import threading
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Imported after the repository root above is placed on sys.path.
from scripts.dev import CommandResult
from scripts.verify import (
    CONTROLLER_PROBE_CODE,
    CONTROLLER_UID,
    EXIT_CANCELLED,
    EXIT_CONFIGURATION,
    EXIT_ENVIRONMENT,
    EXIT_FAILED,
    EXIT_PASSED,
    SANDBOX_UID,
    SELECTIONS,
    ConfigurationError,
    LocalVerificationHarness,
    RunRequest,
    build_parser,
    build_request,
    is_reserved_environment_name,
    print_selections,
)

IMAGE_ID = "sha256:" + "a" * 64


class FakeCommandRunner:
    """Deterministic stand-in for the host and Docker toolchain."""

    def __init__(
        self,
        *,
        controller_exit: int = 0,
        init_process: str = "docker-init",
        uid: int = CONTROLLER_UID,
        leftover: bool = False,
        cancel_after: int | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.controller_exit = controller_exit
        self.init_process = init_process
        self.uid = uid
        self.leftover = leftover
        self.cancel_after = cancel_after
        self.owned_names: list[str] = []

    @property
    def argv_calls(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls]

    def spawn(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the verification harness must not spawn unmanaged processes")

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> CommandResult:
        command = [str(item) for item in argv]
        self.calls.append({"argv": command, "cwd": cwd, "env": env, "timeout": timeout})
        if (
            self.cancel_after is not None
            and len(self.calls) >= self.cancel_after
            and stop_event is not None
        ):
            stop_event.set()
        return self._dispatch(command)

    def _probe_stdout(self) -> str:
        return (
            f"uid={self.uid}\n"
            f"gid={self.uid}\n"
            f"init={self.init_process}\n"
            "python=3.14.2\n"
            "pytest=8.4.2\n"
            "uv_lock_present=True\n"
        )

    def _dispatch(self, argv: list[str]) -> CommandResult:
        if argv[:3] == ["docker", "image", "inspect"]:
            return CommandResult(0, f"{IMAGE_ID}\n", "")
        if argv[:2] == ["docker", "build"]:
            return CommandResult(0, "built\n", "")
        if argv[:3] == ["docker", "run", "--detach"]:
            return CommandResult(0, "b" * 64 + "\n", "")
        if argv[:3] == ["docker", "run", "--init"]:
            if argv[-1] == CONTROLLER_PROBE_CODE:
                return CommandResult(0, self._probe_stdout(), "")
            return CommandResult(self.controller_exit, "", "")
        if argv[:2] == ["docker", "exec"]:
            return CommandResult(0, "", "")
        if argv[:2] == ["docker", "rm"]:
            self.owned_names.append(argv[-1])
            return CommandResult(0, f"{argv[-1]}\n", "")
        if argv[:2] == ["docker", "ps"]:
            names = self.owned_names if self.leftover else []
            return CommandResult(0, "".join(f"{name}\n" for name in names), "")
        if argv[:2] == ["docker", "--version"]:
            return CommandResult(0, "Docker version 29.7.2\n", "")
        if argv[:2] == ["docker", "version"]:
            return CommandResult(0, "29.7.2 linux/amd64\n", "")
        if argv[:2] == ["node", "--version"]:
            return CommandResult(0, "v24.20.0\n", "")
        if argv[:2] == ["npm", "--version"]:
            return CommandResult(0, "12.0.2\n", "")
        if argv[:2] == ["uv", "--version"]:
            return CommandResult(0, "uv 0.12.15\n", "")
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return CommandResult(0, "c" * 40 + "\n", "")
        if argv[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return CommandResult(0, "forge/v0-2\n", "")
        if argv[:3] == ["git", "ls-files", "-s"]:
            return CommandResult(0, "100644 abc123 0\tfile.py\n", "")
        if argv[:2] == ["git", "status"]:
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile.verify").write_text("# fake controller image\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'forge'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return repo


def run_harness(
    repo: Path,
    runner: FakeCommandRunner,
    request: RunRequest,
) -> tuple[int, dict[str, Any], LocalVerificationHarness]:
    harness = LocalVerificationHarness(
        repo_root=repo,
        runner=runner,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    exit_code = harness.run_selection(request)
    evidence_root = repo / ".llm-output" / "local-verification"
    run_dirs = sorted(path for path in evidence_root.iterdir() if path.is_dir())
    assert len(run_dirs) == 1, run_dirs
    record = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    return exit_code, record, harness


def parse(*argv: str) -> Any:
    return build_parser().parse_args(list(argv))


def test_list_prints_every_selection() -> None:
    stream = StringIO()
    print_selections(stream)
    output = stream.getvalue()
    for name, selection in SELECTIONS.items():
        assert name in output
        assert selection.summary in output
    assert "acknowledge with --full" in output


def test_unknown_selection_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        build_request(parse("run", "everything"))


@pytest.mark.parametrize("target", ["/etc/passwd", "../outside", "--collect-only", "  "])
def test_focused_targets_reject_escapes_and_options(target: str) -> None:
    with pytest.raises(ConfigurationError):
        build_request(parse("run", "backend", f"--focused={target}"))


def test_focused_and_full_are_mutually_exclusive() -> None:
    with pytest.raises(ConfigurationError):
        build_request(parse("run", "backend", "--full", "--focused", "tests/x.py"))


def test_broad_selection_requires_acknowledgement() -> None:
    with pytest.raises(ConfigurationError) as error:
        build_request(parse("run", "backend"))
    assert "--full" in str(error.value)


def test_bounded_selection_rejects_full_acknowledgement() -> None:
    with pytest.raises(ConfigurationError):
        build_request(parse("run", "unit", "--full"))


def test_host_selection_rejects_image_override() -> None:
    with pytest.raises(ConfigurationError):
        build_request(parse("run", "unit", "--image", "forge-local-verification:x"))


def test_selection_without_pytest_step_rejects_focus() -> None:
    with pytest.raises(ConfigurationError):
        build_request(parse("run", "web", "--focused", "tests/x.py"))


def test_focused_request_is_accepted() -> None:
    request = build_request(
        parse("run", "backend", "--focused", "tests/a.py", "--focused", "tests/b.py::x")
    )
    assert request.focused == ("tests/a.py", "tests/b.py::x")
    assert request.full is False


@pytest.mark.parametrize(
    ("name", "reserved"),
    [
        ("PATH", False),
        ("HOME", False),
        ("UV_CACHE_DIR", False),
        ("FORGE_E2E_OPERATOR", False),
        ("DATABASE_URL", True),
        ("DEEPSEEK_API_KEY", True),
        ("FORGE_SUBSCRIPTION_INSTALLATIONS_PATH", True),
        ("ANTHROPIC_API_KEY", True),
        ("GH_TOKEN", True),
        ("PGPASSWORD", True),
    ],
)
def test_reserved_environment_names_are_filtered(name: str, reserved: bool) -> None:
    assert is_reserved_environment_name(name) is reserved


def test_container_run_records_identity_commands_and_cleanup(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-recorded")
    runner = FakeCommandRunner()
    exit_code, record, _ = run_harness(
        fake_repo, runner, RunRequest(selection="backend", focused=("tests/recovery_process/x.py",))
    )

    assert exit_code == EXIT_PASSED
    assert record["run"]["outcome"] == "passed"
    assert record["run"]["mode"] == "focused"
    assert record["run"]["targets"] == ["tests/recovery_process/x.py"]
    assert record["candidate"]["head"] == "c" * 40
    assert record["candidate"]["branch"] == "forge/v0-2"
    assert record["policy"]["live_providers"] is False
    assert record["policy"]["github_actions"] is False
    assert record["policy"]["automatic_retries"] is False
    assert "DEEPSEEK_API_KEY" in record["policy"]["redacted_environment_names"]

    controller = record["environment"]["controller"]
    assert controller["uid"] == CONTROLLER_UID
    assert controller["distinct_from_sandbox"] is True
    assert controller["sandbox_uid"] == SANDBOX_UID
    assert controller["init_process"] == "docker-init"
    assert record["environment"]["postgres"]["readiness"]["attempts"] >= 1
    assert record["environment"]["postgres"]["endpoint"] == "127.0.0.1:5435"
    assert record["environment"]["postgres"]["network"] == "none"

    # Every recorded command is an argv list, and the test step runs in the mount.
    assert all(isinstance(step["argv"], list) for step in record["steps"])
    test_argv = record["run"]["pytest_argv"]
    assert test_argv[0:3] == ["python", "-m", "pytest"]
    assert test_argv[3] == "tests/recovery_process/x.py"
    assert any(argument.startswith("--junitxml=/workspace/") for argument in test_argv)
    controller_step = record["steps"][-1]
    assert controller_step["container"]["user"] == "1000:1000"
    assert controller_step["container"]["init"] is True
    assert controller_step["exit_code"] == 0

    # Host credentials never reach a child process or the record.
    serialized = json.dumps(record)
    assert "must-not-be-recorded" not in serialized
    container_calls = [argv for argv in runner.argv_calls if argv[:2] == ["docker", "run"]]
    assert container_calls
    for argv in container_calls:
        assert "--env-file" not in argv
        assert not any("DEEPSEEK" in item for item in argv)
    for call in runner.calls:
        assert "DEEPSEEK_API_KEY" not in (call["env"] or {})


def test_selection_failure_is_recorded_and_still_cleans_up(fake_repo: Path) -> None:
    runner = FakeCommandRunner(controller_exit=EXIT_FAILED)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_FAILED
    assert record["run"]["outcome"] == "failed"
    assert record["run"]["exit_code"] == EXIT_FAILED
    assert record["cleanup"]["attempted"] is True
    assert record["cleanup"]["verified_absent"] is True
    removed = record["cleanup"]["containers_removed"]
    assert len(removed) == 3
    assert any(name.endswith("-controller") for name in removed)
    assert record["cleanup"]["removal_argv"]


def test_owned_container_leftovers_fail_the_run(fake_repo: Path) -> None:
    runner = FakeCommandRunner(leftover=True)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "cleanup_failed"
    assert record["cleanup"]["verified_absent"] is False
    assert record["cleanup"]["leftovers"]


def test_missing_init_reaper_fails_closed(fake_repo: Path) -> None:
    runner = FakeCommandRunner(init_process="python3")
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["error"]["kind"] == "environment"
    assert record["cleanup"]["verified_absent"] is True


def test_sandbox_uid_is_rejected(fake_repo: Path) -> None:
    runner = FakeCommandRunner(uid=SANDBOX_UID)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"


def test_cancellation_is_recorded_and_cleanup_still_runs(fake_repo: Path) -> None:
    runner = FakeCommandRunner(cancel_after=3)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_CANCELLED
    assert record["run"]["outcome"] == "cancelled"
    assert record["cleanup"]["attempted"] is True
    assert len(record["cleanup"]["containers_removed"]) == 3


def test_host_selection_records_steps_without_containers(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_SUBSCRIPTION_INSTALLATIONS_PATH", "/secret/path")
    runner = FakeCommandRunner()
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="unit"))

    assert exit_code == EXIT_PASSED
    assert record["run"]["outcome"] == "passed"
    assert record["run"]["mode"] == "full"
    assert record["cleanup"]["attempted"] is False
    assert [step["name"] for step in record["steps"]] == [
        "Sync locked Python dependencies",
        "Unit and host wrapper contracts",
    ]
    assert record["environment"]["host"]["node"] == "v24.20.0"
    assert "docker_server" not in record["environment"]["host"]
    assert record["run"]["junit_artifact"] == "junit-unit-and-host-wrapper-contracts.xml"
    assert any(argument.startswith("--junitxml=") for argument in record["run"]["pytest_argv"])
    assert "FORGE_SUBSCRIPTION_INSTALLATIONS_PATH" in record["policy"]["redacted_environment_names"]
    assert "/secret/path" not in json.dumps(record)
    host_calls = [call for call in runner.calls if call["env"] is not None]
    assert host_calls
    for call in host_calls:
        assert "FORGE_SUBSCRIPTION_INSTALLATIONS_PATH" not in call["env"]


def test_focused_host_selection_replaces_default_targets(fake_repo: Path) -> None:
    runner = FakeCommandRunner()
    exit_code, record, _ = run_harness(
        fake_repo,
        runner,
        RunRequest(selection="unit", focused=("tests/ci/test_workflow_policy.py",)),
    )

    assert exit_code == EXIT_PASSED
    assert record["run"]["mode"] == "focused"
    argv = record["run"]["pytest_argv"]
    assert "tests/ci/test_workflow_policy.py" in argv
    assert not any(argument.startswith("apps/orchestrator/tests/agents") for argument in argv)
    assert (
        len([step for step in record["steps"] if step["name"] == "Sync locked Python dependencies"])
        == 1
    )


def test_configuration_error_is_recorded(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    class NoTestsRunner(FakeCommandRunner):
        def _dispatch(self, argv: list[str]) -> CommandResult:
            if argv[:3] == ["docker", "run", "--init"] and argv[-1] != CONTROLLER_PROBE_CODE:
                return CommandResult(5, "", "no tests ran\n")
            return super()._dispatch(argv)

    exit_code, record, _ = run_harness(
        fake_repo, NoTestsRunner(), RunRequest(selection="backend", focused=("tests/missing.py",))
    )

    assert exit_code == EXIT_CONFIGURATION
    assert record["run"]["outcome"] == "configuration_error"
    assert record["cleanup"]["verified_absent"] is True


def test_setup_failure_before_controller_still_cleans_up(fake_repo: Path) -> None:
    class BrokenPostgresRunner(FakeCommandRunner):
        def _dispatch(self, argv: list[str]) -> CommandResult:
            if argv[:3] == ["docker", "run", "--detach"]:
                return CommandResult(125, "", "docker: port is already allocated\n")
            return super()._dispatch(argv)

    exit_code, record, _ = run_harness(
        fake_repo, BrokenPostgresRunner(), RunRequest(selection="recovery")
    )

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["cleanup"]["attempted"] is True
    assert record["cleanup"]["verified_absent"] is True
