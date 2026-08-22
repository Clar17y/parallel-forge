from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from forge.tools.paths import CanonicalRoot, PathEscape, RepositoryAccessDenied


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_bytes(b"print('ok')\n")
    return root


def _make_symlink(link: Path, target: Path, *, directory: bool) -> None:
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


def test_canonical_root_rejects_a_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    _make_symlink(linked, target, directory=True)

    with pytest.raises(RepositoryAccessDenied):
        CanonicalRoot(linked)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "src/./main.py",
        "src/../main.py",
        "../outside.txt",
        "/etc/passwd",
        "C:/outside.txt",
        "//server/share/outside.txt",
        "src//main.py",
        "src\\main.py",
        "src/with\x00nul",
    ],
)
def test_normalize_rejects_noncanonical_or_escaping_relative_paths(
    tmp_path: Path, value: str
) -> None:
    root = CanonicalRoot(_make_root(tmp_path))

    with pytest.raises(PathEscape):
        root.normalize(value)


def test_normalize_and_contains_use_repository_relative_forward_slashes(tmp_path: Path) -> None:
    root = CanonicalRoot(_make_root(tmp_path))

    assert root.normalize("src/main.py") == "src/main.py"
    assert root.contains("src/main.py") is True
    assert root.contains("src") is True
    assert root.contains("src/../outside.txt") is False


def test_matcher_is_exact_or_descendant_not_a_string_prefix(tmp_path: Path) -> None:
    root = CanonicalRoot(_make_root(tmp_path))

    assert root.matches(".env", ".env") is True
    assert root.matches(".env/local", ".env") is True
    assert root.matches(".env.example", ".env") is False
    assert root.matches("src/app.py", "src") is True
    assert root.matches("src2/app.py", "src") is False


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive matching is Windows-specific")
def test_matcher_uses_windows_case_insensitive_path_identity(tmp_path: Path) -> None:
    root = CanonicalRoot(_make_root(tmp_path))

    assert root.matches("SRC/Main.py", "src") is True


def test_open_read_and_stat_are_regular_file_only(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    root = CanonicalRoot(root_path)

    with root.open_read("src/main.py") as stream:
        assert stream.read() == b"print('ok')\n"
    assert root.stat_file("src/main.py").st_size == len(b"print('ok')\n")


def test_open_read_rejects_a_symlinked_intermediate_component(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root_path / "linked"
    _make_symlink(link, outside, directory=True)
    root = CanonicalRoot(root_path)

    with pytest.raises(RepositoryAccessDenied), root.open_read("linked/secret.txt"):
        pass


def test_open_read_rejects_a_symlinked_final_component(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    _make_symlink(root_path / "src" / "linked.txt", outside, directory=False)
    root = CanonicalRoot(root_path)

    with pytest.raises(RepositoryAccessDenied), root.open_read("src/linked.txt"):
        pass


def test_root_identity_is_revalidated_before_access(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    root = CanonicalRoot(root_path)
    moved = tmp_path / "moved"
    root_path.rename(moved)
    fresh_parent = tmp_path / "fresh"
    fresh_root = _make_root(fresh_parent)
    fresh_root.rename(root_path)

    with pytest.raises(RepositoryAccessDenied), root.open_read("src/main.py"):
        pass


def test_absolute_path_objects_are_not_accepted_as_relative_inputs(tmp_path: Path) -> None:
    root = CanonicalRoot(_make_root(tmp_path))

    with pytest.raises(PathEscape):
        root.normalize(Path(os.fspath(root.path)) / "src" / "main.py")
