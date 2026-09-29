from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from forge.application.ports.repository import (
    ProcessResult,
    RepositoryAccessDenied,
    RepositoryLimitExceeded,
)
from forge.tools.repository import RepositoryReader


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _commit_all(root: Path) -> None:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Forge Tests",
        "-c",
        "user.email=forge@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )


def test_git_discovery_prunes_ignored_generated_directory_before_listing_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("generated/\n", encoding="utf-8")
    generated = root / "generated"
    generated.mkdir()
    for index in range(20):
        (generated / f"{index}.txt").write_text("ignored", encoding="utf-8")
    (root / "visible.txt").write_text("needle", encoding="utf-8")

    reader = RepositoryReader(root, max_list_entries=2)
    entries = reader.list_files()

    assert tuple(entry.path for entry in entries) == (".gitignore", "visible.txt")
    assert tuple(match.path for match in reader.search("needle")) == ("visible.txt",)


def test_git_discovery_honors_nested_negation_and_keeps_tracked_ignored_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "generated").mkdir()
    (root / "generated" / "tracked.txt").write_text("tracked", encoding="utf-8")
    _commit_all(root)
    (root / ".gitignore").write_text(
        "/generated/*\n!/generated/keep/\n/generated/keep/*\n!/generated/keep/wanted.txt\n",
        encoding="utf-8",
    )
    (root / "generated" / "ignored.txt").write_text("ignored", encoding="utf-8")
    keep = root / "generated" / "keep"
    keep.mkdir()
    (keep / "wanted.txt").write_text("wanted", encoding="utf-8")
    (keep / "ignored.txt").write_text("ignored", encoding="utf-8")

    entries = RepositoryReader(root).list_files()

    assert tuple(entry.path for entry in entries) == (
        ".gitignore",
        "generated/keep/wanted.txt",
        "generated/tracked.txt",
    )


def test_git_discovery_limits_narrow_scope_and_preserves_secret_exclusions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "one").mkdir()
    (root / "two").mkdir()
    (root / "one" / "visible.txt").write_text("visible", encoding="utf-8")
    (root / "one" / ".env").write_text("secret", encoding="utf-8")
    (root / "two" / "other.txt").write_text("other", encoding="utf-8")
    (root / "one" / "deleted.txt").write_text("deleted", encoding="utf-8")
    _commit_all(root)
    (root / "one" / "deleted.txt").unlink()

    entries = RepositoryReader(root, secret_paths=("one/.env",)).list_files("one")

    assert tuple(entry.path for entry in entries) == ("one/visible.txt",)


def test_git_discovery_skips_tracked_paths_with_deleted_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    nested = root / "dir" / "nested"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("tracked", encoding="utf-8")
    _commit_all(root)
    (nested / "a.txt").unlink()
    nested.rmdir()
    (root / "dir").rmdir()

    assert RepositoryReader(root).list_files() == ()


def test_git_discovery_skips_tracked_path_when_parent_becomes_a_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    nested = root / "dir" / "nested"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("tracked", encoding="utf-8")
    _commit_all(root)
    (nested / "a.txt").unlink()
    nested.rmdir()
    (root / "dir").rmdir()
    target = root / "elsewhere"
    target.mkdir()
    try:
        (root / "dir").symlink_to(target, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("directory symlinks are unavailable")

    assert tuple(entry.path for entry in RepositoryReader(root).list_files()) == ()


def test_git_discovery_keeps_virtual_environment_exclusion_for_tracked_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    virtual = root / "custom-env"
    virtual.mkdir()
    (virtual / "tracked.txt").write_text("tracked", encoding="utf-8")
    _commit_all(root)
    (virtual / "pyvenv.cfg").write_text("home = hidden", encoding="utf-8")

    assert RepositoryReader(root).list_files() == ()


def test_git_discovery_skips_tracked_path_when_parent_becomes_a_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    nested = root / "dir" / "nested"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("tracked", encoding="utf-8")
    _commit_all(root)
    (nested / "a.txt").unlink()
    nested.rmdir()
    (root / "dir").rmdir()
    (root / "dir").write_text("replacement", encoding="utf-8")

    assert tuple(entry.path for entry in RepositoryReader(root).list_files()) == ("dir",)


def test_git_discovery_supports_linked_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "tracked.txt").write_text("tracked", encoding="utf-8")
    _commit_all(main)
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-qb", "linked", str(linked))
    (linked / "untracked.txt").write_text("untracked", encoding="utf-8")

    entries = RepositoryReader(linked).list_files()

    assert tuple(entry.path for entry in entries) == ("tracked.txt", "untracked.txt")


class _StaticGitRunner:
    def __init__(
        self, stdout: str, *, truncated: bool = False, byte_count: int | None = None
    ) -> None:
        self.result = ProcessResult(
            return_code=0,
            stdout=stdout,
            stderr="",
            timed_out=False,
            stdout_original_byte_count=(
                len(stdout.encode("utf-8")) if byte_count is None else byte_count
            ),
            stderr_original_byte_count=0,
            stdout_truncated=truncated,
            stderr_truncated=False,
        )

    def run_argv(self, *_: object, **__: object) -> ProcessResult:
        return self.result


def test_git_discovery_fails_closed_on_malformed_process_output(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")

    with pytest.raises(RepositoryAccessDenied):
        RepositoryReader(root, process_runner=_StaticGitRunner("not-null-terminated")).list_files()


def test_git_discovery_reports_process_output_limit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")

    with pytest.raises(RepositoryLimitExceeded):
        RepositoryReader(
            root, process_runner=_StaticGitRunner("", truncated=True, byte_count=64 * 1024)
        ).list_files()


def test_git_discovery_deduplicates_unmerged_index_paths_before_entry_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "visible.txt").write_text("visible", encoding="utf-8")

    entries = RepositoryReader(
        root, max_list_entries=1, process_runner=_StaticGitRunner("visible.txt\0visible.txt\0")
    ).list_files()

    assert tuple(entry.path for entry in entries) == ("visible.txt",)


def test_git_discovery_fails_closed_when_git_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    monkeypatch.setattr("forge.tools.repository.shutil.which", lambda _: None)

    with pytest.raises(RepositoryAccessDenied):
        RepositoryReader(root).list_files()


def test_git_discovery_skips_symlink_entries(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    target = root / "target.txt"
    target.write_text("visible", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(target)
    except OSError, NotImplementedError:
        pytest.skip("file symlinks are unavailable")

    entries = RepositoryReader(root).list_files()

    assert tuple(entry.path for entry in entries) == ("target.txt",)


def test_git_discovery_skips_untracked_nested_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    nested = root / "nested_repo"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "nested_file.txt").write_text("nested", encoding="utf-8")
    (root / "visible.txt").write_text("visible", encoding="utf-8")

    entries = RepositoryReader(root).list_files()

    assert tuple(entry.path for entry in entries) == ("visible.txt",)


def test_git_discovery_skips_unignored_linked_worktree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "tracked.txt").write_text("tracked", encoding="utf-8")
    _commit_all(root)
    linked = root / ".worktrees" / "feature"
    linked.parent.mkdir()
    _git(root, "worktree", "add", "-qb", "feature", str(linked))
    (linked / "feature.txt").write_text("feature", encoding="utf-8")

    entries = RepositoryReader(root).list_files()

    assert tuple(entry.path for entry in entries) == ("tracked.txt",)


def test_git_discovery_fails_closed_on_malformed_directory_entry(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")

    with pytest.raises(RepositoryAccessDenied):
        RepositoryReader(root, process_runner=_StaticGitRunner("valid.txt\0sub//\0")).list_files()


def test_git_discovery_prunes_unignored_reserved_components_before_output_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("forge.tools.repository._DEFAULT_GIT_DISCOVERY_BYTES", 128)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    node_modules = root / "node_modules"
    node_modules.mkdir()
    for index in range(20):
        (node_modules / f"pkg_{index}.js").write_text("console.log(1);", encoding="utf-8")
    venv = root / ".venv"
    venv.mkdir()
    for index in range(20):
        (venv / f"lib_{index}.py").write_text("x = 1", encoding="utf-8")
    (root / "visible.txt").write_text("visible", encoding="utf-8")

    reader = RepositoryReader(root, max_list_entries=2)
    entries = reader.list_files()

    assert tuple(entry.path for entry in entries) == ("visible.txt",)


@pytest.mark.parametrize("exclusion_kind", ["artifact_paths", "managed_worktree_paths"])
def test_git_discovery_configured_exclusions_are_literal(
    tmp_path: Path,
    exclusion_kind: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for name in ("build[1]", "build1"):
        directory = root / name
        directory.mkdir()
        (directory / "source.txt").write_text("source", encoding="utf-8")

    reader = RepositoryReader(
        root,
        artifact_paths=("build[1]",) if exclusion_kind == "artifact_paths" else (),
        managed_worktree_paths=("build[1]",) if exclusion_kind == "managed_worktree_paths" else (),
    )

    assert tuple(entry.path for entry in reader.list_files()) == ("build1/source.txt",)


def test_git_discovery_caches_directory_listings_per_call(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    sub1 = root / "pkg" / "sub1"
    sub1.mkdir(parents=True)
    sub2 = root / "pkg" / "sub2"
    sub2.mkdir(parents=True)
    for index in range(10):
        (sub1 / f"file_{index}.txt").write_text("data", encoding="utf-8")
        (sub2 / f"file_{index}.txt").write_text("data", encoding="utf-8")

    reader = RepositoryReader(root)
    original_list_dir = reader._root.list_directory
    call_counts: dict[str, int] = {}

    def counting_list_directory(
        dir_path: str | os.PathLike[str],
    ) -> tuple[tuple[str, os.stat_result], ...]:
        key = str(dir_path)
        call_counts[key] = call_counts.get(key, 0) + 1
        return original_list_dir(dir_path)

    reader._root.list_directory = counting_list_directory  # type: ignore[assignment,method-assign]
    entries = reader.list_files()

    assert len(entries) == 20
    # Root "." is listed once for .git check and once inside cache; subdirectories once each.
    for dir_path, count in call_counts.items():
        assert count <= 2, f"Directory {dir_path!r} listed {count} times (expected <= 2)"


def test_git_discovery_fails_closed_on_invalid_git_directory_inside_parent_repo(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    _git(parent, "init", "-q")
    (parent / "parent.txt").write_text("parent", encoding="utf-8")
    child = parent / "fake_child"
    child.mkdir()
    fake_git = child / ".git"
    fake_git.mkdir()
    (fake_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (child / "child.txt").write_text("child", encoding="utf-8")

    with pytest.raises(RepositoryAccessDenied):
        RepositoryReader(child).list_files()


def test_git_discovery_core_worktree_redirection_cannot_change_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "tracked.txt").write_text("tracked", encoding="utf-8")
    _commit_all(root)
    other = tmp_path / "other"
    other.mkdir()
    (other / "other.txt").write_text("other", encoding="utf-8")
    _git(root, "config", "core.worktree", str(other))

    entries = RepositoryReader(root).list_files()

    assert tuple(entry.path for entry in entries) == ("tracked.txt",)


def test_git_discovery_case_varied_scoped_listing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    sub = root / "SubDir"
    sub.mkdir()
    (sub / "file.txt").write_text("content", encoding="utf-8")

    reader = RepositoryReader(root)
    if os.name == "nt":
        for scope in ("subdir", "SUBDIR", "SubDir"):
            entries = reader.list_files(scope)
            assert tuple(entry.path for entry in entries) == ("SubDir/file.txt",)
    else:
        entries = reader.list_files("SubDir")
        assert tuple(entry.path for entry in entries) == ("SubDir/file.txt",)
