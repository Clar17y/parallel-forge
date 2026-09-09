import pytest
from forge.release.git_adoption import ManagedBaseAdoption

from apps.orchestrator.tests.release.test_git_push import setup_push
from apps.orchestrator.tests.tools.test_git_adoption import output


async def test_exact_fetch_does_not_update_refs_or_expose_credentials(tmp_path):
    _, tree, runner, policy, push = setup_push(tmp_path)
    calls = []
    original = runner.run_argv

    def capture(argv, **kwargs):
        if "fetch" in argv:
            calls.append((list(argv), dict(kwargs["environment"])))
            # Offline fixture: objects already exist; substitute only this transfer.
            from forge.application.ports.repository import ProcessResult

            return ProcessResult(
                return_code=0,
                stdout="",
                stderr="",
                timed_out=False,
                stdout_original_byte_count=0,
                stderr_original_byte_count=0,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        return original(argv, **kwargs)

    runner.run_argv = capture
    adopted = []
    push._git.adopt_head = lambda *args: adopted.append(args)
    adapter = ManagedBaseAdoption(push._git, push._credentials, "env://TOKEN")
    await adapter.adopt(tree, policy, tree.base_sha, "c" * 40, "b" * 40)
    assert len(calls) == 1 and len(adopted) == 1
    argv, env = calls[0]
    assert argv[argv.index("fetch") :] == [
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--no-auto-maintenance",
        "--refmap=",
        "origin",
        "c" * 40,
    ]
    assert "test-token" not in repr(argv)
    assert any("Authorization: Basic" in value for value in env.values())
    assert output(tree.path, "rev-parse", "HEAD") == tree.base_sha


async def test_fetch_rejects_changed_origin_before_network(tmp_path):
    repository, tree, _runner, policy, push = setup_push(tmp_path)
    from forge.release.git_adoption import ManagedAdoptionError

    from apps.orchestrator.tests.tools.test_git import _git

    _git(repository, "remote", "set-url", "origin", "https://example.test/repo")
    original = push._git._run

    def no_fetch(path, args, **kwargs):
        assert args[0] != "fetch"
        return original(path, args, **kwargs)

    push._git._run = no_fetch
    adapter = ManagedBaseAdoption(push._git, push._credentials, "env://TOKEN")
    with pytest.raises(ManagedAdoptionError):
        await adapter.adopt(tree, policy, tree.base_sha, "c" * 40, "b" * 40)
    assert output(tree.path, "rev-parse", "HEAD") == tree.base_sha


async def test_real_fetch_and_adoption_replay_keeps_remote_refs_unchanged(tmp_path):
    from forge.domain.policy import ProjectPolicy

    from apps.orchestrator.tests.release.test_git_push import Credentials
    from apps.orchestrator.tests.tools.test_git import _git
    from apps.orchestrator.tests.tools.test_git_adoption import candidate

    git, tree, previous, base, new = candidate(tmp_path)
    repository = tmp_path / "repository"
    _git(repository, "remote", "add", "origin", "https://github.com/owner/project.git")
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare")
    _git(repository, "push", str(bare), f"{new}:refs/heads/feat")
    policy = ProjectPolicy(
        id=tree.identity.project_id,
        version=1,
        repository_path=str(repository),
        github_repository="owner/project",
        default_branch="main",
    )
    original = git._runner.run_argv
    calls = []

    def offline(argv, *, cwd, environment, **kwargs):
        if "fetch" not in argv:
            return original(argv, cwd=cwd, environment=environment, **kwargs)
        calls.append(tuple(argv))
        # The only test transport substitution; production permits HTTPS only.
        command = list(argv)
        command[command.index("origin")] = str(bare)
        env = dict(environment)
        index = int(env["GIT_CONFIG_COUNT"])
        env.update(
            {
                "GIT_CONFIG_COUNT": str(index + 1),
                f"GIT_CONFIG_KEY_{index}": "protocol.file.allow",
                f"GIT_CONFIG_VALUE_{index}": "always",
            }
        )
        return original(command, cwd=cwd, environment=env, **kwargs)

    git._runner.run_argv = offline
    remote_refs = output(bare, "show-ref")
    adapter = ManagedBaseAdoption(git, Credentials(), "env://TOKEN")
    from forge.release.git_adoption import ManagedAdoptionError

    with pytest.raises(ManagedAdoptionError):
        await adapter.inspect(tree, policy, previous, new, base)
    assert git.head_sha(tree) == previous and calls == []
    for _ in range(2):
        await adapter.adopt(tree, policy, previous, new, base)

    async def no_credentials(*args):
        raise AssertionError("inspection must not resolve credentials")

    adapter._credentials.resolve = no_credentials
    await adapter.inspect(tree, policy, previous, new, base)
    assert git.head_sha(tree) == new
    assert len(calls) == 1
    assert output(bare, "show-ref") == remote_refs
    assert not (repository / ".git" / "FETCH_HEAD").exists()
    assert output(repository, "rev-parse", "main") == base
