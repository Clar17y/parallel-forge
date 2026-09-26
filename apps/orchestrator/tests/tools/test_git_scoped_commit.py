"""Primary checkpoints cannot stage or commit changes outside approved paths."""

import pytest
from forge.tools.git import ControlledGitError

from apps.orchestrator.tests.tools.test_git import _controlled, _managed_repository


def test_scoped_commit_preserves_foreign_changes_without_staging(tmp_path):
    repository, _, tree = _managed_repository(tmp_path)
    (tree.path / "allowed.txt").write_text("approved\n")
    (tree.path / "foreign.txt").write_text("human work\n")
    git = _controlled(repository, tmp_path / "state")
    with pytest.raises(ControlledGitError):
        git.prepare_commit(tree, "scoped", allowed_paths=("allowed.txt",))
    assert git.head_sha(tree) == tree.base_sha
    assert git._run(tree.path, ("diff", "--cached", "--name-only")).stdout.strip() == ""
    assert (tree.path / "foreign.txt").read_text() == "human work\n"


def test_scoped_commit_rechecks_exact_staged_tree(tmp_path, monkeypatch):
    repository, _, tree = _managed_repository(tmp_path)
    (tree.path / "allowed.txt").write_text("approved\n")
    git = _controlled(repository, tmp_path / "state")
    run = git._run

    def inject(path, arguments, **kwargs):
        if arguments == ("add", "-A", "--"):
            (tree.path / "foreign.txt").write_text("concurrent human work\n")
        return run(path, arguments, **kwargs)

    monkeypatch.setattr(git, "_run", inject)
    with pytest.raises(ControlledGitError):
        git.prepare_commit(tree, "scoped", allowed_paths=("allowed.txt",))
    assert git.head_sha(tree) == tree.base_sha
    assert (tree.path / "foreign.txt").read_text() == "concurrent human work\n"


def test_scoped_commit_accepts_directory_children_and_preserves_prepared_tree(tmp_path):
    repository, _, tree = _managed_repository(tmp_path)
    (tree.path / "docs").mkdir()
    (tree.path / "docs/result.md").write_text("approved\n")
    git = _controlled(repository, tmp_path / "state")
    prepared = git.prepare_commit(tree, "scoped", allowed_paths=("docs",))
    (tree.path / "foreign.txt").write_text("later human work\n")
    published = git.commit_prepared(tree, prepared)
    assert published.previous_sha == tree.base_sha
    assert (
        git._run(
            tree.path, ("diff", "--name-only", tree.base_sha, published.new_sha)
        ).stdout.strip()
        == "docs/result.md"
    )
    assert (tree.path / "foreign.txt").read_text() == "later human work\n"


@pytest.mark.parametrize("paths", [(), ("../docs",), (".git",), ("docs", "docs")])
def test_scoped_commit_rejects_invalid_or_empty_scope(tmp_path, paths):
    repository, _, tree = _managed_repository(tmp_path)
    (tree.path / "allowed.txt").write_text("approved\n")
    git = _controlled(repository, tmp_path / "state")
    with pytest.raises(ControlledGitError):
        git.prepare_commit(tree, "scoped", allowed_paths=paths)
    assert git.head_sha(tree) == tree.base_sha
