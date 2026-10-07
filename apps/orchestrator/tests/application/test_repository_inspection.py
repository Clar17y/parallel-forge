from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from forge.application.adapters.git import LocalGitRepositoryInspector, RepositoryInspectionError


def _success_runner(repository: Path, *, remote: str = "https://github.com/Owner/Repo.git"):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{repository}\n", "")
        if "config" in argv and "--local" in argv and "--get" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{remote}\n", "")
        if "check-ref-format" in argv:
            return subprocess.CompletedProcess(argv, 0, "main\n", "")
        if "--verify" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{'a' * 40}\n", "")
        raise AssertionError(argv)

    return run, calls


def test_inspection_returns_exact_git_identity_and_uses_bounded_noninteractive_argv(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    data_root = tmp_path / "data"
    repository.mkdir()
    (repository / ".git").mkdir()
    data_root.mkdir()
    runner, calls = _success_runner(repository)

    result = LocalGitRepositoryInspector(runner=runner).inspect(
        repository_path=str(repository),
        data_root=str(data_root),
        github_repository="owner/repo",
        default_branch="main",
    )

    assert result.canonical_path == str(repository.resolve())
    assert result.github_repository == "owner/repo"
    assert result.default_branch == "main"
    assert result.base_ref == "refs/heads/main"
    assert result.base_sha == "a" * 40
    assert calls
    for argv, kwargs in calls:
        assert isinstance(argv, list)
        assert argv[0] == "git"
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 10
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("relation", ["equal", "repo_contains_data", "data_contains_repo"])
def test_inspection_rejects_repository_data_root_overlap(tmp_path: Path, relation: str) -> None:
    if relation == "equal":
        repository = data_root = tmp_path / "same"
    elif relation == "repo_contains_data":
        repository = tmp_path / "repo"
        data_root = repository / "data"
    else:
        data_root = tmp_path / "data"
        repository = data_root / "repo"
    repository.mkdir(parents=True)
    data_root.mkdir(parents=True, exist_ok=True)
    (repository / ".git").mkdir()

    with pytest.raises(RepositoryInspectionError, match="repository validation failed"):
        LocalGitRepositoryInspector().inspect(
            repository_path=str(repository),
            data_root=str(data_root),
            github_repository="owner/repo",
            default_branch="main",
        )


def test_inspection_rejects_subdirectory_registration_and_remote_mismatch(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    child = repository / "src"
    child.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()

    def subdir_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{repository}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(RepositoryInspectionError):
        LocalGitRepositoryInspector(runner=subdir_runner).inspect(
            repository_path=str(child),
            data_root=str(data_root),
            github_repository="owner/repo",
            default_branch="main",
        )

    runner, _ = _success_runner(repository, remote="https://github.com/other/repo.git")
    with pytest.raises(RepositoryInspectionError):
        LocalGitRepositoryInspector(runner=runner).inspect(
            repository_path=str(repository),
            data_root=str(data_root),
            github_repository="owner/repo",
            default_branch="main",
        )


def test_inspection_rejects_non_git_and_invalid_commit_without_echoing_git_output(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()

    def failed_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 128, "", "PRIVATE / secret stderr")

    with pytest.raises(RepositoryInspectionError) as error:
        LocalGitRepositoryInspector(runner=failed_runner).inspect(
            repository_path=str(repository),
            data_root=str(data_root),
            github_repository="owner/repo",
            default_branch="main",
        )
    assert "PRIVATE" not in str(error.value)
    assert "secret" not in str(error.value)


def test_inspection_rejects_symlinked_component_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("symlinks are not available on this host")
    data_root = tmp_path / "data"
    data_root.mkdir()

    with pytest.raises(RepositoryInspectionError):
        LocalGitRepositoryInspector().inspect(
            repository_path=str(linked),
            data_root=str(data_root),
            github_repository="owner/repo",
            default_branch="main",
        )


@pytest.mark.integration
def test_inspection_reads_only_local_git_origin_and_resolves_real_default_branch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repository = tmp_path / "repo"
    data_root = tmp_path / "data"
    repository.mkdir()
    data_root.mkdir()
    commands = [
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "forge@example.test"],
        ["git", "config", "user.name", "Forge Test"],
        ["git", "config", "remote.origin.url", "git@github.com:Owner/Repo.git"],
    ]
    for command in commands:
        subprocess.run(command, cwd=repository, check=True, capture_output=True, shell=False)
    (repository / "README.md").write_text("forge\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True, shell=False)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repository,
        check=True,
        capture_output=True,
        shell=False,
    )

    result = LocalGitRepositoryInspector().inspect(
        repository_path=str(repository),
        data_root=str(data_root),
        github_repository="owner/repo",
        default_branch="main",
    )

    assert result.github_repository == "owner/repo"
    assert result.base_ref == "refs/heads/main"
    assert len(result.base_sha) == 40
    assert result.base_sha == result.base_sha.lower()


@pytest.mark.integration
def test_linked_worktree_proves_complete_history_and_rejects_metadata_overlays(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    linked = tmp_path / "linked"
    data_root = tmp_path / "data"
    repository.mkdir()
    data_root.mkdir()

    def git(path: Path, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    git(repository, "init", "-b", "main")
    git(repository, "config", "user.email", "forge@example.test")
    git(repository, "config", "user.name", "Forge Test")
    git(repository, "remote", "add", "origin", "https://github.com/Owner/Repo.git")
    (repository / "README.md").write_text("base", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "base")
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "worktree", "add", "-b", "linked", str(linked), "main")
    inspector = LocalGitRepositoryInspector()
    arguments = {
        "repository_path": str(linked), "data_root": str(data_root),
        "github_repository": "owner/repo", "default_branch": "linked",
        "base_sha": base, "merge_sha": base,
    }
    assert inspector.proves_integration(**arguments)
    common = Path(git(linked, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = linked / common
    common = common.resolve()
    per_worktree = Path(git(linked, "rev-parse", "--git-dir"))
    if not per_worktree.is_absolute():
        per_worktree = linked / per_worktree
    per_worktree = per_worktree.resolve()
    for metadata in (common, per_worktree):
        for overlay in (metadata / "shallow", metadata / "info" / "grafts"):
            overlay.parent.mkdir(exist_ok=True)
            overlay.write_text(base + "\n", encoding="utf-8")
            with pytest.raises(RepositoryInspectionError):
                inspector.proves_integration(**arguments)
            overlay.unlink()
    assert inspector.proves_integration(**arguments)
