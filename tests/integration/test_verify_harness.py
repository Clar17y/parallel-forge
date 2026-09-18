"""Focused argument, evidence and cleanup contract checks for scripts/verify.py."""

from __future__ import annotations

import json
import sys
import threading
import time
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
    CONTROLLER_MOUNT_TARGET,
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
    SupervisorError,
    build_parser,
    build_request,
    is_reserved_environment_name,
    is_root_dotenv_secret,
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
        cancel_on_pytest: bool = False,
        rm_exit_code: int = 0,
        probe_stdout_override: str | None = None,
        sleep_seconds: float = 0.0,
        git_status: str | None = None,
        ls_files: list[str] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.controller_exit = controller_exit
        self.init_process = init_process
        self.uid = uid
        self.leftover = leftover
        self.cancel_after = cancel_after
        self.cancel_on_pytest = cancel_on_pytest
        self.rm_exit_code = rm_exit_code
        self.probe_stdout_override = probe_stdout_override
        self.sleep_seconds = sleep_seconds
        self.git_status = git_status
        self.ls_files = ls_files
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
        if self.cancel_on_pytest and "pytest" in command:
            if stop_event is not None:
                stop_event.set()
            return CommandResult(-2, "", "Command cancelled")
        if self.sleep_seconds and "pytest" in command:
            time.sleep(self.sleep_seconds)
        return self._dispatch(command)

    def _probe_stdout(self) -> str:
        if self.probe_stdout_override is not None:
            return self.probe_stdout_override
        return (
            f"uid={self.uid}\n"
            f"gid={self.uid}\n"
            f"init={self.init_process}\n"
            "python=3.14.2\n"
            "pytest=8.4.2\n"
            "uv_lock_present=True\n"
            "dotenv_empty=True\n"
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
        if argv[:2] == ["docker", "cp"]:
            destination = Path(argv[3])
            destination.write_text("<testsuites></testsuites>\n", encoding="utf-8")
            return CommandResult(0, "", "")
        if argv[:2] == ["docker", "rm"]:
            if self.rm_exit_code:
                return CommandResult(
                    self.rm_exit_code, "", "Error response from daemon: removal failed\n"
                )
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
        if argv[:2] == ["git", "ls-files"]:
            files = (
                self.ls_files
                if self.ls_files is not None
                else ["Dockerfile.verify", "pyproject.toml", "uv.lock"]
            )
            if "-s" in argv:
                return CommandResult(0, "".join(f"100644 abc123 0\t{f}\n" for f in files), "")
            if "-z" in argv:
                return CommandResult(0, "".join(f"{f}\0" for f in files), "")
            return CommandResult(0, "\n".join(files) + "\n", "")
        if argv[:2] == ["git", "status"]:
            return CommandResult(0, self.git_status if self.git_status is not None else "", "")
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
    assert any(argument.startswith("--junitxml=/tmp/") for argument in test_argv)
    assert not any(argument.startswith("--junitxml=/workspace/") for argument in test_argv)
    controller_step = [step for step in record["steps"] if "container" in step][-1]
    assert controller_step["container"]["user"] == "1000:1000"
    assert controller_step["container"]["init"] is True
    assert controller_step["exit_code"] == 0
    extract_step = record["steps"][-1]
    assert extract_step["name"] == "Extract controller JUnit artifact"
    assert extract_step["argv"][:2] == ["docker", "cp"]
    assert extract_step["exit_code"] == 0

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
    assert record["run"]["mode"] == "default"
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


def test_timed_out_step_is_not_recorded_as_a_test_failure(fake_repo: Path) -> None:
    """A runner timeout sentinel means nothing is known about the tests."""
    runner = FakeCommandRunner(controller_exit=-1, sleep_seconds=0.05)
    exit_code, record, _ = run_harness(
        fake_repo, runner, RunRequest(selection="recovery", timeout=0.01)
    )

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "timed_out"
    assert record["steps"][-1]["outcome"] == "timed_out"
    assert record["cleanup"]["verified_absent"] is True


def test_instant_runner_failure_is_not_reported_as_a_timeout(fake_repo: Path) -> None:
    """The runner also uses -1 for a launch that never started the command."""
    runner = FakeCommandRunner(controller_exit=-1)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["steps"][-1]["outcome"] == "launch_failed"


def test_docker_refusal_is_not_reported_as_a_test_failure(fake_repo: Path) -> None:
    """`docker run` exits 125 when the daemon refuses to start the container."""
    runner = FakeCommandRunner(controller_exit=125)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["steps"][-1]["outcome"] == "launch_failed"


def test_launch_failure_is_recorded_as_an_environment_error(fake_repo: Path) -> None:
    runner = FakeCommandRunner(controller_exit=127)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["steps"][-1]["outcome"] == "launch_failed"


def test_interrupted_step_is_still_recorded(fake_repo: Path) -> None:
    """The step the operator interrupted must not vanish from the record."""
    runner = FakeCommandRunner(cancel_on_pytest=True)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_CANCELLED
    assert record["run"]["outcome"] == "cancelled"
    interrupted = record["steps"][-1]
    assert interrupted["name"] == "Focused recovery boundaries"
    assert interrupted["outcome"] == "cancelled"
    assert interrupted["exit_code"] == -2
    assert record["cleanup"]["verified_absent"] is True


def test_cleanup_failure_outranks_cancellation(fake_repo: Path) -> None:
    """A surviving owned container is the diagnosis, even if a signal stopped the run."""
    runner = FakeCommandRunner(cancel_after=3, leftover=True)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "cleanup_failed"
    assert record["cleanup"]["verified_absent"] is False


def test_failed_removal_is_not_a_cleanup_failure_when_the_container_is_gone(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner(rm_exit_code=1)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_PASSED
    assert record["cleanup"]["verified_absent"] is True
    assert record["cleanup"]["containers_removed"] == []
    assert record["cleanup"]["rm_notes"]


def test_host_selection_maps_an_empty_selection_to_a_configuration_error(
    fake_repo: Path,
) -> None:
    class NoTestsRunner(FakeCommandRunner):
        def _dispatch(self, argv: list[str]) -> CommandResult:
            if "pytest" in argv:
                return CommandResult(5, "", "no tests ran\n")
            return super()._dispatch(argv)

    exit_code, record, _ = run_harness(
        fake_repo, NoTestsRunner(), RunRequest(selection="unit", focused=("tests/missing.py",))
    )

    assert exit_code == EXIT_CONFIGURATION
    assert record["run"]["outcome"] == "configuration_error"


@pytest.mark.parametrize("target", ["\\Windows\\x.py", "C:/x.py", ".", "./"])
def test_focused_targets_reject_repo_roots_and_drive_relative_paths(target: str) -> None:
    with pytest.raises(ConfigurationError):
        build_request(parse("run", "backend", f"--focused={target}"))


def test_focused_target_accepts_a_directory_target() -> None:
    request = build_request(parse("run", "backend", "--focused", "apps/orchestrator/tests"))
    assert request.focused == ("apps/orchestrator/tests",)


def test_timeout_is_rejected_for_a_selection_without_a_pytest_step() -> None:
    with pytest.raises(ConfigurationError):
        build_request(parse("run", "web", "--timeout", "300"))


def test_container_selection_with_setup_steps_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container selection whose setup steps would be skipped must fail closed."""
    from scripts.verify import Selection, Step

    monkeypatch.setitem(
        SELECTIONS,
        "container-with-steps",
        Selection(
            name="container-with-steps",
            summary="test-only selection",
            container=True,
            broad=False,
            steps=(Step("Setup", ("python", "-c", "pass"), 60.0),),
            pytest_step=SELECTIONS["recovery"].pytest_step,
        ),
    )

    with pytest.raises(ConfigurationError):
        build_request(parse("run", "container-with-steps"))


def test_missing_controller_image_input_is_a_prerequisite_error(fake_repo: Path) -> None:
    (fake_repo / "uv.lock").unlink()
    exit_code, record, _ = run_harness(
        fake_repo, FakeCommandRunner(), RunRequest(selection="recovery")
    )

    assert exit_code == EXIT_ENVIRONMENT
    assert record["error"]["kind"] == "prerequisite"


def test_malformed_identity_probe_is_an_environment_error(fake_repo: Path) -> None:
    runner = FakeCommandRunner(probe_stdout_override="uid=unknown\ngid=unknown\ninit=docker-init\n")
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["error"]["kind"] == "environment"


def test_unexpected_harness_defect_is_recorded(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: LocalVerificationHarness) -> dict[str, Any]:
        raise ValueError("unexpected defect")

    monkeypatch.setattr(LocalVerificationHarness, "candidate_identity", explode)
    exit_code, record, _ = run_harness(
        fake_repo, FakeCommandRunner(), RunRequest(selection="recovery")
    )

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "internal_error"
    assert record["error"]["kind"] == "internal"
    assert "unexpected defect" in record["error"]["message"]
    assert record["cleanup"]["verified_absent"] is True


def test_candidate_identity_changes_when_unstaged_tracked_file_bytes_change(
    fake_repo: Path,
) -> None:
    file_path = fake_repo / "file.py"
    file_path.write_text("print('version 1')\n", encoding="utf-8")
    runner = FakeCommandRunner(git_status=" M file.py\0")
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)

    ident1 = harness.candidate_identity()
    assert ident1["dirty"] is True

    # Change unstaged file bytes without status or path change
    file_path.write_text("print('version 2')\n", encoding="utf-8")
    ident2 = harness.candidate_identity()

    assert ident2["dirty"] is True
    assert ident1["tracked_tree_digest"] == ident2["tracked_tree_digest"]
    assert ident1["worktree_digest"] != ident2["worktree_digest"]


def test_candidate_identity_changes_when_untracked_file_bytes_change(
    fake_repo: Path,
) -> None:
    untracked_path = fake_repo / "scratch with spaces.bin"
    untracked_path.write_bytes(b"\x00\xff\xfe\x01")
    runner = FakeCommandRunner(git_status="?? scratch with spaces.bin\0")
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)

    ident1 = harness.candidate_identity()
    assert ident1["dirty"] is True

    # Change untracked bytes without rename
    untracked_path.write_bytes(b"\x00\xff\xfe\x02")
    ident2 = harness.candidate_identity()

    assert ident2["dirty"] is True
    assert ident1["tracked_tree_digest"] == ident2["tracked_tree_digest"]
    assert ident1["worktree_digest"] != ident2["worktree_digest"]


def test_parse_worktree_status_handles_worktree_renames_and_copies(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner()
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    raw = (
        " R renamed_worktree.py\0orig_worktree.py\0"
        "?? following_file.py\0"
        " C copied_worktree.py\0orig_copied.py\0"
        "M  staged_modified.py\0"
    )
    entries = harness._parse_worktree_status(raw)
    assert entries == [
        (" R", "renamed_worktree.py", "orig_worktree.py"),
        ("??", "following_file.py", None),
        (" C", "copied_worktree.py", "orig_copied.py"),
        ("M ", "staged_modified.py", None),
    ]


def test_parse_worktree_status_rejects_truncated_entry(fake_repo: Path) -> None:
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=FakeCommandRunner())

    with pytest.raises(SupervisorError, match="malformed git status entry"):
        harness._parse_worktree_status("?\0")


def test_candidate_identity_fails_closed_on_decode_mangled_path(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner(git_status="?? invalid_\\xff_path.py\0")
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    with pytest.raises(SupervisorError, match="decode-mangled|unreadable|missing"):
        harness.candidate_identity()


def test_candidate_identity_fails_closed_on_raced_away_modified_file(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner(git_status=" M raced_away.py\0")
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    with pytest.raises(SupervisorError, match="missing, raced away, or unreadable"):
        harness.candidate_identity()


def test_candidate_identity_allows_missing_digest_for_actual_deletion(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner(git_status=" D deleted_file.py\0")
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    ident = harness.candidate_identity()
    assert ident["dirty"] is True
    assert ident["worktree_digest"]


def test_controller_containers_mask_host_dotenv(
    fake_repo: Path,
) -> None:
    host_dotenv = fake_repo / ".env"
    host_dotenv.write_text("SUPER_SECRET_TOKEN=must-not-enter-container\n", encoding="utf-8")
    runner = FakeCommandRunner()
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_PASSED
    serialized_record = json.dumps(record)
    assert "must-not-enter-container" not in serialized_record

    # Check all controller invocations (probe and pytest)
    controller_runs = [
        argv for argv in runner.argv_calls if argv[:3] == ["docker", "run", "--init"]
    ]
    assert len(controller_runs) == 2  # probe and pytest
    for argv in controller_runs:
        assert any(arg.endswith(":/workspace/.env:ro") for arg in argv)
        assert not any("must-not-enter-container" in arg for arg in argv)


def test_dotenv_mask_rejects_preexisting_symlink(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeCommandRunner()
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    mask_path = harness.evidence_root / ".empty-dotenv"
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    mask_path.write_bytes(b"")

    orig_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: True if str(self) == str(mask_path) else orig_is_symlink(self),
    )
    with pytest.raises(SupervisorError, match="symlink"):
        harness.empty_dotenv_path()


def test_dotenv_mask_rejects_directory(fake_repo: Path) -> None:
    runner = FakeCommandRunner()
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    mask_path = harness.evidence_root / ".empty-dotenv"
    mask_path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(SupervisorError, match="not a regular file"):
        harness.empty_dotenv_path()


def test_dotenv_mask_overwrites_stale_content_and_revalidates_zero_bytes(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner()
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    mask_path = harness.evidence_root / ".empty-dotenv"
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    mask_path.write_text("STALE_CONTENT=1\n", encoding="utf-8")

    path = harness.empty_dotenv_path()
    assert path.is_file()
    assert not path.is_symlink()
    assert path.stat().st_size == 0


def test_controller_argv_revalidates_mask_immediately_before_mount(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner()
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    mask_path = harness.empty_dotenv_path()
    mask_path.unlink()
    mask_path.mkdir()
    with pytest.raises(SupervisorError, match="not a regular file"):
        harness.controller_argv("controller", "postgres", IMAGE_ID, ["pytest"])


def test_controller_probe_rejects_non_empty_dotenv(fake_repo: Path) -> None:
    runner = FakeCommandRunner(
        probe_stdout_override=(
            f"uid={CONTROLLER_UID}\n"
            f"gid={CONTROLLER_UID}\n"
            f"init=docker-init\n"
            "python=3.14.2\n"
            "pytest=8.4.2\n"
            "uv_lock_present=True\n"
            "dotenv_empty=False\n"
        )
    )
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))
    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"


def test_container_junit_destination_uses_container_storage_and_extracts_artifact(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner()
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_PASSED
    test_argv = record["run"]["pytest_argv"]
    assert any(arg.startswith("--junitxml=/tmp/") for arg in test_argv)
    assert not any(arg.startswith("--junitxml=/workspace/") for arg in test_argv)

    # docker cp was invoked to extract the junit artifact to the host evidence directory
    cp_calls = [argv for argv in runner.argv_calls if argv[:2] == ["docker", "cp"]]
    assert len(cp_calls) == 1
    assert cp_calls[0][2].endswith(":/tmp/junit-focused-recovery-boundaries.xml")
    assert cp_calls[0][3].endswith("junit-focused-recovery-boundaries.xml")
    assert Path(cp_calls[0][3]).is_file()


def test_container_junit_extraction_failure_is_an_environment_error(
    fake_repo: Path,
) -> None:
    class FailingCpRunner(FakeCommandRunner):
        def _dispatch(self, argv: list[str]) -> CommandResult:
            if argv[:2] == ["docker", "cp"]:
                return CommandResult(1, "", "Error: could not copy from container\n")
            return super()._dispatch(argv)

    exit_code, record, _ = run_harness(
        fake_repo, FailingCpRunner(), RunRequest(selection="recovery")
    )

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["error"]["kind"] == "environment"
    assert record["cleanup"]["verified_absent"] is True


def test_container_junit_extraction_recorded_as_evidence_step(fake_repo: Path) -> None:
    runner = FakeCommandRunner()
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_PASSED
    assert record["run"]["junit_artifact"] == "junit-focused-recovery-boundaries.xml"
    step_names = [step["name"] for step in record["steps"]]
    assert "Extract controller JUnit artifact" in step_names
    extract_step = next(
        step for step in record["steps"] if step["name"] == "Extract controller JUnit artifact"
    )
    assert extract_step["argv"][:2] == ["docker", "cp"]
    assert extract_step["exit_code"] == 0
    assert extract_step["timeout_seconds"] == 120.0
    assert extract_step["duration_seconds"] >= 0
    evidence_dir = Path(record["run"]["evidence_dir"])
    assert (evidence_dir / extract_step["stdout_log"]).is_file()
    assert (evidence_dir / extract_step["stderr_log"]).is_file()


def test_container_junit_extraction_succeeds_on_failing_pytest_preserving_failure_verdict(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner(controller_exit=EXIT_FAILED)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_FAILED
    assert record["run"]["outcome"] == "failed"
    assert record["run"]["junit_artifact"] == "junit-focused-recovery-boundaries.xml"
    step_names = [step["name"] for step in record["steps"]]
    assert "Extract controller JUnit artifact" in step_names
    extract_step = next(
        step for step in record["steps"] if step["name"] == "Extract controller JUnit artifact"
    )
    assert extract_step["exit_code"] == 0
    pytest_step = [step for step in record["steps"] if "container" in step][-1]
    assert pytest_step["exit_code"] == EXIT_FAILED


def test_container_junit_extraction_failure_fails_closed_when_pytest_fails(
    fake_repo: Path,
) -> None:
    class FailingCpAndPytestRunner(FakeCommandRunner):
        def __init__(self) -> None:
            super().__init__(controller_exit=EXIT_FAILED)

        def _dispatch(self, argv: list[str]) -> CommandResult:
            if argv[:2] == ["docker", "cp"]:
                return CommandResult(1, "", "Error: could not copy\n")
            return super()._dispatch(argv)

    exit_code, record, _ = run_harness(
        fake_repo, FailingCpAndPytestRunner(), RunRequest(selection="recovery")
    )

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["run"]["junit_artifact"] is None
    assert record["error"]["kind"] == "environment"
    pytest_step = [step for step in record["steps"] if "container" in step][-1]
    assert pytest_step["exit_code"] == EXIT_FAILED
    extract_step = next(
        step for step in record["steps"] if step["name"] == "Extract controller JUnit artifact"
    )
    assert extract_step["exit_code"] == 1


def test_container_junit_extraction_fails_closed_on_empty_extracted_file(
    fake_repo: Path,
) -> None:
    class EmptyCpRunner(FakeCommandRunner):
        def _dispatch(self, argv: list[str]) -> CommandResult:
            if argv[:2] == ["docker", "cp"]:
                destination = Path(argv[3])
                destination.write_bytes(b"")  # empty artifact
                return CommandResult(0, "", "")
            return super()._dispatch(argv)

    exit_code, record, _ = run_harness(fake_repo, EmptyCpRunner(), RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["run"]["junit_artifact"] is None
    assert record["error"]["kind"] == "environment"


def test_container_junit_extraction_fails_closed_on_preexisting_destination(
    fake_repo: Path,
) -> None:
    orig_extract = LocalVerificationHarness._extract_container_junit

    def inject_preexisting(self_harness: Any, **kwargs: Any) -> None:
        host_junit_path = kwargs["host_junit_path"]
        host_junit_path.parent.mkdir(parents=True, exist_ok=True)
        host_junit_path.write_text("STALE", encoding="utf-8")
        orig_extract(self_harness, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LocalVerificationHarness, "_extract_container_junit", inject_preexisting)
        exit_code, record, _ = run_harness(
            fake_repo, FakeCommandRunner(), RunRequest(selection="recovery")
        )

    assert exit_code == EXIT_ENVIRONMENT
    assert record["error"]["kind"] == "environment"
    assert "pre-existing destination" in record["error"]["message"]


def test_container_junit_extraction_skipped_when_pytest_never_started(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner(controller_exit=125)
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_ENVIRONMENT
    assert record["run"]["outcome"] == "environment_error"
    assert record["run"]["junit_artifact"] is None
    cp_calls = [argv for argv in runner.argv_calls if argv[:2] == ["docker", "cp"]]
    assert len(cp_calls) == 0
    assert "Extract controller JUnit artifact" not in [s["name"] for s in record["steps"]]


def test_container_junit_extraction_attempted_on_collection_error(
    fake_repo: Path,
) -> None:
    class NoTestsRunner(FakeCommandRunner):
        def _dispatch(self, argv: list[str]) -> CommandResult:
            if argv[:3] == ["docker", "run", "--init"] and argv[-1] != CONTROLLER_PROBE_CODE:
                return CommandResult(5, "", "no tests ran\n")
            return super()._dispatch(argv)

    exit_code, record, _ = run_harness(fake_repo, NoTestsRunner(), RunRequest(selection="recovery"))

    assert exit_code == EXIT_CONFIGURATION
    assert record["run"]["outcome"] == "configuration_error"
    assert record["run"]["junit_artifact"] == "junit-focused-recovery-boundaries.xml"
    assert "Extract controller JUnit artifact" in [s["name"] for s in record["steps"]]


def test_controller_argv_projects_workspace_entries_without_binding_repository_root(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner()
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    argv = harness.controller_argv("controller", "postgres", IMAGE_ID, ["pytest"])

    assert f"{fake_repo}:{CONTROLLER_MOUNT_TARGET}" not in argv
    assert not any(arg.endswith(f":{CONTROLLER_MOUNT_TARGET}") for arg in argv)

    assert any(arg.endswith(":/workspace/Dockerfile.verify") for arg in argv)
    assert any(arg.endswith(":/workspace/pyproject.toml") for arg in argv)
    assert any(arg.endswith(":/workspace/uv.lock") for arg in argv)
    assert any(arg.endswith(":/workspace/.env:ro") for arg in argv)


def test_is_root_dotenv_secret_matches_variants_and_preserves_example() -> None:
    assert is_root_dotenv_secret(".env") is True
    assert is_root_dotenv_secret(".env.local") is True
    assert is_root_dotenv_secret(".env.test") is True
    assert is_root_dotenv_secret(".env.production") is True
    assert is_root_dotenv_secret(".env.secrets") is True
    assert is_root_dotenv_secret(".env_custom") is True
    assert is_root_dotenv_secret(".env-vault") is True
    # .env.example is the fixture that must be preserved
    assert is_root_dotenv_secret(".env.example") is False
    # Regular files/directories are not dotenv secrets
    assert is_root_dotenv_secret("uv.lock") is False
    assert is_root_dotenv_secret("apps") is False
    assert is_root_dotenv_secret(".gitignore") is False


def test_workspace_projection_membership_and_deterministic_order(fake_repo: Path) -> None:
    (fake_repo / "apps").mkdir()
    (fake_repo / "apps" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (fake_repo / "tests").mkdir()
    (fake_repo / "tests" / "test_sample.py").write_text("print(2)\n", encoding="utf-8")
    (fake_repo / "extra.txt").write_text("hello\n", encoding="utf-8")

    runner = FakeCommandRunner(
        ls_files=[
            "apps/main.py",
            "tests/test_sample.py",
            "Dockerfile.verify",
            "pyproject.toml",
            "uv.lock",
        ],
        git_status="?? extra.txt\0",
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    projection = harness.workspace_projection()

    container_targets = [cont for _, cont in projection]
    expected_order = [
        "/workspace/Dockerfile.verify",
        "/workspace/apps",
        "/workspace/extra.txt",
        "/workspace/pyproject.toml",
        "/workspace/tests",
        "/workspace/uv.lock",
    ]
    assert container_targets == expected_order

    for host_path, cont_path in projection:
        assert host_path.exists()
        top_name = cont_path.removeprefix("/workspace/")
        assert host_path == (fake_repo / top_name).resolve()


def test_workspace_projection_uses_worktree_rename_and_copy_destinations(
    fake_repo: Path,
) -> None:
    (fake_repo / "renamed.py").write_text("renamed\n", encoding="utf-8")
    (fake_repo / "copy_source.py").write_text("source\n", encoding="utf-8")
    (fake_repo / "copied.py").write_text("copied\n", encoding="utf-8")
    runner = FakeCommandRunner(
        ls_files=[
            "Dockerfile.verify",
            "pyproject.toml",
            "uv.lock",
            "rename_source.py",
            "copy_source.py",
        ],
        git_status=(" R renamed.py\0rename_source.py\0 C copied.py\0copy_source.py\0"),
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)

    targets = [container for _, container in harness.workspace_projection()]

    assert "/workspace/renamed.py" in targets
    assert "/workspace/rename_source.py" not in targets
    assert "/workspace/copy_source.py" in targets
    assert "/workspace/copied.py" in targets


def test_workspace_projection_excludes_dotenv_variants_git_and_ignored(fake_repo: Path) -> None:
    (fake_repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (fake_repo / ".env.local").write_text("LOCAL_SECRET=1\n", encoding="utf-8")
    (fake_repo / ".env.test").write_text("TEST_SECRET=1\n", encoding="utf-8")
    (fake_repo / ".env.example").write_text("FOO=bar\n", encoding="utf-8")
    (fake_repo / ".git").mkdir()
    (fake_repo / ".git" / "config").write_text("config\n", encoding="utf-8")
    (fake_repo / ".llm-output").mkdir()
    (fake_repo / ".llm-output" / "trace.txt").write_text("evidence\n", encoding="utf-8")
    (fake_repo / ".venv").mkdir()
    (fake_repo / ".venv" / "pyvenv.cfg").write_text("home = ...\n", encoding="utf-8")

    runner = FakeCommandRunner(
        ls_files=[
            "Dockerfile.verify",
            "pyproject.toml",
            "uv.lock",
            ".env.example",
        ],
        git_status="?? .env\0?? .env.local\0",
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    projection = harness.workspace_projection()

    container_targets = [cont for _, cont in projection]
    assert "/workspace/.env.example" in container_targets
    assert "/workspace/.env" not in container_targets
    assert "/workspace/.env.local" not in container_targets
    assert "/workspace/.env.test" not in container_targets
    assert not any(cont.startswith("/workspace/.git") for cont in container_targets)
    assert not any(cont.startswith("/workspace/.llm-output") for cont in container_targets)
    assert not any(cont.startswith("/workspace/.venv") for cont in container_targets)


def test_workspace_projection_fails_closed_on_missing_source_unexplained_by_deletion(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner(
        ls_files=[
            "Dockerfile.verify",
            "pyproject.toml",
            "uv.lock",
            "missing_file.py",
        ],
        git_status="",
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    with pytest.raises(
        SupervisorError, match="missing from the working tree and not explained by deletion"
    ):
        harness.workspace_projection()


def test_workspace_projection_allows_deletion_when_explained_by_git_status(
    fake_repo: Path,
) -> None:
    runner = FakeCommandRunner(
        ls_files=[
            "Dockerfile.verify",
            "pyproject.toml",
            "uv.lock",
            "deleted_file.py",
        ],
        git_status=" D deleted_file.py\0",
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    projection = harness.workspace_projection()
    container_targets = [cont for _, cont in projection]
    assert "/workspace/deleted_file.py" not in container_targets
    assert "/workspace/Dockerfile.verify" in container_targets


def test_workspace_projection_fails_closed_on_decode_mangled_path(fake_repo: Path) -> None:
    runner = FakeCommandRunner(
        ls_files=["Dockerfile.verify", "pyproject.toml", "uv.lock"],
        git_status="?? mangled_\\xff_path.py\0",
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    with pytest.raises(SupervisorError, match="decode-mangled"):
        harness.workspace_projection()


def test_workspace_projection_fails_closed_on_escaping_or_absolute_path(fake_repo: Path) -> None:
    runner = FakeCommandRunner(
        ls_files=["Dockerfile.verify", "pyproject.toml", "uv.lock"],
        git_status="?? ../escaping.py\0",
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    with pytest.raises(SupervisorError, match="absolute or escapes"):
        harness.workspace_projection()


def test_workspace_projection_symlink_validation(fake_repo: Path) -> None:
    # 1. Valid top-level symlink pointing inside repo to a candidate directory
    (fake_repo / "apps").mkdir()
    (fake_repo / "apps" / "main.py").write_text("print(1)\n", encoding="utf-8")
    symlink_dir = fake_repo / "apps_link"
    try:
        symlink_dir.symlink_to(fake_repo / "apps", target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")

    runner = FakeCommandRunner(
        ls_files=["Dockerfile.verify", "pyproject.toml", "uv.lock", "apps/main.py"],
        git_status="?? apps_link\0",
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    proj = harness.workspace_projection()
    assert any(cont == "/workspace/apps_link" for _, cont in proj)
    symlink_dir.unlink()

    # 2. Symlink escaping outside repository root
    symlink_esc = fake_repo / "outside_link"
    try:
        symlink_esc.symlink_to(fake_repo.parent, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")
    runner_esc = FakeCommandRunner(
        ls_files=["Dockerfile.verify", "pyproject.toml", "uv.lock", "apps/main.py"],
        git_status="?? outside_link\0",
    )
    harness_esc = LocalVerificationHarness(repo_root=fake_repo, runner=runner_esc)
    with pytest.raises(SupervisorError, match="resolves outside repository root"):
        harness_esc.workspace_projection()
    symlink_esc.unlink()

    # 3. Symlink resolving to excluded target (.git or .env)
    symlink_env = fake_repo / "env_link"
    (fake_repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    try:
        symlink_env.symlink_to(fake_repo / ".env")
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")
    runner_env = FakeCommandRunner(
        ls_files=["Dockerfile.verify", "pyproject.toml", "uv.lock", "apps/main.py"],
        git_status="?? env_link\0",
    )
    harness_env = LocalVerificationHarness(repo_root=fake_repo, runner=runner_env)
    with pytest.raises(SupervisorError, match="resolves outside candidate entries"):
        harness_env.workspace_projection()


def test_workspace_projection_symlink_validation_mocked(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_repo / "apps").mkdir()
    (fake_repo / "apps" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (fake_repo / "link_entry").write_text("", encoding="utf-8")

    runner = FakeCommandRunner(
        ls_files=["Dockerfile.verify", "pyproject.toml", "uv.lock", "apps/main.py"],
        git_status="?? link_entry\0",
    )
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)

    target_link = fake_repo / "link_entry"
    orig_is_symlink = Path.is_symlink
    orig_resolve = Path.resolve

    # 1. Valid symlink pointing to candidate dir
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: True if self == target_link else orig_is_symlink(self),
    )
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *a, **kw: (
            (fake_repo / "apps") if self == target_link else orig_resolve(self, *a, **kw)
        ),
    )
    proj = harness.workspace_projection()
    assert any(cont == "/workspace/link_entry" for _, cont in proj)

    # 2. Symlink resolving to the repository root
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *a, **kw: fake_repo if self == target_link else orig_resolve(self, *a, **kw),
    )
    with pytest.raises(SupervisorError, match="resolves to the repository root"):
        harness.workspace_projection()

    # 3. Symlink resolving outside repo
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *a, **kw: (
            (fake_repo.parent / "outside") if self == target_link else orig_resolve(self, *a, **kw)
        ),
    )
    with pytest.raises(SupervisorError, match="resolves outside repository root"):
        harness.workspace_projection()

    # 4. Symlink resolving to excluded target (.env)
    (fake_repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *a, **kw: (
            (fake_repo / ".env") if self == target_link else orig_resolve(self, *a, **kw)
        ),
    )
    with pytest.raises(SupervisorError, match="resolves outside candidate entries"):
        harness.workspace_projection()

    # 5. Symlink pointing to non-existent target within candidate
    non_existent = fake_repo / "apps" / "does_not_exist.py"
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *a, **kw: (
            non_existent if self == target_link else orig_resolve(self, *a, **kw)
        ),
    )
    with pytest.raises(SupervisorError, match="points to non-existent target"):
        harness.workspace_projection()

    # 6. Symlink pointing to a special file within a candidate directory
    special = fake_repo / "apps" / "special"
    orig_exists = Path.exists
    orig_is_file = Path.is_file
    orig_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *a, **kw: special if self == target_link else orig_resolve(self, *a, **kw),
    )
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: True if self == special else orig_exists(self),
    )
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda self: False if self == special else orig_is_file(self),
    )
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda self: False if self == special else orig_is_dir(self),
    )
    with pytest.raises(SupervisorError, match="not a regular file or directory"):
        harness.workspace_projection()


def test_container_structured_evidence_records_projected_mounts(fake_repo: Path) -> None:
    runner = FakeCommandRunner()
    exit_code, record, _ = run_harness(fake_repo, runner, RunRequest(selection="recovery"))

    assert exit_code == EXIT_PASSED

    # Examine all container steps (probe and pytest)
    container_steps = [step for step in record["steps"] if "container" in step]
    assert len(container_steps) == 2  # probe and pytest
    for step in container_steps:
        container_meta = step["container"]
        # Falsely claiming repo_root itself was mounted is prohibited
        assert "mount" not in container_meta
        assert not any(
            m == f"{fake_repo}:{CONTROLLER_MOUNT_TARGET}" for m in container_meta.get("mounts", [])
        )

        # Honest projected mounts are recorded
        mounts = container_meta["mounts"]
        assert any(m.endswith(":/workspace/Dockerfile.verify") for m in mounts)
        assert any(m.endswith(":/workspace/pyproject.toml") for m in mounts)
        assert any(m.endswith(":/workspace/uv.lock") for m in mounts)
        assert any(m.endswith(":/workspace/.env:ro") for m in mounts)

        # Network is recorded
        assert "network" in container_meta


def test_container_reuses_one_projection_for_probe_and_pytest(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeCommandRunner()
    harness = LocalVerificationHarness(repo_root=fake_repo, runner=runner)
    projection = [
        (fake_repo / "Dockerfile.verify", "/workspace/Dockerfile.verify"),
        (fake_repo / "pyproject.toml", "/workspace/pyproject.toml"),
        (fake_repo / "uv.lock", "/workspace/uv.lock"),
    ]
    calls = 0

    def projection_once() -> list[tuple[Path, str]]:
        nonlocal calls
        calls += 1
        if calls > 1:
            pytest.fail("workspace projection was recomputed during one container run")
        return projection

    monkeypatch.setattr(harness, "workspace_projection", projection_once)

    exit_code = harness.run_selection(RunRequest(selection="recovery"))

    assert exit_code == EXIT_PASSED
    assert calls == 1
