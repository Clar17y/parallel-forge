from __future__ import annotations

import ast
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from forge.application.ports.runner import RunCommandRequest
from forge.application.ports.worktrees import DatabaseBinding
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.observability.redaction import Redactor
from forge.tools.docker import DockerRunner
from forge.tools.environment import EnvironmentStager
from forge.tools.git import ControlledGit
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner
from forge.tools.worktree_runner import WorktreeRunnerFactory

pytestmark = pytest.mark.docker


def _docker_or_skip() -> str:
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("Docker CLI is required in CI for the runner smoke")
        pytest.skip("Docker CLI is unavailable outside CI")
    probe = subprocess.run(
        [docker, "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    if probe.returncode != 0:
        if os.environ.get("CI"):
            pytest.fail("Docker daemon is required in CI for the runner smoke")
        pytest.skip("Docker daemon is unavailable outside CI")
    return docker


def test_mount_guard_rejects_wrong_inode_before_loader_or_command_execution(tmp_path: Path) -> None:
    docker = _docker_or_skip()
    repository = Path(__file__).resolve().parents[2]
    image_id = subprocess.check_output(
        [
            docker,
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            "Dockerfile.runner",
            "-q",
            str(repository),
        ],
        text=True,
        timeout=600,
    ).strip()
    expected, substituted = tmp_path / "expected", tmp_path / "substituted"
    for directory in (expected, substituted):
        directory.mkdir()
        directory.chmod(0o777)

    def run(
        directory: Path,
        *arguments: str,
        entrypoint: str | None = None,
        environment: tuple[str, ...] = (),
        seccomp: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            docker,
            "run",
            "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=10001:10001",
            "--workdir=/",
            "--mount",
            f"type=bind,src={directory},dst=/workspace,bind-recursive=disabled",
        ]
        if entrypoint:
            argv.extend(("--entrypoint", entrypoint))
        if seccomp is not None:
            argv.extend(("--security-opt", f"seccomp={seccomp}"))
        for value in environment:
            argv.extend(("--env", value))
        return subprocess.run(
            [*argv, image_id, *arguments], capture_output=True, text=True, timeout=30, check=False
        )

    identity = run(
        expected,
        "python",
        "-I",
        "-S",
        "-c",
        "import os; s=os.stat('/workspace'); "
        "print(open('/proc/sys/kernel/random/boot_id').read().strip(), s.st_dev, s.st_ino, s.st_uid)",
    )
    assert identity.returncode == 0, identity.stderr
    boot_id, device, inode, host_uid = identity.stdout.split()
    guard = "/usr/local/bin/forge-mount-guard"
    command = ("python", "-I", "-S", "-c", "open('command-ran', 'w').write('allowed')")
    accepted = run(expected, boot_id, device, inode, *command, entrypoint=guard)
    assert accepted.returncode == 0, accepted.stderr
    assert (expected / "command-ran").read_text() == "allowed"

    denied_syscall = tmp_path / "deny-recovery.json"
    denied_syscall.write_text(
        json.dumps(
            {
                "defaultAction": "SCMP_ACT_ALLOW",
                "syscalls": [{"names": ["fchmodat2"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1}],
            }
        ),
        encoding="utf-8",
    )
    unsupported = run(
        expected,
        boot_id,
        device,
        inode,
        "python",
        "-I",
        "-S",
        "-c",
        "open('unsupported-ran', 'w').write('unsafe')",
        entrypoint=guard,
        seccomp=denied_syscall,
    )
    assert unsupported.returncode == 126, unsupported.stderr
    assert "access-repair-unsupported" in unsupported.stderr
    assert not (expected / "unsupported-ran").exists()

    if os.name != "nt":
        damaged = run(
            expected,
            boot_id,
            device,
            inode,
            "sh",
            "-c",
            "mkdir -p damaged/nested && touch damaged/nested/output && chmod 000 damaged/nested damaged",
            entrypoint=guard,
        )
        assert damaged.returncode == 0, damaged.stderr
        repaired = run(expected, boot_id, device, inode, "--repair", host_uid, entrypoint=guard)
        assert repaired.returncode == 0, repaired.stderr
        readable = run(
            expected,
            boot_id,
            device,
            inode,
            "sh",
            "-c",
            "test -r damaged/nested/output",
            entrypoint=guard,
        )
        assert readable.returncode == 0, readable.stderr

    # Exercise actual Linux inode ownership and ACL syscalls even on Docker
    # Desktop, whose Windows bind mounts do not preserve POSIX ownership.
    native_repair = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network=none",
            "--user=0:0",
            image_id,
            "python",
            "-I",
            "-S",
            "-c",
            """
import os, subprocess
os.makedirs('/workspace', mode=0o777, exist_ok=True)
os.chmod('/workspace', 0o777)
os.chown('/workspace', 1000, 1000)
s = os.stat('/workspace')
identity = [open('/proc/sys/kernel/random/boot_id').read().strip(), str(s.st_dev), str(s.st_ino)]
create = subprocess.run(['python', '-I', '-S', '-c',
    "import os; os.makedirs('/workspace/damaged/nested'); os.symlink('/outside-never-opened', '/workspace/tool-link'); open('/workspace/damaged/nested/output','w').write('proof'); os.chmod('/workspace/damaged/nested/output',0); os.chmod('/workspace/damaged/nested',0); os.chmod('/workspace/damaged',0)"],
    user=10001, group=10001, check=True)
subprocess.run(['/usr/local/bin/forge-mount-guard', *identity, '--repair', '1000'],
    user=10001, group=10001, check=True)
subprocess.run(['python', '-I', '-S', '-c',
    "assert open('/workspace/damaged/nested/output').read() == 'proof'"],
    user=1000, group=1000, check=True)
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert native_repair.returncode == 0, native_repair.stderr

    # Execute the exact ACL helper source against the Linux kernel, with only
    # its exception type supplied so this stdlib-only runner needs no app install.
    source = (repository / "apps/orchestrator/src/forge/tools/paths.py").read_text()
    acl_source = "\n\n".join(
        ast.get_source_segment(source, node) or ""
        for node in ast.parse(source).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in {"_DockerAcl", "_read_posix_acl", "_write_posix_acl"}
    )
    acl_probe = (
        "import os, stat, struct, errno\nfrom collections.abc import Callable\n"
        "from typing import cast\nRepositoryAccessDenied = RuntimeError\n"
        + acl_source
        + """
os.mkdir('/tmp/acl-probe', 0o700)
fd = os.open('/tmp/acl-probe', os.O_RDONLY | os.O_DIRECTORY)
acl = _DockerAcl()
before = acl.snapshot(fd)
granted = acl.grant(fd, 7)
assert os.getxattr(fd, 'system.posix_acl_access') == granted
os.setxattr(fd, 'system.posix_acl_default', granted)
assert acl.grant(fd, 7) == granted
acl.restore(fd, before)
assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o700
assert 'system.posix_acl_access' not in os.listxattr(fd)
os.close(fd)
"""
    )
    native_acl = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--interactive",
            "--network=none",
            "--user=0:0",
            image_id,
            "python",
            "-I",
            "-S",
            "-",
        ],
        input=acl_probe,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert native_acl.returncode == 0, native_acl.stderr

    # A dynamic launcher would load this constructor before reaching its guard.
    (substituted / "preload.c").write_text(
        "#include <fcntl.h>\n#include <unistd.h>\n"
        "__attribute__((constructor)) static void injected(void) {"
        'int fd=open("/workspace/loader-ran",O_CREAT|O_WRONLY,0600);'
        "if(fd>=0)close(fd);}\n",
        encoding="utf-8",
    )
    compiled = run(
        substituted, "cc", "-shared", "-fPIC", "/workspace/preload.c", "-o", "/workspace/preload.so"
    )
    assert compiled.returncode == 0, compiled.stderr
    rejected = run(
        substituted,
        boot_id,
        device,
        inode,
        *command,
        entrypoint=guard,
        environment=("LD_PRELOAD=/workspace/preload.so", "PYTHONPATH=/workspace"),
    )
    assert rejected.returncode == 126, rejected.stderr
    assert rejected.stderr == "Forge runner mount identity rejected: directory-identity\n"
    assert not (substituted / "command-ran").exists()
    assert not (substituted / "loader-ran").exists()
    (expected / "command-ran").unlink()
    foreign_kernel = run(
        expected, "00000000-0000-0000-0000-000000000000", device, inode, *command, entrypoint=guard
    )
    assert foreign_kernel.returncode == 126
    assert foreign_kernel.stderr == "Forge runner mount identity rejected: kernel-identity\n"
    assert not (expected / "command-ran").exists()
    missing_command = run(
        expected, boot_id, device, inode, "forge-missing-command", entrypoint=guard
    )
    assert missing_command.returncode == 126
    assert missing_command.stderr == "Forge runner mount identity rejected: command-exec\n"
    headers = run(expected, "readelf", "-l", guard)
    assert headers.returncode == 0, headers.stderr
    assert "INTERP" not in headers.stdout


def test_runner_image_is_pinned_nonroot_and_has_required_tool_versions(tmp_path: Path) -> None:
    docker = _docker_or_skip()
    repository = Path(__file__).resolve().parents[2]
    image_id = subprocess.check_output(
        [
            docker,
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            "Dockerfile.runner",
            "-q",
            str(repository),
        ],
        text=True,
        timeout=600,
    ).strip()
    assert image_id.startswith("sha256:")
    smoke_command = " ".join(  # noqa: FLY002 - keep the shell probe readable and bounded
        (
            "python --version && node --version && npm --version && git --version",
            "&& rg --version | head -1 && cc --version | head -1 && id -u",
            "&& ! command -v docker",
        )
    )
    output = subprocess.check_output(
        [
            docker,
            "run",
            "--rm",
            "--network=none",
            image_id,
            "sh",
            "-c",
            smoke_command,
        ],
        text=True,
        timeout=30,
    )
    lines = output.splitlines()
    assert lines[0].startswith("Python 3.14")
    assert lines[1].startswith("v24.")
    assert lines[2]
    assert lines[3].startswith("git version")
    assert lines[4].startswith("ripgrep ")
    assert lines[5]
    assert lines[6] == "10001"

    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = CommandSpec(
        kind=StepKind.TEST,
        name="python-version",
        argv=("python", "--version"),
        timeout_seconds=30,
    )
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="local/runner-smoke",
        default_branch="main",
        runner_mode=RunnerMode.DOCKER,
        commands=(command,),
    )
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    runner = DockerRunner(
        policy=policy,
        root=CanonicalRoot(worktree),
        image_digest=image_id,
        artifact_store=artifact_store,
    )
    result = asyncio.run(
        runner.run(
            RunCommandRequest(command_name=command.name, kind=command.kind),
        )
    )
    stdout = json.loads(asyncio.run(artifact_store.open_bytes(result.stdout_digest)))
    assert result.exit_code == 0
    assert result.runner_mode is RunnerMode.DOCKER
    assert result.unsandboxed is False
    assert result.image_digest == image_id
    assert stdout["text"].startswith("Python 3.14")


def test_bound_runner_reads_e2a_staged_file_as_fixed_container_user(tmp_path: Path) -> None:
    docker = _docker_or_skip()
    project_root = Path(__file__).resolve().parents[2]
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Forge Test"),
        ("config", "user.email", "forge@example.test"),
    ):
        result = subprocess.run(
            [(shutil.which("git") or "git"), "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    (repository / "README.md").write_text("forge\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    (repository / "config").mkdir()
    (repository / "config" / ".keep").write_text("\n", encoding="utf-8")
    if os.name != "nt":
        (repository / "tool").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (repository / "tool").chmod(0o755)
    (repository / "tests").mkdir()
    (repository / "tests" / "e2b_reader.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "assert Path.cwd() == Path('/workspace')\n"
        "assert os.getuid() == 10001\n"
        "assert Path('/workspace/.git').is_file()\n"
        "assert Path('config/local.env').read_text(encoding='utf-8').strip() == 'FORGE_SECRET=secret-value'\n"
        + (
            "import subprocess\nsubprocess.run(['./tool'], check=True)\n"
            "Path('outputs').mkdir(exist_ok=True)\n"
            "for number in range(4200):\n"
            "    Path('outputs', str(number)).write_text('output')\n"
            if os.name != "nt"
            else ""
        )
        + "print('FORGE_E2B_OK')\n",
        encoding="utf-8",
    )
    git = shutil.which("git") or "git"
    for arguments in (("add", "."), ("commit", "-m", "initial")):
        result = subprocess.run(
            [git, "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    base_sha = subprocess.check_output(
        [git, "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()

    project_id = uuid4()
    run_id = uuid4()
    identity = WorktreeIdentity.for_run(project_id, run_id, "feature/e2b-smoke", False)
    root = CanonicalRoot(repository)
    controlled = ControlledGit(
        root,
        default_branch="main",
        state_root=tmp_path / "forge-state",
        git_executable=git,
    )
    worktree = controlled.create_worktree(identity, base_sha)
    (repository / "config" / "local.env").write_text(
        "FORGE_SECRET=secret-value\n", encoding="utf-8"
    )
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(repository.resolve()),
        github_repository="local/e2b-smoke",
        default_branch="main",
        runner_mode=RunnerMode.DOCKER,
        allowed_environment_files=("config/local.env",),
        commands=(
            CommandSpec(
                kind=StepKind.TEST,
                name="e2b-reader",
                argv=("python", "tests/e2b_reader.py"),
                timeout_seconds=60,
            ),
        ),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(
        worktree,
        policy,
        DatabaseBinding(state=ResourceState.DISABLED),
    )
    stager.publish(worktree, policy, plan)

    image_id = subprocess.check_output(
        [
            docker,
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            str(project_root / "Dockerfile.runner"),
            "-q",
            str(project_root),
        ],
        text=True,
        timeout=600,
    ).strip()
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")

    class DiagnosticProcessRunner:
        """Expose bounded startup diagnostics while retaining the real runner."""

        def run_argv(self, argv: Any, **kwargs: Any) -> Any:
            result = ProcessRunner(root).run_argv(argv, **kwargs)
            if result.return_code == 125:
                print(
                    "Docker startup diagnostic:",
                    Redactor(secrets=("secret-value",)).redact(result.stderr),
                )
            return result

    runner = WorktreeRunnerFactory(
        controlled,
        image_digest=image_id,
        artifact_store=artifacts,
        process_runner=DiagnosticProcessRunner(),
    ).create(worktree, policy)
    terminal = asyncio.run(
        runner.run_terminal(RunCommandRequest(command_name="e2b-reader", kind=StepKind.TEST))
    )
    stdout_bytes = asyncio.run(artifacts.open_bytes(terminal.result.stdout_digest))
    stderr_bytes = asyncio.run(artifacts.open_bytes(terminal.result.stderr_digest))
    stdout = json.loads(stdout_bytes)
    stderr = json.loads(stderr_bytes)
    assert terminal.result.exit_code == 0, stderr["text"]
    assert terminal.result.runner_mode is RunnerMode.DOCKER
    assert terminal.result.unsandboxed is False
    assert stdout["text"].strip() == "FORGE_E2B_OK"
    assert stderr["text"] == ""
    assert "secret-value" not in repr(terminal)
    assert b"secret-value" not in stdout_bytes

    if os.name != "nt":
        # Existing container-owned output trees must not consume one retained
        # host descriptor per entry on the next command.
        repeated = asyncio.run(
            runner.run_terminal(RunCommandRequest(command_name="e2b-reader", kind=StepKind.TEST))
        )
        assert repeated.result.exit_code == 0
