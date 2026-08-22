from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from forge.tools.paths import CanonicalRoot, PathEscape, RepositoryAccessDenied


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
        return
    except OSError, NotImplementedError:
        pass
    if os.name == "nt" and directory:
        result = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            shell=False,
            check=False,
        )
        if result.returncode == 0 and link.is_dir():
            return
    pytest.skip("the current host cannot create a symlink or junction")


def test_sibling_prefix_never_counts_as_contained(tmp_path: Path) -> None:
    root_path = tmp_path / "repo"
    sibling = tmp_path / "repo-secrets"
    root_path.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")
    root = CanonicalRoot(root_path)

    with pytest.raises(PathEscape):
        root.normalize("../repo-secrets/secret.txt")
    with pytest.raises(PathEscape):
        root.normalize(str(sibling / "secret.txt"))


def test_root_intermediate_link_is_denied_before_following_it(tmp_path: Path) -> None:
    root_path = tmp_path / "repo"
    outside = tmp_path / "outside"
    root_path.mkdir()
    outside.mkdir()
    (outside / "outside.txt").write_text("outside", encoding="utf-8")
    _symlink_or_skip(root_path / "escape", outside, directory=True)
    root = CanonicalRoot(root_path)

    with pytest.raises(RepositoryAccessDenied), root.open_read("escape/outside.txt"):
        pass


def test_root_replacement_is_not_reaccepted_after_construction(tmp_path: Path) -> None:
    root_path = tmp_path / "repo"
    root_path.mkdir()
    (root_path / "safe.txt").write_text("safe", encoding="utf-8")
    root = CanonicalRoot(root_path)
    moved = tmp_path / "repo-old"
    root_path.rename(moved)
    root_path.mkdir()
    (root_path / "safe.txt").write_text("replacement", encoding="utf-8")

    with pytest.raises(RepositoryAccessDenied):
        root.stat_file("safe.txt")
