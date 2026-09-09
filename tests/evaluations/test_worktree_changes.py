from dataclasses import replace
from pathlib import Path

import pytest
from forge.domain.policy import ProjectPolicy
from forge.tools.git import ControlledGitError

from apps.orchestrator.tests.tools.test_git import _controlled, _git, _managed_repository


def test_evaluation_paths_include_committed_dirty_and_untracked_changes(tmp_path: Path) -> None:
    repository, identity, worktree = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    policy = ProjectPolicy(
        id=identity.project_id,
        version=1,
        repository_path=str(repository),
        github_repository="local/eval",
        default_branch="main",
    )
    (worktree.path / "committed.txt").write_text("committed")
    _git(worktree.path, "add", "committed.txt")
    _git(worktree.path, "commit", "-m", "first")
    (worktree.path / "README.md").write_text("dirty")
    (worktree.path / "untracked space.txt").write_text("new")
    assert controlled.changed_paths(worktree, policy) == (
        "README.md",
        "committed.txt",
        "untracked space.txt",
    )


@pytest.mark.parametrize("command", ["diff", "ls-files"])
def test_evaluation_paths_reject_incomplete_git_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    repository, identity, worktree = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    policy = ProjectPolicy(
        id=identity.project_id,
        version=1,
        repository_path=str(repository),
        github_repository="local/eval",
        default_branch="main",
    )
    original = controlled._run

    def truncated(path, arguments, **kwargs):
        result = original(path, arguments, **kwargs)
        if arguments[0] == command and "-z" in arguments:
            return replace(result, stdout_truncated=True)
        return result

    monkeypatch.setattr(controlled, "_run", truncated)
    with pytest.raises(ControlledGitError):
        controlled.changed_paths(worktree, policy)
