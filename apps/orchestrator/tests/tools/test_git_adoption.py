import subprocess

import pytest
from forge.tools.git import ControlledGitError

from apps.orchestrator.tests.tools.test_git import (
    TRUSTED_GIT,
    _controlled,
    _git,
    _managed_repository,
)


def output(path, *args):
    return subprocess.run(
        [str(TRUSTED_GIT), "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def candidate(tmp_path):
    repository, _, tree = _managed_repository(tmp_path)
    (tree.path / "feature.txt").write_text("feature\n")
    _git(tree.path, "add", "feature.txt")
    _git(tree.path, "commit", "-m", "feature")
    previous = output(tree.path, "rev-parse", "HEAD")
    (repository / "upstream.txt").write_text("upstream\n")
    _git(repository, "add", "upstream.txt")
    _git(repository, "commit", "-m", "upstream")
    base = output(repository, "rev-parse", "HEAD")
    # Objects stand in for an exact fetched remote merge commit. Adoption proves
    # ancestry; subsequent validation/review remains responsible for its content.
    new = output(
        repository, "commit-tree", f"{base}^{{tree}}", "-p", previous, "-p", base, "-m", "update"
    )
    return _controlled(repository, tmp_path / "state"), tree, previous, base, new


def test_adoption_proves_both_parents_and_replays_without_rewriting(tmp_path):
    git, tree, previous, base, new = candidate(tmp_path)
    git.adopt_head(tree, previous, new, base)
    assert git.head_sha(tree) == new
    assert (tree.path / "upstream.txt").read_text() == "upstream\n"
    git.adopt_head(tree, previous, new, base)
    assert git.head_sha(tree) == new


@pytest.mark.parametrize(
    "defect", ["missing_previous", "missing_base", "dirty", "untracked", "ignored_collision"]
)
def test_adoption_rejects_unproven_ancestry_or_local_changes(tmp_path, defect):
    git, tree, previous, base, new = candidate(tmp_path)
    if defect == "missing_previous":
        new = base
    elif defect == "missing_base":
        new = previous
    elif defect == "dirty":
        (tree.path / "README.md").write_text("local work\n")
    elif defect == "ignored_collision":
        common = output(tree.path, "rev-parse", "--git-common-dir")
        from pathlib import Path

        (Path(common) / "info" / "exclude").write_text("upstream.txt\n")
        (tree.path / "upstream.txt").write_text("local ignored work\n")
    else:
        (tree.path / "untracked.txt").write_text("local work\n")
    with pytest.raises(ControlledGitError):
        git.adopt_head(tree, previous, new, base)
    assert git.head_sha(tree) == previous
    if defect == "ignored_collision":
        assert (tree.path / "upstream.txt").read_text() == "local ignored work\n"
