from __future__ import annotations

import asyncio
import base64
import subprocess
from pathlib import Path

import pytest
from forge.application.ports.repository import ProcessResult
from forge.domain.policy import ProjectPolicy
from forge.release.git_push import ManagedPush, ManagedPushError
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner

from apps.orchestrator.tests.tools.test_git import _controlled, _git, _managed_repository


class Credentials:
    async def resolve(self, reference: str) -> str:
        return "test-token-never-log"


class OfflinePushRunner(ProcessRunner):
    def __init__(self, root: CanonicalRoot) -> None:
        super().__init__(root)
        self.pushes: list[tuple[list[str], dict[str, str]]] = []
        self.fail = False
        self.bare_remote: Path | None = None

    def run_argv(self, argv, *, cwd, environment, timeout_seconds=None):
        if "push" not in argv:
            return super().run_argv(
                argv, cwd=cwd, environment=environment, timeout_seconds=timeout_seconds
            )
        self.pushes.append((list(argv), dict(environment)))
        if self.bare_remote is not None:
            # Substitute only the transport in this offline fixture. Production
            # has no configurable URL or file-protocol escape hatch.
            command = list(argv)
            command[command.index("origin")] = str(self.bare_remote)
            local_env = dict(environment)
            index = int(local_env["GIT_CONFIG_COUNT"])
            local_env.update(
                {
                    "GIT_CONFIG_COUNT": str(index + 1),
                    f"GIT_CONFIG_KEY_{index}": "protocol.file.allow",
                    f"GIT_CONFIG_VALUE_{index}": "always",
                }
            )
            return super().run_argv(command, cwd=cwd, environment=local_env)
        return ProcessResult(
            return_code=1 if self.fail else 0,
            stdout="",
            stderr="test-token-never-log" if self.fail else "",
            timed_out=False,
            stdout_original_byte_count=0,
            stderr_original_byte_count=19 if self.fail else 0,
            stdout_truncated=False,
            stderr_truncated=False,
        )


def setup_push(tmp_path: Path):
    repository, identity, worktree = _managed_repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://github.com/owner/project.git")
    runner = OfflinePushRunner(CanonicalRoot(repository))
    git = _controlled(repository, tmp_path / "state", runner)
    policy = ProjectPolicy(
        id=identity.project_id,
        version=1,
        repository_path=str(repository),
        github_repository="owner/project",
        default_branch="main",
    )
    return repository, worktree, runner, policy, ManagedPush(git, Credentials(), "env://TOKEN")


async def test_push_uses_only_approved_sha_and_scoped_ephemeral_auth(tmp_path):
    _, worktree, runner, policy, push = setup_push(tmp_path)
    await push.push(worktree, policy, worktree.base_sha)
    assert len(runner.pushes) == 1
    argv, env = runner.pushes[0]
    assert argv[argv.index("push") :] == [
        "push",
        "--porcelain",
        "--no-follow-tags",
        "--recurse-submodules=no",
        "origin",
        f"{worktree.base_sha}:refs/heads/feat",
    ]
    assert "test-token-never-log" not in repr(argv)
    config = {
        env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"]
        for n in range(int(env["GIT_CONFIG_COUNT"]))
    }
    expected = base64.b64encode(b"x-access-token:test-token-never-log").decode()
    assert config["http.https://github.com/owner/project.git.extraHeader"] == (
        f"Authorization: Basic {expected}"
    )
    assert config["http.followRedirects"] == "false"
    assert config["push.followTags"] == "false"


async def test_stale_head_never_reaches_push(tmp_path):
    _, worktree, runner, policy, push = setup_push(tmp_path)
    with pytest.raises(ManagedPushError):
        await push.push(worktree, policy, "a" * 40)
    assert not runner.pushes


@pytest.mark.parametrize(
    "key,value",
    [
        ("remote.origin.pushurl", "https://evil.test/repo"),
        ("remote.origin.mirror", "true"),
        ("url.https://evil.test/.insteadOf", "https://github.com/"),
        ("http.extraHeader", "Authorization: bad"),
        ("push.followTags", "true"),
        ("core.sshCommand", "malicious"),
        ("remote.origin.receivepack", "malicious"),
    ],
)
async def test_repository_network_configuration_cannot_override_push(tmp_path, key, value):
    repository, worktree, runner, policy, push = setup_push(tmp_path)
    _git(repository, "config", key, value)
    with pytest.raises(ManagedPushError):
        await push.push(worktree, policy, worktree.base_sha)
    assert not runner.pushes


async def test_remote_change_and_failure_are_redacted_without_retry(tmp_path):
    repository, worktree, runner, policy, push = setup_push(tmp_path)
    runner.fail = True
    with pytest.raises(ManagedPushError) as failure:
        await push.push(worktree, policy, worktree.base_sha)
    assert "test-token" not in str(failure.value)
    assert failure.value.__suppress_context__
    assert len(runner.pushes) == 1
    _git(repository, "remote", "set-url", "origin", "https://github.com/other/project.git")
    with pytest.raises(ManagedPushError):
        await push.push(worktree, policy, worktree.base_sha)
    assert len(runner.pushes) == 1


async def test_real_git_push_does_not_publish_tags_or_overwrite_diverged_branch(tmp_path):
    repository, worktree, runner, policy, push = setup_push(tmp_path)
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare")
    runner.bare_remote = bare
    _git(repository, "tag", "-a", "private-tag", "-m", "must stay local")
    await push.push(worktree, policy, worktree.base_sha)
    result = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert result.stdout.splitlines() == ["refs/heads/feat"]
    (repository / "upstream.txt").write_text("remote advanced\n")
    _git(repository, "add", "upstream.txt")
    _git(repository, "commit", "-m", "upstream")
    _git(repository, "push", str(bare), "HEAD:refs/heads/feat")
    (worktree.path / "candidate.txt").write_text("candidate\n")
    _git(worktree.path, "add", "candidate.txt")
    _git(worktree.path, "commit", "-m", "candidate")
    candidate = push._git.head_sha(worktree)
    with pytest.raises(ManagedPushError):
        await push.push(worktree, policy, candidate)
    observed = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(bare), "rev-parse", "refs/heads/feat"],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert observed.stdout.strip() != candidate
