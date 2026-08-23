from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from forge.tools import paths
from forge.tools.paths import CanonicalRoot, PathEscape, RepositoryAccessDenied


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_bytes(b"print('ok')\n")
    return root


def _make_quarantine_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "repository"
    worktree_parent = root / ".worktrees"
    target = worktree_parent / "forge-target"
    metadata_parent = root / ".git" / "worktrees"
    registration = metadata_parent / "opaque-registration"
    target.mkdir(parents=True)
    (target / "outside-marker").write_text("target\n", encoding="utf-8")
    (target / ".git").write_text("gitdir: metadata\n", encoding="utf-8")
    registration.mkdir(parents=True)
    (registration / "gitdir").write_text(f"{target / '.git'}\n", encoding="utf-8")
    return root, target, registration, tmp_path / "outside"


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


@pytest.mark.skipif(os.name != "nt", reason="Windows native ABI guard")
def test_native_rename_abi_guard_and_io_status_layout() -> None:
    assert ctypes.sizeof(ctypes.c_void_p) == 8
    with pytest.raises(RepositoryAccessDenied):
        paths._require_windows_native_pointer_size(4)
    assert ctypes.sizeof(paths._IoStatusBlock) == 16
    assert paths._IoStatusBlock.status.offset == 0
    assert paths._IoStatusBlock.information.offset == 8


@pytest.mark.skipif(os.name != "nt", reason="Windows security descriptor behavior")
def test_windows_quarantine_roots_are_created_with_owner_only_dacl(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        api.verify_owner_only_dacl(access._target_quarantine_parent.capability)
        api.verify_owner_only_dacl(access._registration_quarantine_parent.capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows security descriptor behavior")
def test_windows_quarantine_refuses_permissive_preexisting_root(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    permissive = root_path / ".worktrees" / ".forge-quarantine"
    permissive.mkdir(parents=True)
    root = CanonicalRoot(root_path)

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_worktree_quarantine(target.name, registration.name),
    ):
        pass
    assert target.is_dir()
    assert registration.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive lock behavior")
def test_windows_create_directory_holds_exclusive_git_mutation_lock(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    (root_path / ".git").mkdir()
    root = CanonicalRoot(root_path)
    competing = CanonicalRoot(root_path)

    with (
        root._create_directory(".", "first"),
        pytest.raises(RepositoryAccessDenied, match="busy"),
        competing._create_directory(".", "second"),
    ):
        pass
    assert not (root_path / "second").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive lock behavior")
def test_windows_quarantine_holds_exclusive_git_mutation_lock(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    competing = CanonicalRoot(root_path)

    with (
        root._open_worktree_quarantine(target.name, registration.name),
        pytest.raises(RepositoryAccessDenied, match="busy"),
        competing._open_worktree_quarantine(target.name, registration.name),
    ):
        pass
    assert target.is_dir()
    assert registration.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point behavior")
def test_windows_reparse_mutation_lock_is_refused_without_following_target(
    tmp_path: Path,
) -> None:
    root_path = _make_root(tmp_path)
    (root_path / ".git").mkdir()
    outside = tmp_path / "outside-lock-target"
    outside.mkdir()
    lock_path = root_path / ".git" / "forge-worktree.lock"
    _make_symlink(lock_path, outside, directory=True)
    root = CanonicalRoot(root_path)

    with pytest.raises(RepositoryAccessDenied), root._create_directory(".", "created"):
        pass
    assert not (outside / "created").exists()


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


@pytest.mark.skipif(os.name != "nt", reason="directory sharing semantics are Windows-specific")
def test_open_directory_blocks_rename_until_capability_is_released(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    root = CanonicalRoot(root_path)
    directory = root_path / "src"
    moved = tmp_path / "moved-src"

    with root.open_directory("src"):
        with pytest.raises(PermissionError):
            directory.rename(moved)
        assert directory.is_dir()

    directory.rename(moved)
    assert moved.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX advisory lock semantics are platform-specific")
def test_create_directory_capability_serializes_git_mutations(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    (root_path / ".git").mkdir()
    root = CanonicalRoot(root_path)

    with root._create_directory(".", "first"):
        competing = CanonicalRoot(root_path)
        with pytest.raises(RepositoryAccessDenied), competing._create_directory(".", "second"):
            pass
        assert not (root_path / "second").exists()


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


def test_quarantine_moves_exact_target_then_registration(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        root._quarantine_target(access)
        assert not target.exists()
        assert access.target_quarantine_path.is_dir()
        assert (access.target_quarantine_path / "outside-marker").is_file()

        root._quarantine_registration(access)
        assert not registration.exists()
        assert access.registration_quarantine_path.is_dir()
        assert (access.registration_quarantine_path / "gitdir").is_file()


@pytest.mark.parametrize("scope", ("target", "metadata"))
def test_quarantine_swap_interposer_cannot_redirect_move(tmp_path: Path, scope: str) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    swapped = False
    swap_succeeded = False
    parent = target.parent if scope == "target" else registration.parent
    moved_parent = parent.with_name(f"{parent.name}-moved")

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        swapped = True
        try:
            parent.rename(moved_parent)
            swap_succeeded = True
            outside.mkdir()
            _make_symlink(parent, outside, directory=True)
        except OSError, NotImplementedError:
            pass
        root._quarantine_target(access)
        if scope == "metadata":
            root._quarantine_registration(access)

    assert swapped is True
    if os.name == "nt":
        assert swap_succeeded is False
    else:
        assert swap_succeeded is True
    assert not outside.exists() or not any(outside.iterdir())


def test_quarantine_rejects_locked_registration_and_collisions_without_mutation(
    tmp_path: Path,
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    (registration / "locked").write_text("locked\n", encoding="utf-8")
    root = CanonicalRoot(root_path)

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_worktree_quarantine(target.name, registration.name),
    ):
        pass
    assert target.is_dir()
    assert registration.is_dir()

    (registration / "locked").unlink()
    collision = root_path / ".worktrees" / ".forge-quarantine" / target.name
    collision.parent.mkdir(parents=True)
    collision.mkdir()
    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_worktree_quarantine(target.name, registration.name),
    ):
        pass
    assert target.is_dir()
    assert registration.is_dir()


def test_quarantine_rejects_registration_quarantine_collision_without_mutation(
    tmp_path: Path,
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    collision = root_path / ".git" / ".forge-worktree-quarantine" / registration.name
    collision.parent.mkdir(parents=True)
    collision.mkdir()
    root = CanonicalRoot(root_path)

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_worktree_quarantine(target.name, registration.name),
    ):
        pass
    assert target.is_dir()
    assert registration.is_dir()


@pytest.mark.parametrize("scope", ("target", "metadata"))
def test_quarantine_rejects_a_linked_quarantine_root_without_mutation(
    tmp_path: Path, scope: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    outside = tmp_path / f"outside-{scope}"
    outside.mkdir()
    quarantine_root = (
        root_path / ".worktrees" / ".forge-quarantine"
        if scope == "target"
        else root_path / ".git" / ".forge-worktree-quarantine"
    )
    _make_symlink(quarantine_root, outside, directory=True)
    root = CanonicalRoot(root_path)

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_worktree_quarantine(target.name, registration.name),
    ):
        pass
    assert target.is_dir()
    assert registration.is_dir()
    assert not any(outside.iterdir())


def test_quarantine_rejects_a_linked_live_target_without_mutation(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    for child in target.iterdir():
        child.unlink()
    target.rmdir()
    _make_symlink(target, outside, directory=True)
    root = CanonicalRoot(root_path)

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_worktree_quarantine(target.name, registration.name),
    ):
        pass
    assert target.is_dir()
    assert not any(outside.iterdir())
    assert registration.is_dir()


def test_quarantine_rejects_stale_target_capability_without_mutation(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        api = root._windows
        if api is not None:
            api.close(access._target.handle.capability)
        else:
            os.close(access._target.handle.capability)
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_target(access)
        assert target.is_dir()
        assert registration.is_dir()


def test_quarantine_rejects_a_substituted_target_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        if root._windows is not None:
            original_identity = root._windows.identity

            def substituted_identity(handle: int) -> object:
                identity = tuple(original_identity(handle))
                if handle == access._target.handle.capability:
                    return (*identity[:-1], identity[-1] + 1)
                return identity

            monkeypatch.setattr(root._windows, "identity", substituted_identity)
        else:
            original_fstat = os.fstat

            def substituted_fstat(descriptor: int) -> os.stat_result:
                metadata = original_fstat(descriptor)
                if descriptor != access._target.handle.capability:
                    return metadata
                values = list(metadata)
                values[1] += 1
                return os.stat_result(values)

            monkeypatch.setattr(os, "fstat", substituted_fstat)
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_target(access)
        assert target.is_dir()
        assert registration.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows post-rename injection")
def test_quarantine_preserves_exact_evidence_after_post_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    def fail_reopen(path: Path) -> int:
        del path
        raise RepositoryAccessDenied("injected verification failure")

    monkeypatch.setattr(api, "open_directory_for_verification", fail_reopen)
    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_worktree_quarantine(target.name, registration.name) as access,
    ):
        root._quarantine_target(access)
    assert not target.exists()
    retained = root_path / ".worktrees" / ".forge-quarantine" / target.name
    assert retained.is_dir()
    assert (retained / "outside-marker").is_file()
    assert registration.is_dir()


def test_quarantine_closes_destination_after_identity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    opened: list[int] = []
    closed: list[int] = []

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        if os.name == "nt":
            api = root._windows
            assert api is not None
            original_open_windows = api.open_directory_for_verification
            original_identity_windows = api.identity
            original_close_windows = api.close

            def open_spy_windows(path: Path) -> int:
                descriptor = original_open_windows(path)
                opened.append(descriptor)
                return descriptor

            def identity_spy_windows(descriptor: int) -> object:
                if opened and descriptor == opened[0]:
                    raise OSError("injected destination identity failure")
                return original_identity_windows(descriptor)

            def close_spy_windows(descriptor: int) -> None:
                closed.append(descriptor)
                original_close_windows(descriptor)

            monkeypatch.setattr(api, "open_directory_for_verification", open_spy_windows)
            monkeypatch.setattr(api, "identity", identity_spy_windows)
            monkeypatch.setattr(api, "close", close_spy_windows)
        else:
            original_open_posix = os.open
            original_fd_identity_posix = paths._fd_identity
            original_close_posix = os.close

            def open_spy_posix(*args: Any, **kwargs: Any) -> int:
                descriptor = original_open_posix(*args, **kwargs)
                if kwargs.get("dir_fd") is not None:
                    opened.append(descriptor)
                return descriptor

            def identity_spy_posix(descriptor: int) -> tuple[int, int]:
                if opened and descriptor == opened[-1]:
                    raise OSError("injected destination identity failure")
                return original_fd_identity_posix(descriptor)

            def close_spy_posix(descriptor: int) -> None:
                closed.append(descriptor)
                original_close_posix(descriptor)

            monkeypatch.setattr(os, "open", open_spy_posix)
            monkeypatch.setattr(paths, "_fd_identity", identity_spy_posix)
            monkeypatch.setattr(os, "close", close_spy_posix)

        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_target(access)

    assert opened
    assert opened[0] in closed
    assert not target.exists()
    assert (root_path / ".worktrees" / ".forge-quarantine" / target.name).is_dir()
    assert registration.is_dir()


@pytest.mark.parametrize("scope", ("target", "metadata"))
def test_quarantine_root_swap_interposer_cannot_redirect_move(tmp_path: Path, scope: str) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        quarantine_parent = (
            access.target_quarantine_path.parent
            if scope == "target"
            else access.registration_quarantine_path.parent
        )
        moved_parent = quarantine_parent.with_name(f"{quarantine_parent.name}-moved")
        attempted = True
        swapped = False
        try:
            quarantine_parent.rename(moved_parent)
            swapped = True
            outside.mkdir()
            _make_symlink(quarantine_parent, outside, directory=True)
        except OSError, NotImplementedError:
            pass
        assert attempted is True
        if os.name == "nt":
            assert swapped is False
        else:
            assert swapped is True
        root._quarantine_target(access)
        if scope == "metadata":
            root._quarantine_registration(access)

    assert not outside.exists() or not any(outside.iterdir())


def test_quarantine_capability_rejects_foreign_and_released_use(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    foreign = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        with pytest.raises(RepositoryAccessDenied):
            foreign._quarantine_target(access)
        root._quarantine_target(access)

    with pytest.raises(RepositoryAccessDenied):
        root._quarantine_registration(access)


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
