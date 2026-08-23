from __future__ import annotations

import ctypes
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from forge.tools import paths
from forge.tools.paths import CanonicalRoot, PathEscape, RepositoryAccessDenied
from forge.tools.process import ProcessRunner


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
    (registration / "gitdir").write_bytes(f"{target / '.git'}\n".encode())
    return root, target, registration, tmp_path / "outside"


def _make_real_quarantine_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    git = shutil.which("git") or "git"
    root = tmp_path / "repository"
    root.mkdir()
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Forge Test"),
        ("config", "user.email", "forge@example.test"),
    ):
        result = subprocess.run(
            [git, "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            shell=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    (root / "README.md").write_text("forge\n", encoding="utf-8")
    for arguments in (("add", "README.md"), ("commit", "-m", "initial")):
        result = subprocess.run(
            [git, "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            shell=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    target = root / ".worktrees" / "target"
    target.parent.mkdir()
    result = subprocess.run(
        [git, "-C", str(root), "worktree", "add", "-b", "feature", str(target), "HEAD"],
        capture_output=True,
        check=False,
        shell=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    metadata = next((root / ".git" / "worktrees").iterdir())
    return root, target, metadata


def _make_opaque_real_quarantine_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root, target, registration = _make_real_quarantine_fixture(tmp_path)
    opaque = registration.with_name("opaque-registration")
    marker = target / ".git"
    marker.chmod(stat.S_IRUSR | stat.S_IWUSR)
    marker.unlink()
    marker.write_bytes(f"gitdir: {opaque}\n".encode())
    registration.rename(opaque)
    return root, target, opaque


def _remove_flat_directory(path: Path) -> None:
    for entry in path.iterdir():
        entry.unlink()
    path.rmdir()


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


def _make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0 or not link.is_dir():
        details = b" ".join((result.stdout, result.stderr)).decode(errors="replace")
        raise AssertionError(f"could not create junction: {details}")


def _file_names_information(*names: str) -> bytes:
    records: list[bytes] = []
    for index, name in enumerate(names):
        encoded = name.encode("utf-16-le")
        record_length = 12 + len(encoded)
        next_offset = 0 if index == len(names) - 1 else (record_length + 3) & ~3
        record = (
            next_offset.to_bytes(4, "little")
            + b"\x00\x00\x00\x00"
            + len(encoded).to_bytes(4, "little")
            + encoded
        )
        records.append(record + b"\x00" * (next_offset - record_length))
    return b"".join(records)


def test_canonical_root_rejects_a_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    _make_symlink(linked, target, directory=True)

    with pytest.raises(RepositoryAccessDenied):
        CanonicalRoot(linked)


@pytest.mark.skipif(os.name == "nt", reason="POSIX namespace swap proof")
def test_managed_worktree_access_remains_bound_when_lexical_target_is_replaced(
    tmp_path: Path,
) -> None:
    root_path, target, registration = _make_real_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    moved = target.with_name("target-moved")
    replacement = target

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_managed_worktree(target.name, registration.name) as access,
    ):
        assert root._active_directory_access(access.normalized) is access
        target.rename(moved)
        replacement.mkdir()
        (replacement / "outside-marker").write_text("replacement\n", encoding="utf-8")
        assert root._directory_access_matches_path(access) is False
        assert access.path == target
        assert moved.is_dir()
        assert (moved / "outside-marker").read_text(encoding="utf-8") == "target\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX active descriptor launch proof")
def test_process_runner_uses_the_retained_managed_worktree_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration = _make_real_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    runner = ProcessRunner(root)
    seen: list[object] = []
    original = root._launch_path_for_access

    def launch(normalized: str, access: object, *, require_fd: bool = False) -> str:
        seen.append(access)
        return original(normalized, access, require_fd=require_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(root, "_launch_path_for_access", launch)
    with root._open_managed_worktree(target.name, registration.name) as access:
        result = runner.run_argv(
            [shutil.which("git") or "git", "--version"],
            cwd=str(target),
            environment={"PATH": os.environ.get("PATH", "")},
        )
        assert result.return_code == 0
        assert seen == [access]


def test_managed_worktree_access_binds_target_registration_and_common_git_paths(
    tmp_path: Path,
) -> None:
    root_path, target, registration = _make_opaque_real_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_managed_worktree(target.name, registration.name) as access:
        normalized = f".worktrees/{target.name}"
        assert access.normalized == normalized
        assert access.registration_path == registration
        assert root._active_directory_access(normalized) is access

        worktree_path = root._launch_path_for_access(normalized, access, require_fd=True)
        git_dir = root._git_directory_for_access(normalized, access)
        common_dir = root._git_common_directory_for_access(normalized, access)
        work_tree = root._git_work_tree_for_access(normalized, access)

        assert git_dir != common_dir
        if os.name == "nt":
            assert worktree_path == str(target)
            assert git_dir == str(registration)
            assert common_dir == str(root_path / ".git")
            assert work_tree == str(target)
        else:
            assert worktree_path == f"/proc/self/fd/{access.capability}"
            assert git_dir == f"/proc/self/fd/{access.registration_capability}"
            assert common_dir == f"/proc/self/fd/{access.git_capability}"
            assert work_tree == worktree_path
            assert Path(worktree_path).resolve() == target.resolve()
            assert Path(git_dir).resolve() == registration.resolve()
            assert Path(common_dir).resolve() == (root_path / ".git").resolve()

        fds = root._pass_fds_for_access(normalized, access)
        if os.name == "nt":
            assert fds == ()
        else:
            assert fds == (access.capability, access.registration_capability, access.git_capability)
            assert len(set(fds)) == 3


def test_managed_worktree_access_rejects_foreign_and_retired_capabilities(
    tmp_path: Path,
) -> None:
    root_path, target, registration = _make_opaque_real_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    foreign = CanonicalRoot(root_path)

    with (
        root._open_managed_worktree(target.name, registration.name) as access,
        pytest.raises(RepositoryAccessDenied),
    ):
        foreign._verify_directory_access(access.normalized, access)

    with pytest.raises(RepositoryAccessDenied):
        root._pass_fds_for_access(access.normalized, access)


@pytest.mark.skipif(os.name != "nt", reason="Windows native ABI guard")
def test_native_rename_abi_guard_and_io_status_layout() -> None:
    assert ctypes.sizeof(ctypes.c_void_p) == 8
    with pytest.raises(RepositoryAccessDenied):
        paths._require_windows_native_pointer_size(4)
    assert ctypes.sizeof(paths._IoStatusBlock) == 16
    assert paths._IoStatusBlock.status.offset == 0
    assert paths._IoStatusBlock.information.offset == 8


@pytest.mark.skipif(os.name != "nt", reason="Windows native quarantine deletion primitives")
def test_windows_native_enumerates_exact_names_from_a_retained_handle(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    (root_path / "one.txt").write_text("one", encoding="utf-8")
    (root_path / "nested").mkdir()
    (root_path / "nested" / "two.txt").write_text("two", encoding="utf-8")
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    handle = api.open_directory(root_path)
    try:
        assert set(api.enumerate_names(handle)) == {"one.txt", "nested", "src"}
    finally:
        api.close(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows native enumeration parser")
def test_windows_native_file_name_parser_accepts_strict_utf16_records() -> None:
    data = _file_names_information("normal.txt", "café.txt", "😀.bin")

    assert paths._WindowsPathApi._parse_file_names_information(data, len(data)) == (
        "normal.txt",
        "café.txt",
        "😀.bin",
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows native enumeration parser")
@pytest.mark.parametrize(
    ("data", "information_length"),
    [
        (b"\x00" * 11, 11),
        (b"\x00" * 12, 13),
        (
            b"\x00" * 8 + (1).to_bytes(4, "little") + b"A",
            13,
        ),
        (
            b"\x00" * 8 + (2).to_bytes(4, "little") + b"\x00\xd8",
            14,
        ),
    ],
)
def test_windows_native_file_name_parser_rejects_malformed_lengths_and_utf16(
    data: bytes, information_length: int
) -> None:
    with pytest.raises(RepositoryAccessDenied):
        paths._WindowsPathApi._parse_file_names_information(data, information_length)


@pytest.mark.skipif(os.name != "nt", reason="Windows native enumeration validation")
def test_windows_native_enumeration_rejects_a_malformed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = CanonicalRoot(_make_root(tmp_path))
    api = root._windows
    assert api is not None
    parent = api.open_directory(root.path)
    malformed = b"\x00" * 8 + (2).to_bytes(4, "little") + b"\x00\xd8"

    def query_directory(
        _handle: int,
        _event: object,
        _apc_routine: object,
        _apc_context: object,
        io_status: object,
        buffer: object,
        _length: int,
        _information_class: int,
        _return_single_entry: bool,
        _file_name: object,
        _restart_scan: bool,
    ) -> int:
        ctypes.memmove(buffer, malformed, len(malformed))
        ctypes.cast(io_status, ctypes.POINTER(paths._IoStatusBlock)).contents.information = len(
            malformed
        )
        return paths._STATUS_SUCCESS

    monkeypatch.setattr(api, "_nt_query_directory_file", query_directory)
    try:
        with pytest.raises(RepositoryAccessDenied):
            api.enumerate_names(parent)
    finally:
        api.close(parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows native relative child opens")
def test_windows_native_opens_normal_directory_and_reparse_children_relative_to_handle(
    tmp_path: Path,
) -> None:
    root_path = _make_root(tmp_path)
    (root_path / "normal.txt").write_text("normal", encoding="utf-8")
    directory = root_path / "directory"
    directory.mkdir()
    (directory / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_text("outside", encoding="utf-8")
    _make_junction(root_path / "junction", outside)

    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    parent = api.open_directory(root_path)
    children: list[int] = []
    try:
        normal = api.open_child(parent, "normal.txt")
        children.append(normal)
        normal_info = api.information(normal)
        assert not int(normal_info.attributes) & (
            paths._FILE_ATTRIBUTE_DIRECTORY | paths._FILE_ATTRIBUTE_REPARSE_POINT
        )

        directory_handle = api.open_child(parent, "directory", list_handle=True)
        children.append(directory_handle)
        directory_info = api.information(directory_handle)
        assert int(directory_info.attributes) & paths._FILE_ATTRIBUTE_DIRECTORY
        assert not int(directory_info.attributes) & paths._FILE_ATTRIBUTE_REPARSE_POINT
        assert api.enumerate_names(directory_handle) == ("inside.txt",)

        junction = api.open_child(parent, "junction")
        children.append(junction)
        junction_info = api.information(junction)
        assert int(junction_info.attributes) & paths._FILE_ATTRIBUTE_REPARSE_POINT
    finally:
        for child in reversed(children):
            api.close(child)
        api.close(parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows native regular-child validation")
def test_windows_native_regular_child_validation_rejects_directory_and_reparse_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path = _make_root(tmp_path)
    (root_path / "normal.txt").write_text("normal", encoding="utf-8")
    (root_path / "directory").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_junction(root_path / "junction", outside)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    opened: list[int] = []
    closed: list[int] = []
    original_open_child = api.open_child
    original_close = api.close

    def open_child_spy(parent: int, name: str, *, list_handle: bool = False) -> int:
        handle = original_open_child(parent, name, list_handle=list_handle)
        opened.append(handle)
        return handle

    def close_spy(handle: int) -> None:
        closed.append(handle)
        original_close(handle)

    monkeypatch.setattr(api, "open_child", open_child_spy)
    monkeypatch.setattr(api, "close", close_spy)
    parent = api.open_directory(root_path)
    try:
        api.verify_regular_child(parent, "normal.txt")
        with pytest.raises(RepositoryAccessDenied):
            api.verify_regular_child(parent, "directory")
        with pytest.raises(RepositoryAccessDenied):
            api.verify_regular_child(parent, "junction")
    finally:
        api.close(parent)

    assert all(handle in closed for handle in opened)


@pytest.mark.skipif(os.name != "nt", reason="Windows native handle disposition")
def test_windows_native_disposition_removes_normal_readonly_and_empty_entries(
    tmp_path: Path,
) -> None:
    root_path = _make_root(tmp_path)
    normal = root_path / "normal.txt"
    normal.write_text("normal", encoding="utf-8")
    readonly = root_path / "readonly.txt"
    readonly.write_text("readonly", encoding="utf-8")
    os.chmod(readonly, 0o444)
    empty = root_path / "empty"
    empty.mkdir()

    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    parent = api.open_directory(root_path)
    try:
        for name in ("normal.txt", "readonly.txt", "empty"):
            handle = api.open_child(parent, name)
            try:
                api.dispose(handle)
            finally:
                api.close(handle)
    finally:
        api.close(parent)

    assert not normal.exists()
    assert not readonly.exists()
    assert not empty.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows native directory disposition")
def test_windows_native_disposition_refuses_nonempty_directory(tmp_path: Path) -> None:
    root_path = _make_root(tmp_path)
    nonempty = root_path / "nonempty"
    nonempty.mkdir()
    marker = nonempty / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    parent = api.open_directory(root_path)
    handle = api.open_child(parent, "nonempty")
    try:
        with pytest.raises(RepositoryAccessDenied):
            api.dispose(handle)
    finally:
        api.close(handle)
        api.close(parent)

    assert nonempty.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows native reparse disposition")
def test_windows_native_disposition_removes_junction_without_touching_target(
    tmp_path: Path,
) -> None:
    root_path = _make_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("outside", encoding="utf-8")
    junction_path = root_path / "junction"
    _make_junction(junction_path, outside)

    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    parent = api.open_directory(root_path)
    handle = api.open_child(parent, "junction")
    try:
        assert int(api.information(handle).attributes) & paths._FILE_ATTRIBUTE_REPARSE_POINT
        api.dispose(handle)
    finally:
        api.close(handle)
        api.close(parent)

    assert not os.path.lexists(junction_path)
    assert marker.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows native sharing and handle cleanup")
def test_windows_native_locked_child_fails_then_retries_and_closes_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path = _make_root(tmp_path)
    locked = root_path / "locked.txt"
    locked.write_text("locked", encoding="utf-8")
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    parent = api.open_directory(root_path)
    closed: list[int] = []
    original_close = api.close

    def close_spy(handle: int) -> None:
        closed.append(handle)
        original_close(handle)

    monkeypatch.setattr(api, "close", close_spy)
    held = api.open_child(parent, "locked.txt")
    try:
        with pytest.raises((OSError, RepositoryAccessDenied)):
            api.open_child(parent, "locked.txt")
    finally:
        api.close(held)

    retried = api.open_child(parent, "locked.txt")
    api.close(retried)
    closed_before_injected_failure = len(closed)

    def fail_information(_handle: int) -> object:
        raise RepositoryAccessDenied("injected child information failure")

    monkeypatch.setattr(api, "information", fail_information)
    with pytest.raises(RepositoryAccessDenied):
        api.open_child(parent, "locked.txt")
    assert len(closed) == closed_before_injected_failure + 1

    api.close(parent)


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


@pytest.mark.skipif(os.name != "nt", reason="Windows directory sharing semantics")
def test_prepared_live_quarantine_allows_git_reopen_before_binding(tmp_path: Path) -> None:
    root_path, target, registration = _make_real_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    git = shutil.which("git") or "git"

    with root._prepare_worktree_quarantine(target.name, registration.name) as access:
        result = subprocess.run(
            [git, "-C", str(target), "branch", "--show-current"],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "feature"
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_registration(access)

    assert target.is_dir()
    assert registration.is_dir()


def test_prepared_live_quarantine_binds_before_ordered_mutation(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._prepare_worktree_quarantine(target.name, registration.name) as access:
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_target(access)
        root._bind_worktree_quarantine(access)
        with pytest.raises(RepositoryAccessDenied):
            root._bind_worktree_quarantine(access)

        root._quarantine_target(access)
        root._delete_target_quarantine(access)
        root._quarantine_registration(access)
        root._delete_registration_quarantine(access)

    assert not target.exists()
    assert not registration.exists()
    assert not (root_path / ".worktrees" / ".forge-quarantine" / target.name).exists()
    assert not (root_path / ".git" / ".forge-worktree-quarantine" / registration.name).exists()


def test_open_worktree_quarantine_prepares_then_binds_before_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    original_bind = root._bind_worktree_quarantine
    bound: list[object] = []

    def bind_spy(access: object) -> None:
        bound.append(access)
        original_bind(access)  # type: ignore[arg-type]

    monkeypatch.setattr(root, "_bind_worktree_quarantine", bind_spy)
    with root._open_worktree_quarantine(target.name, registration.name) as access:
        assert access._mutation_bound is True
        assert bound == [access]


@pytest.mark.parametrize(
    "change",
    (
        "target-disappears",
        "registration-disappears",
        "target-collision",
        "registration-collision",
        "locked",
        "malformed-gitdir",
    ),
)
def test_prepared_live_bind_rejects_changed_sources_or_metadata_without_mutation(
    tmp_path: Path, change: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    moved: Path | None = None

    with root._prepare_worktree_quarantine(target.name, registration.name) as access:
        if change == "target-disappears":
            moved = target.with_name("moved-target")
            target.rename(moved)
        elif change == "registration-disappears":
            moved = registration.with_name("moved-registration")
            try:
                registration.rename(moved)
            except OSError:
                if os.name == "nt":
                    pytest.skip("retained proof sharing blocks registration replacement")
                raise
        elif change == "target-collision":
            access.target_quarantine_path.mkdir()
        elif change == "registration-collision":
            access.registration_quarantine_path.mkdir()
        elif change == "locked":
            (registration / "locked").write_text("locked\n", encoding="utf-8")
        else:
            (registration / "gitdir").unlink()
            (registration / "gitdir").mkdir()

        with pytest.raises(RepositoryAccessDenied):
            root._bind_worktree_quarantine(access)
        assert access._mutation_bound is False
        assert not access.target_quarantine_path.is_dir() or change == "target-collision"
        assert (
            not access.registration_quarantine_path.is_dir() or change == "registration-collision"
        )

    if moved is not None:
        assert moved.is_dir()
    else:
        assert target.is_dir()
        assert registration.is_dir()


def test_bind_rejects_foreign_released_stale_and_absent_accesses(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    foreign = CanonicalRoot(root_path)

    with (
        root._prepare_worktree_quarantine(target.name, registration.name) as prepared,
        pytest.raises(RepositoryAccessDenied),
    ):
        foreign._bind_worktree_quarantine(prepared)

    with pytest.raises(RepositoryAccessDenied):
        root._bind_worktree_quarantine(prepared)

    _remove_flat_directory(target)
    with (
        root._open_stale_registration_quarantine(target.name, registration.name) as stale,
        pytest.raises(RepositoryAccessDenied),
    ):
        root._bind_worktree_quarantine(stale)

    _remove_flat_directory(registration)
    with (
        root._inspect_absent_worktree_removal(target.name) as absent,
        pytest.raises(RepositoryAccessDenied),
    ):
        root._bind_worktree_quarantine(absent)


def test_prepared_live_access_rejects_every_mutation_before_binding(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._prepare_worktree_quarantine(target.name, registration.name) as access:
        for mutation in (
            root._quarantine_target,
            root._delete_target_quarantine,
            root._quarantine_registration,
            root._delete_registration_quarantine,
        ):
            with pytest.raises(RepositoryAccessDenied):
                mutation(access)

    assert target.is_dir()
    assert registration.is_dir()


def test_prepared_live_access_holds_the_repository_mutation_lock(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    competing = CanonicalRoot(root_path)

    with (
        root._prepare_worktree_quarantine(target.name, registration.name),
        pytest.raises(RepositoryAccessDenied),
        competing._prepare_worktree_quarantine(target.name, registration.name),
    ):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows rename-sharing conflict")
def test_windows_prepared_live_bind_rejects_a_rename_sharing_conflict(
    tmp_path: Path,
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._prepare_worktree_quarantine(target.name, registration.name) as access:
        competing = api.open_directory_for_rename(target)
        try:
            with pytest.raises(RepositoryAccessDenied):
                root._bind_worktree_quarantine(access)
        finally:
            api.close(competing)
        assert access._mutation_bound is False
        assert target.is_dir()
        assert registration.is_dir()
        assert not access.target_quarantine_path.exists()
        assert not access.registration_quarantine_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows bind handle cleanup")
def test_windows_prepared_live_bind_closes_target_when_registration_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    original_open = api.open_directory_for_rename
    original_close = api.close
    opened: list[int] = []
    closed: list[int] = []

    def open_spy(path: Path) -> int:
        if opened:
            raise RepositoryAccessDenied("injected registration open failure")
        handle = original_open(path)
        opened.append(handle)
        return handle

    def close_spy(handle: int) -> None:
        closed.append(handle)
        original_close(handle)

    monkeypatch.setattr(api, "open_directory_for_rename", open_spy)
    monkeypatch.setattr(api, "close", close_spy)
    with root._prepare_worktree_quarantine(target.name, registration.name) as access:
        with pytest.raises(RepositoryAccessDenied):
            root._bind_worktree_quarantine(access)
        assert access._mutation_bound is False
        with pytest.raises((OSError, RepositoryAccessDenied)):
            api.identity(opened[0])

    assert opened
    assert opened[0] in closed
    assert target.is_dir()
    assert registration.is_dir()


@pytest.mark.parametrize("state", ("prepare", "bind", "bind-failure", "exception"))
def test_prepared_live_context_releases_all_retained_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    closed: list[int] = []
    retained: set[int] = set()
    proof_capabilities: list[int] = []
    if os.name == "nt":
        api = root._windows
        assert api is not None
        original_close = api.close

        def close_spy(handle: int) -> None:
            closed.append(handle)
            original_close(handle)

        monkeypatch.setattr(api, "close", close_spy)
    else:
        original_close = os.close

        def close_spy(handle: int) -> None:
            closed.append(handle)
            original_close(handle)

        monkeypatch.setattr(os, "close", close_spy)

    if state == "exception":
        with (
            pytest.raises(RuntimeError),
            root._prepare_worktree_quarantine(target.name, registration.name) as access,
        ):
            retained.update(access._resources)
            assert access._registration_gitdir_proof is not None
            proof_capabilities.append(access._registration_gitdir_proof.capability)
            raise RuntimeError("injected context failure")
    else:
        with root._prepare_worktree_quarantine(target.name, registration.name) as access:
            retained.update(access._resources)
            assert access._registration_gitdir_proof is not None
            proof_capabilities.append(access._registration_gitdir_proof.capability)
            if state == "bind":
                root._bind_worktree_quarantine(access)
                retained.update(access._resources)
            elif state == "bind-failure":
                (registration / "locked").write_text("locked\n", encoding="utf-8")
                with pytest.raises(RepositoryAccessDenied):
                    root._bind_worktree_quarantine(access)

    assert retained <= set(closed)
    assert all(closed.count(capability) == 1 for capability in proof_capabilities)


def test_quarantine_moves_exact_target_then_registration(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        root._quarantine_target(access)
        assert not target.exists()
        assert access.target_quarantine_path.is_dir()
        assert (access.target_quarantine_path / "outside-marker").is_file()

        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_registration(access)
        root._delete_target_quarantine(access)
        assert access._target_deleted is True

        root._quarantine_registration(access)
        assert not registration.exists()
        assert access.registration_quarantine_path.is_dir()
        assert (access.registration_quarantine_path / "gitdir").is_file()


def test_stale_registration_quarantine_accepts_absent_target_and_deletes_exact_metadata(
    tmp_path: Path,
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    root = CanonicalRoot(root_path)

    with root._open_stale_registration_quarantine(target.name, registration.name) as access:
        assert access._target_initially_present is False
        assert access._target_moved is False
        assert access._target_deleted is False

        root._quarantine_registration(access)
        assert not registration.exists()
        assert (access.registration_quarantine_path / "gitdir").is_file()

        root._delete_registration_quarantine(access)
        assert access._registration_deleted is True
        assert not access.registration_quarantine_path.exists()


@pytest.mark.parametrize("reappeared", ("target", "target-quarantine"))
def test_stale_registration_revalidates_target_absence_before_move(
    tmp_path: Path, reappeared: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    root = CanonicalRoot(root_path)

    with root._open_stale_registration_quarantine(target.name, registration.name) as access:
        unexpected = target if reappeared == "target" else access.target_quarantine_path
        unexpected.mkdir()
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_registration(access)
        assert registration.is_dir()
        assert not access.registration_quarantine_path.exists()
        unexpected.rmdir()


@pytest.mark.parametrize("reappeared", ("target", "target-quarantine"))
def test_stale_registration_revalidates_target_absence_before_delete(
    tmp_path: Path, reappeared: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    root = CanonicalRoot(root_path)

    with root._open_stale_registration_quarantine(target.name, registration.name) as access:
        root._quarantine_registration(access)
        unexpected = target if reappeared == "target" else access.target_quarantine_path
        unexpected.mkdir()
        with pytest.raises(RepositoryAccessDenied):
            root._delete_registration_quarantine(access)
        assert access.registration_quarantine_path.is_dir()
        assert (access.registration_quarantine_path / "gitdir").is_file()
        unexpected.rmdir()
        root._delete_registration_quarantine(access)


def test_stale_registration_refuses_locked_or_malformed_registration_without_mutation(
    tmp_path: Path,
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    root = CanonicalRoot(root_path)
    locked = registration / "locked"
    locked.write_text("locked\n", encoding="utf-8")

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_stale_registration_quarantine(target.name, registration.name),
    ):
        pass
    assert registration.is_dir()

    locked.unlink()
    (registration / "gitdir").unlink()
    (registration / "gitdir").mkdir()
    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_stale_registration_quarantine(target.name, registration.name),
    ):
        pass
    assert registration.is_dir()


@pytest.mark.parametrize("stale", (False, True))
def test_quarantine_accepts_a_relative_exact_gitdir_proof(tmp_path: Path, stale: bool) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    relative = os.path.relpath(target / ".git", registration)
    (registration / "gitdir").write_bytes(f"{relative}\n".encode())
    if stale:
        _remove_flat_directory(target)
    root = CanonicalRoot(root_path)
    opener = root._open_stale_registration_quarantine if stale else root._open_worktree_quarantine

    with opener(target.name, registration.name) as access:
        assert access._registration_gitdir_content == f"{relative}\n".encode()
        if not stale:
            root._quarantine_target(access)
            root._delete_target_quarantine(access)
        root._quarantine_registration(access)
        root._delete_registration_quarantine(access)

    assert not target.exists()
    assert not registration.exists()


@pytest.mark.parametrize("stale", (False, True))
def test_quarantine_rejects_a_wrong_target_regular_gitdir_proof_without_mutation(
    tmp_path: Path, stale: bool
) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    outside.mkdir()
    alternate = outside / "alternate" / ".git"
    alternate.parent.mkdir()
    alternate.write_text("alternate\n", encoding="utf-8")
    (registration / "gitdir").write_bytes(f"{alternate}\n".encode())
    if stale:
        _remove_flat_directory(target)
    root = CanonicalRoot(root_path)
    opener = root._open_stale_registration_quarantine if stale else root._open_worktree_quarantine

    with (
        pytest.raises(RepositoryAccessDenied),
        opener(target.name, registration.name),
    ):
        pass

    assert target.is_dir() == (not stale)
    assert registration.is_dir()


@pytest.mark.parametrize(
    "contents",
    (
        b"/one\n/two\n",
        b"/one\r\n",
        b"/one\rfoo\n",
        b"\xff\n",
        b"",
        b"/one",
        b"/one\n" + b"x" * 4096,
    ),
)
@pytest.mark.parametrize("stale", (False, True))
def test_quarantine_rejects_malformed_or_oversized_gitdir_proof_without_mutation(
    tmp_path: Path, contents: bytes, stale: bool
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    (registration / "gitdir").write_bytes(contents)
    if stale:
        _remove_flat_directory(target)
    root = CanonicalRoot(root_path)
    opener = root._open_stale_registration_quarantine if stale else root._open_worktree_quarantine

    with (
        pytest.raises(RepositoryAccessDenied),
        opener(target.name, registration.name),
    ):
        pass

    assert target.is_dir() == (not stale)
    assert registration.is_dir()


@pytest.mark.parametrize("stale", (False, True))
def test_quarantine_rejects_a_linked_gitdir_proof_without_mutation(
    tmp_path: Path, stale: bool
) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    outside.mkdir()
    linked = outside / "gitdir"
    linked.write_bytes(f"{target / '.git'}\n".encode())
    (registration / "gitdir").unlink()
    _make_symlink(registration / "gitdir", linked, directory=False)
    if stale:
        _remove_flat_directory(target)
    root = CanonicalRoot(root_path)
    opener = root._open_stale_registration_quarantine if stale else root._open_worktree_quarantine

    with (
        pytest.raises(RepositoryAccessDenied),
        opener(target.name, registration.name),
    ):
        pass

    assert target.is_dir() == (not stale)
    assert registration.is_dir()


def test_prepared_bind_rejects_a_substituted_gitdir_proof_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    outside.mkdir()
    replacement = outside / "replacement-gitdir"
    replacement.write_bytes(f"{target / '.git'}\n".encode())
    root = CanonicalRoot(root_path)
    original = registration / "gitdir"
    moved = registration / "gitdir.original"
    with root._prepare_worktree_quarantine(target.name, registration.name) as access:
        if os.name == "nt":
            api = root._windows
            assert api is not None
            replacement_handle = api.open_regular(replacement)

            def substitute(_parent: int, _name: str) -> int:
                return replacement_handle

            monkeypatch.setattr(api, "open_proof_child", substitute)
        else:
            original.rename(moved)
            original.write_bytes(f"{target / '.git'}\n".encode())

        with pytest.raises(RepositoryAccessDenied):
            root._bind_worktree_quarantine(access)
        if os.name != "nt":
            original.unlink()
            moved.rename(original)

    assert target.is_dir()
    assert registration.is_dir()


def test_stale_registration_rejects_a_substituted_gitdir_proof_before_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    outside.mkdir()
    replacement = outside / "replacement-gitdir"
    replacement.write_bytes(f"{target / '.git'}\n".encode())
    root = CanonicalRoot(root_path)
    original = registration / "gitdir"
    moved = registration / "gitdir.original"

    with root._open_stale_registration_quarantine(target.name, registration.name) as access:
        if os.name == "nt":
            api = root._windows
            assert api is not None
            replacement_handle = api.open_regular(replacement)

            def substitute(_parent: int, _name: str) -> int:
                return replacement_handle

            monkeypatch.setattr(api, "open_proof_child", substitute)
        else:
            original.rename(moved)
            original.write_bytes(f"{target / '.git'}\n".encode())

        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_registration(access)
        if os.name != "nt":
            original.unlink()
            moved.rename(original)

    assert not target.exists()
    assert registration.is_dir()


def test_prepared_bind_rejects_a_closed_gitdir_proof_without_mutation(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._prepare_worktree_quarantine(target.name, registration.name) as access:
        proof = access._registration_gitdir_proof
        assert proof is not None
        if os.name == "nt":
            api = root._windows
            assert api is not None
            api.close(proof.capability)
        else:
            os.close(proof.capability)
        with pytest.raises(RepositoryAccessDenied):
            root._bind_worktree_quarantine(access)

    assert target.is_dir()
    assert registration.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows proof sharing semantics")
def test_windows_prepared_proof_rejects_an_already_open_writer(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    raw = api._create_file(
        str(registration / "gitdir"),
        paths._GENERIC_WRITE,
        paths._FILE_SHARE_READ | paths._FILE_SHARE_WRITE,
        None,
        paths._OPEN_EXISTING,
        paths._FILE_ATTRIBUTE_NORMAL | paths._FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    writer = api._value(raw)
    try:
        with (
            pytest.raises(RepositoryAccessDenied),
            root._prepare_worktree_quarantine(target.name, registration.name),
        ):
            pass
    finally:
        api.close(writer)

    assert target.is_dir()
    assert registration.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows registration rename retry")
def test_windows_registration_rename_failure_reacquires_proof_for_same_access_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    original_rename = api.rename_directory
    original_close = api.close
    failed = False
    proof_closes = 0
    observed_access: Any | None = None

    def close_spy(handle: int) -> None:
        nonlocal proof_closes
        if (
            observed_access is not None
            and observed_access._registration_gitdir_proof is not None
            and handle == observed_access._registration_gitdir_proof.capability
        ):
            proof_closes += 1
        original_close(handle)

    monkeypatch.setattr(api, "close", close_spy)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        observed_access = access
        root._quarantine_target(access)
        root._delete_target_quarantine(access)

        def fail_first_registration_rename(handle: int, parent: int, name: str) -> None:
            nonlocal failed
            if not failed and handle == access._registration.handle.capability:
                failed = True
                raise OSError("injected registration rename failure")
            original_rename(handle, parent, name)

        monkeypatch.setattr(api, "rename_directory", fail_first_registration_rename)
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_registration(access)

        assert failed is True
        assert registration.is_dir()
        assert access._registration_gitdir_proof is not None
        assert access._registration_gitdir_proof_retired is False

        root._quarantine_registration(access)
        root._delete_registration_quarantine(access)

    assert not registration.exists()
    assert proof_closes == 2


@pytest.mark.parametrize("stale", (False, True))
def test_registration_gitdir_proof_closes_once_after_successful_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    if stale:
        _remove_flat_directory(target)
    root = CanonicalRoot(root_path)
    observed_access: Any | None = None
    proof_closes = 0

    if os.name == "nt":
        api = root._windows
        assert api is not None
        original_close = api.close

        def close_spy(handle: int) -> None:
            nonlocal proof_closes
            if (
                observed_access is not None
                and observed_access._registration_gitdir_proof is not None
                and handle == observed_access._registration_gitdir_proof.capability
            ):
                proof_closes += 1
            original_close(handle)

        monkeypatch.setattr(api, "close", close_spy)
    else:
        original_close = os.close

        def close_spy(handle: int) -> None:
            nonlocal proof_closes
            if (
                observed_access is not None
                and observed_access._registration_gitdir_proof is not None
                and handle == observed_access._registration_gitdir_proof.capability
            ):
                proof_closes += 1
            original_close(handle)

        monkeypatch.setattr(os, "close", close_spy)

    opener = root._open_stale_registration_quarantine if stale else root._open_worktree_quarantine
    with opener(target.name, registration.name) as access:
        observed_access = access
        if not stale:
            root._quarantine_target(access)
            root._delete_target_quarantine(access)
        root._quarantine_registration(access)
        root._delete_registration_quarantine(access)

    assert proof_closes == 1
    assert not registration.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows proof retirement ordering")
def test_windows_registration_proof_is_rechecked_retired_then_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    original_verify = root._verify_windows_registration_state
    original_retire = root._retire_windows_registration_gitdir_proof
    original_rename = api.rename_directory
    events: list[str] = []

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        root._quarantine_target(access)
        root._delete_target_quarantine(access)

        def verify_spy(candidate: Any) -> None:
            events.append("verify")
            original_verify(candidate)

        def retire_spy(candidate: Any) -> None:
            events.append("retire")
            original_retire(candidate)

        def rename_spy(handle: int, parent: int, name: str) -> None:
            if handle == access._registration.handle.capability:
                events.append("rename")
            original_rename(handle, parent, name)

        monkeypatch.setattr(root, "_verify_windows_registration_state", verify_spy)
        monkeypatch.setattr(root, "_retire_windows_registration_gitdir_proof", retire_spy)
        monkeypatch.setattr(api, "rename_directory", rename_spy)

        root._quarantine_registration(access)
        assert events[-3:] == ["verify", "retire", "rename"]
        root._delete_registration_quarantine(access)

    assert not registration.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained registration validation")
@pytest.mark.parametrize("stale", (False, True))
def test_windows_removal_validates_registration_children_through_retained_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    if stale:
        _remove_flat_directory(target)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    original_open_child = api.open_child
    original_open_proof_child = api.open_proof_child
    original_lexists = paths.os.path.lexists
    opened_children: list[tuple[int, str]] = []

    def open_child_spy(parent: int, name: str, *, list_handle: bool = False) -> int:
        opened_children.append((parent, name))
        return original_open_child(parent, name, list_handle=list_handle)

    def open_proof_child_spy(parent: int, name: str) -> int:
        opened_children.append((parent, name))
        return original_open_proof_child(parent, name)

    def reject_lexical_registration_children(path: Path | str) -> bool:
        if Path(path) in {registration / "locked", registration / "gitdir"}:
            raise AssertionError("registration children must use the retained handle")
        return original_lexists(path)

    def reject_lexical_regular_open(_path: Path) -> int:
        raise AssertionError("registration gitdir must use the retained handle")

    monkeypatch.setattr(api, "open_child", open_child_spy)
    monkeypatch.setattr(api, "open_proof_child", open_proof_child_spy)
    monkeypatch.setattr(paths.os.path, "lexists", reject_lexical_registration_children)
    monkeypatch.setattr(api, "open_regular", reject_lexical_regular_open)
    opener = root._open_stale_registration_quarantine if stale else root._open_worktree_quarantine

    with opener(target.name, registration.name) as access:
        registration_entry = access._registration
        assert registration_entry is not None
        registration_handle = registration_entry.handle.capability
        assert (registration_handle, "locked") in opened_children
        assert (registration_handle, "gitdir") in opened_children


@pytest.mark.skipif(os.name != "nt", reason="Windows retained registration validation")
def test_windows_stale_registration_rechecks_a_new_lock_without_mutation(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    root = CanonicalRoot(root_path)

    with root._open_stale_registration_quarantine(target.name, registration.name) as access:
        locked = registration / "locked"
        locked.write_text("locked\n", encoding="utf-8")
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_registration(access)
        assert registration.is_dir()
        assert not access.registration_quarantine_path.exists()
        locked.unlink()


def test_stale_registration_refuses_linked_registration_without_following(
    tmp_path: Path,
) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    outside.mkdir()
    moved = outside / "registration"
    registration.rename(moved)
    _make_symlink(registration, moved, directory=True)
    root = CanonicalRoot(root_path)

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_stale_registration_quarantine(target.name, registration.name),
    ):
        pass
    assert (moved / "gitdir").is_file()


@pytest.mark.parametrize("collision_scope", ("target", "registration"))
def test_stale_registration_refuses_exact_quarantine_collision_without_mutation(
    tmp_path: Path, collision_scope: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    root = CanonicalRoot(root_path)
    with root._inspect_absent_worktree_removal(target.name):
        pass
    collision = (
        root_path / ".worktrees" / ".forge-quarantine" / target.name
        if collision_scope == "target"
        else root_path / ".git" / ".forge-worktree-quarantine" / registration.name
    )
    collision.mkdir()

    with (
        pytest.raises(RepositoryAccessDenied),
        root._open_stale_registration_quarantine(target.name, registration.name),
    ):
        pass
    assert registration.is_dir()


def test_absent_removal_inspection_has_no_sources_or_mutation_surface(tmp_path: Path) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    _remove_flat_directory(registration)
    root = CanonicalRoot(root_path)

    with root._inspect_absent_worktree_removal(target.name) as access:
        assert access._target_initially_present is False
        assert access._registration_initially_present is False
        assert access._target is None
        assert access._registration is None
        with pytest.raises(RepositoryAccessDenied):
            _ = access.registration_path
        with pytest.raises(RepositoryAccessDenied):
            _ = access.registration_quarantine_path
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_registration(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_registration_quarantine(access)


@pytest.mark.parametrize("present_scope", ("target", "target-quarantine"))
def test_absent_removal_inspection_refuses_initial_exact_presence(
    tmp_path: Path, present_scope: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    if present_scope == "target-quarantine":
        _remove_flat_directory(target)
        _remove_flat_directory(registration)
        with root._inspect_absent_worktree_removal(target.name):
            pass
        (root_path / ".worktrees" / ".forge-quarantine" / target.name).mkdir()

    with pytest.raises(RepositoryAccessDenied), root._inspect_absent_worktree_removal(target.name):
        pass


@pytest.mark.parametrize("reappeared", ("target", "target-quarantine"))
def test_absent_removal_inspection_revalidates_absence_before_return(
    tmp_path: Path, reappeared: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    _remove_flat_directory(registration)
    root = CanonicalRoot(root_path)

    with (
        pytest.raises(RepositoryAccessDenied),
        root._inspect_absent_worktree_removal(target.name) as access,
    ):
        unexpected = target if reappeared == "target" else access.target_quarantine_path
        unexpected.mkdir()


@pytest.mark.parametrize("mode", ("stale-registration", "absent"))
def test_removal_state_capability_rejects_foreign_released_and_competing_use(
    tmp_path: Path, mode: str
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    if mode == "absent":
        _remove_flat_directory(registration)
    root = CanonicalRoot(root_path)
    foreign = CanonicalRoot(root_path)

    def open_for(owner: CanonicalRoot) -> Any:
        if mode == "stale-registration":
            return owner._open_stale_registration_quarantine(target.name, registration.name)
        return owner._inspect_absent_worktree_removal(target.name)

    with open_for(root) as access:
        with pytest.raises(RepositoryAccessDenied):
            foreign._verify_worktree_removal_state(access)
        with pytest.raises(RepositoryAccessDenied), open_for(foreign):
            pass

    with pytest.raises(RepositoryAccessDenied):
        root._verify_worktree_removal_state(access)
    with open_for(foreign):
        pass


def test_stale_registration_rejects_substituted_retained_identity_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    _remove_flat_directory(target)
    outside.mkdir()
    root = CanonicalRoot(root_path)

    with root._open_stale_registration_quarantine(target.name, registration.name) as access:
        registration_entry = access._registration
        assert registration_entry is not None
        capability = registration_entry.handle.capability
        if os.name == "nt":
            api = root._windows
            assert api is not None
            original_identity = api.identity

            def substituted_identity(handle: int) -> object:
                identity = tuple(original_identity(handle))
                if handle == capability:
                    return (identity[0], identity[1], identity[2] + 1)
                return identity

            with monkeypatch.context() as patcher:
                patcher.setattr(api, "identity", substituted_identity)
                with pytest.raises(RepositoryAccessDenied):
                    root._quarantine_registration(access)
        else:
            saved_registration = os.dup(capability)
            os.close(capability)
            replacement: int | None = None
            try:
                replacement = os.open(
                    outside,
                    os.O_RDONLY | paths._O_DIRECTORY | paths._O_NOFOLLOW | paths._O_CLOEXEC,
                )
                if replacement != capability:
                    os.dup2(replacement, capability)
                with pytest.raises(RepositoryAccessDenied):
                    root._quarantine_registration(access)
            finally:
                os.dup2(saved_registration, capability)
                os.close(saved_registration)
                if replacement is not None and replacement != capability:
                    os.close(replacement)

        assert registration.is_dir()
        assert not access.registration_quarantine_path.exists()


def test_quarantine_delete_requires_a_moved_target_and_keeps_registration_order(
    tmp_path: Path,
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_registration_quarantine(access)
        root._delete_target_quarantine(access)
        assert access._target_deleted is True


@pytest.mark.skipif(os.name != "nt", reason="Windows native quarantine deletion")
def test_windows_quarantine_delete_handles_nested_readonly_hardlink_and_junction(
    tmp_path: Path,
) -> None:
    root_path, target, _registration, outside = _make_quarantine_fixture(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    readonly = nested / "readonly.txt"
    readonly.write_text("readonly\n", encoding="utf-8")
    os.chmod(readonly, 0o444)
    outside.mkdir()
    outside_marker = outside / "outside-marker"
    outside_marker.write_text("outside\n", encoding="utf-8")
    os.link(outside_marker, target / "hardlink")
    _make_junction(target / "linked", outside)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        root._delete_target_quarantine(access)
        assert access._target_deleted is True
        assert not os.path.lexists(access.target_quarantine_path)

    assert outside_marker.read_text(encoding="utf-8") == "outside\n"
    assert outside.is_dir()
    assert not (outside / "readonly.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows native proof-last deletion")
def test_windows_quarantine_delete_is_proof_last_for_target_and_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None
    opened_names: dict[int, str] = {}
    dispose_order: list[str] = []
    original_open_child = api.open_child
    original_dispose = api.dispose

    def open_child_spy(parent: int, name: str, *, list_handle: bool = False) -> int:
        handle = original_open_child(parent, name, list_handle=list_handle)
        opened_names[handle] = name
        return handle

    def dispose_spy(handle: int) -> None:
        if handle == access._target.handle.capability and not access._target_root_retired:
            dispose_order.append("target-root")
        elif (
            handle == access._registration.handle.capability
            and not access._registration_root_retired
        ):
            dispose_order.append("registration-root")
        else:
            dispose_order.append(opened_names.get(handle, "unknown"))
        original_dispose(handle)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        root._quarantine_target(access)
        monkeypatch.setattr(api, "open_child", open_child_spy)
        monkeypatch.setattr(api, "dispose", dispose_spy)
        with pytest.raises(RepositoryAccessDenied):
            root._quarantine_registration(access)
        root._delete_target_quarantine(access)
        assert dispose_order[-2:] == [".git", "target-root"]
        root._quarantine_registration(access)
        root._delete_registration_quarantine(access)
        assert dispose_order[-2:] == ["gitdir", "registration-root"]


@pytest.mark.skipif(os.name != "nt", reason="Windows native missing-proof retry")
def test_windows_quarantine_delete_missing_proof_refuses_nonempty_then_retries_empty(
    tmp_path: Path,
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        (access.target_quarantine_path / ".git").unlink()
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        assert (access.target_quarantine_path / "outside-marker").is_file()
        (access.target_quarantine_path / "outside-marker").unlink()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True
        root._delete_target_quarantine(access)
    assert not os.path.lexists(access.target_quarantine_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows native locked-child retry")
def test_windows_quarantine_delete_locked_child_fails_then_retries(tmp_path: Path) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        lock = api.open_child(access._target_quarantine.handle.capability, "outside-marker")
        try:
            with pytest.raises((OSError, RepositoryAccessDenied)):
                root._delete_target_quarantine(access)
            assert access._target_deleted is False
            assert (access.target_quarantine_path / ".git").is_file()
        finally:
            api.close(lock)
        root._delete_target_quarantine(access)
        assert access._target_deleted is True


@pytest.mark.skipif(os.name != "nt", reason="Windows native proof failure retry")
def test_windows_quarantine_delete_proof_failure_preserves_evidence_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        probe = api.open_child(access._target_quarantine.handle.capability, ".git")
        try:
            proof_identity = tuple(api.identity(probe))
        finally:
            api.close(probe)
        original_dispose = api.dispose
        failures = 0

        def fail_proof_once(handle: int) -> None:
            nonlocal failures
            if tuple(api.identity(handle)) == proof_identity and failures == 0:
                failures += 1
                raise RepositoryAccessDenied("injected proof disposition failure")
            original_dispose(handle)

        monkeypatch.setattr(api, "dispose", fail_proof_once)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert failures == 1
        assert access._target_deleted is False
        assert (access.target_quarantine_path / ".git").is_file()
        monkeypatch.undo()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True


@pytest.mark.skipif(os.name != "nt", reason="Windows native nested-directory failure retry")
def test_windows_quarantine_delete_nested_directory_failure_closes_handles_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child\n", encoding="utf-8")
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        probe = api.open_child(access._target_quarantine.handle.capability, "nested")
        try:
            nested_identity = tuple(api.identity(probe))
        finally:
            api.close(probe)
        opened: set[int] = set()
        closed: set[int] = set()
        original_open_child = api.open_child
        original_close = api.close
        original_dispose = api.dispose
        failures = 0

        def open_child_spy(parent: int, name: str, *, list_handle: bool = False) -> int:
            handle = original_open_child(parent, name, list_handle=list_handle)
            opened.add(handle)
            return handle

        def close_spy(handle: int) -> None:
            if handle in opened:
                closed.add(handle)
            original_close(handle)

        def fail_nested_once(handle: int) -> None:
            nonlocal failures
            if tuple(api.identity(handle)) == nested_identity and failures == 0:
                failures += 1
                raise RepositoryAccessDenied("injected nested disposition failure")
            original_dispose(handle)

        monkeypatch.setattr(api, "open_child", open_child_spy)
        monkeypatch.setattr(api, "close", close_spy)
        monkeypatch.setattr(api, "dispose", fail_nested_once)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert failures == 1
        assert opened <= closed
        assert access._target_deleted is False
        assert (access.target_quarantine_path / ".git").is_file()
        assert (access.target_quarantine_path / "nested").is_dir()
        assert not any((access.target_quarantine_path / "nested").iterdir())
        monkeypatch.undo()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True


@pytest.mark.skipif(os.name != "nt", reason="Windows native root-disposition retry")
def test_windows_quarantine_delete_root_failure_allows_exact_empty_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        original_dispose = api.dispose
        failures = 0

        def fail_root_once(handle: int) -> None:
            nonlocal failures
            if handle == access._target.handle.capability and failures == 0:
                failures += 1
                raise RepositoryAccessDenied("injected root disposition failure")
            original_dispose(handle)

        monkeypatch.setattr(api, "dispose", fail_root_once)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert failures == 1
        assert access._target_deleted is False
        assert access.target_quarantine_path.is_dir()
        assert not os.path.lexists(access.target_quarantine_path / ".git")
        assert not os.path.lexists(access.target_quarantine_path / "outside-marker")
        monkeypatch.undo()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True


@pytest.mark.skipif(os.name != "nt", reason="Windows final absence retry")
def test_windows_quarantine_delete_retries_after_uncertain_root_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        original_enumerate = api.enumerate_names
        failures = 0

        def fail_final_absence(handle: int) -> tuple[str, ...]:
            nonlocal failures
            names = original_enumerate(handle)
            if handle == access._target_quarantine_parent.capability and failures == 0:
                failures += 1
                raise RepositoryAccessDenied("injected final absence failure")
            return names

        monkeypatch.setattr(api, "enumerate_names", fail_final_absence)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert failures == 1
        assert access._target_deleted is False

        monkeypatch.undo()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True


@pytest.mark.skipif(os.name != "nt", reason="Windows bounded quarantine enumeration")
def test_windows_quarantine_delete_does_not_reenumerate_parent_per_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    (target / "outside-marker").unlink()
    for index in range(8):
        (target / f"entry-{index}").write_text("entry\n", encoding="utf-8")
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        root_list_handle = access._target_quarantine.handle.capability
        original_enumerate = api.enumerate_names
        calls: dict[int, int] = {}

        def count_enumerations(handle: int) -> tuple[str, ...]:
            calls[handle] = calls.get(handle, 0) + 1
            return original_enumerate(handle)

        monkeypatch.setattr(api, "enumerate_names", count_enumerations)
        root._delete_target_quarantine(access)

        assert calls[root_list_handle] <= 6
        assert sum(calls.values()) <= 7
        assert access._target_deleted is True


@pytest.mark.skipif(os.name != "nt", reason="Windows native identity-bound deletion")
def test_windows_quarantine_delete_refuses_child_identity_change_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child\n", encoding="utf-8")
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        original_open_child = api.open_child
        original_identity = api.identity
        child_pins: set[int] = set()

        def open_child_spy(parent: int, name: str, *, list_handle: bool = False) -> int:
            handle = original_open_child(parent, name, list_handle=list_handle)
            if name == "nested" and not list_handle:
                child_pins.add(handle)
            return handle

        def mismatched_identity(handle: int) -> object:
            identity = tuple(original_identity(handle))
            if handle in child_pins:
                return (identity[0] + 1, *identity[1:])
            return identity

        monkeypatch.setattr(api, "open_child", open_child_spy)
        monkeypatch.setattr(api, "identity", mismatched_identity)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert child_pins
        assert access._target_deleted is False
        assert (access.target_quarantine_path / ".git").is_file()
        assert (access.target_quarantine_path / "nested" / "child.txt").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows native quarantine bounds")
def test_windows_quarantine_delete_enforces_depth_and_entry_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    (nested / "deep").mkdir()
    root = CanonicalRoot(root_path)

    monkeypatch.setattr(paths, "_QUARANTINE_MAX_DEPTH", 1)
    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        assert (access.target_quarantine_path / ".git").is_file()

    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path / "entries")
    for index in range(3):
        (target / f"entry-{index}").write_text("entry\n", encoding="utf-8")
    root = CanonicalRoot(root_path)
    monkeypatch.setattr(paths, "_QUARANTINE_MAX_DEPTH", 128)
    monkeypatch.setattr(paths, "_QUARANTINE_MAX_ENTRIES", 2)
    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        assert (access.target_quarantine_path / ".git").is_file()
        assert (access.target_quarantine_path / "entry-0").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows native malformed enumeration")
def test_windows_quarantine_delete_rejects_malformed_enumeration_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    api = root._windows
    assert api is not None

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        original_enumerate = api.enumerate_names

        def malformed(handle: int) -> tuple[str, ...]:
            names = original_enumerate(handle)
            if handle == access._target_quarantine.handle.capability:
                return (*names, "invalid/name")
            return names

        monkeypatch.setattr(api, "enumerate_names", malformed)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        assert (access.target_quarantine_path / ".git").is_file()
        assert (access.target_quarantine_path / "outside-marker").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative quarantine deletion")
def test_quarantine_delete_is_proof_last_and_registration_ordered(tmp_path: Path) -> None:
    root_path, target, registration, outside = _make_quarantine_fixture(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    (nested / "readonly.txt").write_text("readonly\n", encoding="utf-8")
    os.chmod(nested / "readonly.txt", 0o444)
    outside.mkdir()
    (outside / "marker.txt").write_text("outside\n", encoding="utf-8")
    _make_symlink(target / "linked", outside, directory=True)
    os.link(target / "outside-marker", target / "hardlink")
    root = CanonicalRoot(root_path)
    unlink_names: list[str] = []
    original_unlink = os.unlink

    def unlink_spy(name: str, *args: Any, **kwargs: Any) -> None:
        unlink_names.append(name)
        original_unlink(name, *args, **kwargs)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_registration_quarantine(access)
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(paths.os, "unlink", unlink_spy)
        try:
            root._delete_target_quarantine(access)
        finally:
            monkeypatch.undo()
        assert access._target_deleted is True
        assert not access.target_quarantine_path.exists()
        assert not target.exists()
        assert (outside / "marker.txt").read_text(encoding="utf-8") == "outside\n"
        assert unlink_names[-1] == ".git"

        root._quarantine_registration(access)
        root._delete_registration_quarantine(access)
        assert access._registration_deleted is True
        assert not access.registration_quarantine_path.exists()
    assert not registration.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative quarantine deletion")
def test_quarantine_delete_missing_proof_refuses_nonempty_and_retries_empty_root(
    tmp_path: Path,
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        proof = access.target_quarantine_path / ".git"
        proof.unlink()
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access.target_quarantine_path.is_dir()
        assert (access.target_quarantine_path / "outside-marker").is_file()
        (access.target_quarantine_path / "outside-marker").unlink()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True
        root._delete_target_quarantine(access)
    assert not access.target_quarantine_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative quarantine deletion")
def test_quarantine_delete_partial_failures_preserve_truthful_retry_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    (target / "child.txt").write_text("child\n", encoding="utf-8")
    root = CanonicalRoot(root_path)
    calls = 0
    original_unlink = paths.os.unlink

    def fail_once(name: str, *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        if name == "child.txt" and calls == 0:
            calls += 1
            raise OSError("injected child failure")
        original_unlink(name, *args, **kwargs)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        monkeypatch.setattr(paths.os, "unlink", fail_once)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        assert (access.target_quarantine_path / ".git").is_file()
        assert (access.target_quarantine_path / "child.txt").is_file()
        monkeypatch.undo()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative quarantine deletion")
def test_quarantine_delete_root_failure_allows_exact_empty_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    original_rmdir = paths.os.rmdir
    calls = 0

    def fail_root_once(name: str, *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        if name == target.name and calls == 0:
            calls += 1
            raise OSError("injected root failure")
        original_rmdir(name, *args, **kwargs)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        monkeypatch.setattr(paths.os, "rmdir", fail_root_once)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        assert access.target_quarantine_path.is_dir()
        assert not (access.target_quarantine_path / ".git").exists()
        monkeypatch.undo()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative quarantine deletion")
@pytest.mark.parametrize("scope, proof_name", (("target", ".git"), ("registration", "gitdir")))
def test_quarantine_delete_proof_failure_preserves_exact_evidence_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    proof_name: str,
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        root._quarantine_target(access)
        if scope == "target":
            quarantine = access.target_quarantine_path
            (quarantine / "outside-marker").unlink()
            deleted_attribute = "_target_deleted"
            quarantine_descriptor = access._target_quarantine.handle.capability
            delete_quarantine = root._delete_target_quarantine
        else:
            root._delete_target_quarantine(access)
            root._quarantine_registration(access)
            quarantine = access.registration_quarantine_path
            deleted_attribute = "_registration_deleted"
            quarantine_descriptor = access._registration_quarantine.handle.capability
            delete_quarantine = root._delete_registration_quarantine

        proof = quarantine / proof_name
        proof_contents = proof.read_text(encoding="utf-8")
        assert {entry.name for entry in quarantine.iterdir()} == {proof_name}

        opened_proof: list[int] = []
        closed_descriptors: list[int] = []
        failures = 0
        original_open = paths.os.open
        original_close = paths.os.close
        original_unlink = paths.os.unlink

        def open_spy(name: Any, *args: Any, **kwargs: Any) -> int:
            descriptor = original_open(name, *args, **kwargs)
            if name == proof_name and kwargs.get("dir_fd") == quarantine_descriptor:
                opened_proof.append(descriptor)
            return descriptor

        def close_spy(descriptor: int) -> None:
            if descriptor in opened_proof:
                closed_descriptors.append(descriptor)
            original_close(descriptor)

        def fail_proof_once(name: Any, *args: Any, **kwargs: Any) -> None:
            nonlocal failures
            if (
                name == proof_name
                and kwargs.get("dir_fd") == quarantine_descriptor
                and failures == 0
            ):
                failures += 1
                raise OSError("injected proof failure")
            original_unlink(name, *args, **kwargs)

        monkeypatch.setattr(paths.os, "open", open_spy)
        monkeypatch.setattr(paths.os, "close", close_spy)
        monkeypatch.setattr(paths.os, "unlink", fail_proof_once)
        with pytest.raises(RepositoryAccessDenied):
            delete_quarantine(access)
        assert failures == 1
        assert getattr(access, deleted_attribute) is False
        assert quarantine.is_dir()
        assert proof.read_text(encoding="utf-8") == proof_contents
        assert {entry.name for entry in quarantine.iterdir()} == {proof_name}
        assert opened_proof
        assert set(opened_proof) <= set(closed_descriptors)

        monkeypatch.undo()
        delete_quarantine(access)
        assert getattr(access, deleted_attribute) is True
        assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative quarantine deletion")
def test_quarantine_delete_nested_directory_failure_preserves_empty_child_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, registration, _outside = _make_quarantine_fixture(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child\n", encoding="utf-8")
    root = CanonicalRoot(root_path)
    opened_nested: list[int] = []
    closed_descriptors: list[int] = []
    failures = 0
    original_open = paths.os.open
    original_close = paths.os.close
    original_rmdir = paths.os.rmdir

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        root._quarantine_target(access)
        quarantine = access.target_quarantine_path
        quarantine_descriptor = access._target_quarantine.handle.capability

        def open_spy(name: Any, *args: Any, **kwargs: Any) -> int:
            descriptor = original_open(name, *args, **kwargs)
            if name == "nested" and kwargs.get("dir_fd") == quarantine_descriptor:
                opened_nested.append(descriptor)
            return descriptor

        def close_spy(descriptor: int) -> None:
            if descriptor in opened_nested:
                closed_descriptors.append(descriptor)
            original_close(descriptor)

        def fail_nested_rmdir(name: Any, *args: Any, **kwargs: Any) -> None:
            nonlocal failures
            if name == "nested" and kwargs.get("dir_fd") == quarantine_descriptor and failures == 0:
                failures += 1
                raise OSError("injected nested directory failure")
            original_rmdir(name, *args, **kwargs)

        monkeypatch.setattr(paths.os, "open", open_spy)
        monkeypatch.setattr(paths.os, "close", close_spy)
        monkeypatch.setattr(paths.os, "rmdir", fail_nested_rmdir)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert failures == 1
        assert access._target_deleted is False
        assert (quarantine / ".git").is_file()
        quarantined_nested = quarantine / "nested"
        assert quarantined_nested.is_dir()
        assert not any(quarantined_nested.iterdir())
        assert opened_nested
        assert set(opened_nested) <= set(closed_descriptors)

        monkeypatch.undo()
        root._delete_target_quarantine(access)
        assert access._target_deleted is True
        assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mount identity primitive")
def test_quarantine_delete_refuses_a_mount_identity_mismatch_without_following_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, outside = _make_quarantine_fixture(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    (nested / "inside-marker").write_text("inside\n", encoding="utf-8")
    outside.mkdir()
    (outside / "outside-marker").write_text("outside\n", encoding="utf-8")
    _make_symlink(target / "linked", outside, directory=True)
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        quarantine_descriptor = access._target_quarantine.handle.capability
        original_mount_id = paths._posix_mount_id
        child_calls = 0

        def mismatch_on_descend(descriptor: int) -> int:
            nonlocal child_calls
            mount_id = original_mount_id(descriptor)
            if descriptor == quarantine_descriptor:
                return mount_id
            child_calls += 1
            return mount_id + 1

        monkeypatch.setattr(paths, "_posix_mount_id", mismatch_on_descend)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)

        assert child_calls >= 1
        assert access._target_deleted is False
        assert access.target_quarantine_path.is_dir()
        assert (outside / "outside-marker").read_text(encoding="utf-8") == "outside\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mount identity primitive")
def test_quarantine_delete_fails_closed_when_mount_identity_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    root = CanonicalRoot(root_path)

    def unavailable(_descriptor: int) -> int:
        raise RepositoryAccessDenied("injected mount identity failure")

    monkeypatch.setattr(paths, "_posix_mount_id", unavailable)
    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        assert access.target_quarantine_path.is_dir()
        assert (access.target_quarantine_path / ".git").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX bind-mount identity primitive")
def test_quarantine_delete_rejects_a_real_bind_mount_when_privileged(tmp_path: Path) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("bind-mount privilege unavailable")
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    source = tmp_path / "bind-source"
    source.mkdir()
    (source / "outside-marker").write_text("outside\n", encoding="utf-8")
    root = CanonicalRoot(root_path)
    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        mountpoint = access.target_quarantine_path / "bind"
        mountpoint.mkdir()
        mounted = False
        try:
            try:
                result = subprocess.run(
                    ["mount", "--bind", str(source), str(mountpoint)],
                    capture_output=True,
                    check=False,
                    shell=False,
                )
            except OSError:
                pytest.skip("bind-mount privilege unavailable")
            if result.returncode != 0:
                pytest.skip("bind-mount privilege unavailable")
            mounted = True
            with pytest.raises(RepositoryAccessDenied):
                root._delete_target_quarantine(access)
            assert access._target_deleted is False
            assert (source / "outside-marker").read_text(encoding="utf-8") == "outside\n"
        finally:
            if mounted:
                unmount = subprocess.run(
                    ["umount", str(mountpoint)],
                    capture_output=True,
                    check=False,
                    shell=False,
                )
                assert unmount.returncode == 0, unmount.stderr.decode(errors="replace")


@pytest.mark.skipif(os.name == "nt", reason="POSIX special-entry refusal")
def test_quarantine_delete_refuses_special_entry_before_mutation(tmp_path: Path) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    (target / "regular.txt").write_text("regular\n", encoding="utf-8")
    try:
        os.mkfifo(target / "special.fifo")
    except OSError, NotImplementedError:
        pytest.skip("the current POSIX host cannot create a FIFO")
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        quarantine = access.target_quarantine_path
        assert (quarantine / ".git").is_file()
        assert (quarantine / "regular.txt").is_file()
        assert stat.S_ISFIFO(os.stat(quarantine / "special.fifo", follow_symlinks=False).st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine bounds")
def test_quarantine_delete_enforces_depth_bound(tmp_path: Path) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    current = target
    for _ in range(paths._QUARANTINE_MAX_DEPTH + 1):
        current /= "nested"
        current.mkdir()
    root = CanonicalRoot(root_path)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        assert access.target_quarantine_path.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine bounds")
def test_quarantine_delete_enforces_entry_bound_with_a_small_injected_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    for index in range(3):
        (target / f"entry-{index}").write_text("entry\n", encoding="utf-8")
    root = CanonicalRoot(root_path)

    monkeypatch.setattr(paths, "_QUARANTINE_MAX_ENTRIES", 2)
    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert access._target_deleted is False
        quarantine = access.target_quarantine_path
        assert (quarantine / ".git").is_file()
        assert (quarantine / "entry-0").is_file()
        assert (quarantine / "entry-1").is_file()
        assert (quarantine / "entry-2").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine descriptor cleanup")
def test_quarantine_delete_closes_child_descriptors_after_injected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path, target, _registration, _outside = _make_quarantine_fixture(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child\n", encoding="utf-8")
    root = CanonicalRoot(root_path)
    opened: set[int] = set()
    closed: set[int] = set()
    original_open = paths.os.open
    original_close = paths.os.close
    original_unlink = paths.os.unlink

    def open_spy(*args: Any, **kwargs: Any) -> int:
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def close_spy(descriptor: int) -> None:
        if descriptor in opened:
            closed.add(descriptor)
        original_close(descriptor)

    def fail_child(name: str, *args: Any, **kwargs: Any) -> None:
        if name == "child.txt":
            raise OSError("injected child failure")
        original_unlink(name, *args, **kwargs)

    with root._open_worktree_quarantine(target.name, "opaque-registration") as access:
        root._quarantine_target(access)
        monkeypatch.setattr(paths.os, "open", open_spy)
        monkeypatch.setattr(paths.os, "close", close_spy)
        monkeypatch.setattr(paths.os, "unlink", fail_child)
        with pytest.raises(RepositoryAccessDenied):
            root._delete_target_quarantine(access)
        assert opened
        assert opened <= closed


@pytest.mark.skipif(os.name == "nt", reason="POSIX encoded component bound")
def test_quarantine_component_uses_encoded_byte_bound() -> None:
    accepted = "a" * paths._QUARANTINE_MAX_COMPONENT_BYTES
    rejected = "a" * (paths._QUARANTINE_MAX_COMPONENT_BYTES - 2) + "éé"
    assert len(os.fsencode(accepted)) == paths._QUARANTINE_MAX_COMPONENT_BYTES
    assert paths._validate_quarantine_component(accepted) == accepted
    assert len(os.fsencode(rejected)) > paths._QUARANTINE_MAX_COMPONENT_BYTES
    with pytest.raises(PathEscape):
        paths._validate_quarantine_component(rejected)


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
            with pytest.raises(RepositoryAccessDenied):
                root._quarantine_registration(access)
            root._delete_target_quarantine(access)
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

    with root._open_worktree_quarantine(target.name, registration.name) as access:
        monkeypatch.setattr(api, "open_directory_for_verification", fail_reopen)
        with pytest.raises(RepositoryAccessDenied):
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
        monkeypatch.undo()

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
            root._delete_target_quarantine(access)
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
