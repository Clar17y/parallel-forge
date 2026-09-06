from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.tools.worktree_manifest import (
    DeveloperWorktreeManifest,
    WorktreeManifestError,
    WorktreeManifestStore,
)
from pydantic import ValidationError


def _manifest(root: Path, branch: str = "feature/a") -> DeveloperWorktreeManifest:
    project_id = uuid4()
    identity = WorktreeIdentity.for_developer(project_id, branch, False)
    return DeveloperWorktreeManifest(
        project_id=project_id,
        repository_path=str(root.resolve()),
        branch=branch,
        worktree_name=identity.worktree_name,
        worktree_path=str((root / ".worktrees" / identity.worktree_name).resolve()),
        base_sha="a" * 40,
        policy_version=3,
        database_state=ResourceState.DISABLED,
        completed_checkpoints=("manifest.created",),
    )


def test_filename_uses_full_branch_digest(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    first = _manifest(tmp_path, "feature/a")
    second = first.model_copy(update={"branch": "feature-a"})
    assert (
        store.path_for(first.project_id, first.branch).name
        != store.path_for(second.project_id, second.branch).name
    )
    assert (
        hashlib.sha256(first.branch.encode()).hexdigest()
        in store.path_for(first.project_id, first.branch).name
    )


def test_manifest_rejects_extra_secret_fields(tmp_path: Path) -> None:
    payload = _manifest(tmp_path).model_dump(mode="json")
    payload["database_url"] = "postgres://secret"
    with pytest.raises(ValidationError):
        DeveloperWorktreeManifest.model_validate(payload)


def test_store_round_trip_update_and_delete(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    store.create(manifest)
    assert store.load(manifest.project_id, manifest.branch) == manifest
    updated = manifest.model_copy(
        update={"completed_checkpoints": ("manifest.created", "worktree.created")}
    )
    store.save(updated)
    assert store.load(manifest.project_id, manifest.branch) == updated
    store.delete(updated)
    assert not store.exists(manifest.project_id, manifest.branch)


def test_different_existing_manifest_is_not_overwritten(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    store.create(manifest)
    forged = manifest.model_copy(update={"base_sha": "b" * 40})
    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.save(forged)
    assert store.load(manifest.project_id, manifest.branch) == manifest


def test_create_rejects_preexisting_manifest_without_changing_it(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    store.create(manifest)

    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.create(manifest.model_copy(update={"base_sha": "b" * 40}))

    assert store.load(manifest.project_id, manifest.branch) == manifest


def test_concurrent_creates_publish_exactly_one_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _manifest(tmp_path)
    second = first.model_copy(update={"base_sha": "b" * 40})
    stores = [WorktreeManifestStore(tmp_path / "data") for _ in range(2)]
    stores[0]._prepare_root(create=True)
    entered = threading.Barrier(2)
    target = stores[0].path_for(first.project_id, first.branch)
    original_lexists = os.path.lexists

    def synchronized_absence_check(path: str | os.PathLike[str]) -> bool:
        result = bool(original_lexists(path))
        if Path(path) == target:
            entered.wait(timeout=5)
        return result

    # Both creators complete the pre-publication absence check. A
    # replacement-based implementation therefore lets the second clobber the
    # first; an exclusive link lets exactly one creator win.
    monkeypatch.setattr(os.path, "lexists", synchronized_absence_check)
    outcomes: list[str] = []
    successful_manifest: list[DeveloperWorktreeManifest] = []

    def create(store: WorktreeManifestStore, value: DeveloperWorktreeManifest) -> None:
        try:
            store.create(value)
        except WorktreeManifestError:
            outcomes.append("conflict")
        else:
            outcomes.append("success")
            successful_manifest.append(value)

    threads = [
        threading.Thread(target=create, args=(stores[0], first)),
        threading.Thread(target=create, args=(stores[1], second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["conflict", "success"]
    assert stores[0].load(first.project_id, first.branch) == successful_manifest[0]


def test_failed_create_publication_cleans_its_staging_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)

    if os.name == "nt":
        assert store._windows is not None
        monkeypatch.setattr(
            store._windows,
            "link_secret",
            lambda _source, _target: (_ for _ in ()).throw(OSError("injected")),
        )
    else:
        monkeypatch.setattr(
            os,
            "link",
            lambda _source, _target: (_ for _ in ()).throw(OSError("injected")),
        )

    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.create(manifest)

    assert not store.path_for(manifest.project_id, manifest.branch).exists()
    assert list(store._root.glob(".manifest-*.tmp")) == []


def test_posix_create_reports_cleanup_failure_after_publication_without_corrupting_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _posix_store(tmp_path / "data", monkeypatch)
    manifest = _manifest(tmp_path)
    target = store.path_for(manifest.project_id, manifest.branch)
    expected = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    original_unlink = Path.unlink

    def fail_staging_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(".manifest-"):
            raise OSError("injected cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as cleanup_failure:
        cleanup_failure.setattr(Path, "unlink", fail_staging_unlink)

        with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
            store.create(manifest)

        # The caller receives only the redacted failure. Publication happened
        # before cleanup, so the final remains exact but has the staging link.
        assert target.read_text() == expected
        assert target.stat().st_nlink == 2

    # Restore unlink before cleanup so this test leaves no linked staging file.
    for staging in store._root.glob(".manifest-*.tmp"):
        staging.unlink()
    assert store.load(manifest.project_id, manifest.branch) == manifest
    assert list(store._root.glob(".manifest-*.tmp")) == []


class _WindowsManifestApi:
    """Minimal native-path boundary fake that retains actual staging files."""

    def __init__(self, *, dispose_failures: int = 0) -> None:
        self.dispose_failures = dispose_failures
        self.closed: set[int] = set()
        self.dispose_calls = 0
        self.link_saw_open_handle = False
        self.disposed_paths: list[Path] = []
        self._directories: dict[int, Path] = {}
        self._files: dict[int, tuple[int, Path]] = {}
        self._next_handle = 1

    def _handle(self) -> int:
        handle = self._next_handle
        self._next_handle += 1
        return handle

    def create_secure_directory(self, path: Path) -> None:
        path.mkdir(mode=0o700)

    def open_secret_directory(self, path: Path) -> int:
        handle = self._handle()
        self._directories[handle] = path
        return handle

    def create_secret_file(self, path: Path, _name: str) -> int:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        handle = self._handle()
        self._files[handle] = (descriptor, path)
        return handle

    def open_secret_file(
        self, _parent: int, name: str, *, access: int, missing_ok: bool
    ) -> int | None:
        path = self._directories[_parent] / name
        if not path.exists():
            if missing_ok:
                return None
            raise FileNotFoundError(path)
        descriptor = os.open(path, os.O_RDONLY)
        handle = self._handle()
        self._files[handle] = (descriptor, path)
        return handle

    def identity(self, handle: int) -> tuple[int, ...]:
        metadata = os.fstat(self._files[handle][0])
        return (metadata.st_dev, metadata.st_ino)

    def write_secret(self, handle: int, data: bytes) -> None:
        os.write(self._files[handle][0], data)
        os.fsync(self._files[handle][0])

    def read_secret(self, handle: int, maximum: int) -> bytes:
        return os.read(self._files[handle][0], maximum)

    def flush_secret_directory(self, _handle: int) -> None:
        return None

    def link_secret(self, source: Path, target: Path) -> None:
        handle = next(
            handle for handle, (_descriptor, path) in self._files.items() if path == source
        )
        self.link_saw_open_handle = handle not in self.closed
        os.link(source, target)

    def dispose_link(self, handle: int) -> None:
        self.dispose_calls += 1
        if self.dispose_calls <= self.dispose_failures:
            raise OSError("injected native disposition failure")
        # Python's os.open uses Windows sharing defaults that cannot model
        # native delete-by-handle.  Keep the API-level handle live until the
        # store closes it, while releasing the test descriptor to remove its
        # unrelated by-name deletion constraint.
        descriptor, path = self._files[handle]
        os.close(descriptor)
        self._files[handle] = (-1, path)
        path.unlink()
        self.disposed_paths.append(path)

    def dispose(self, handle: int) -> None:
        self.dispose_link(handle)

    def close(self, handle: int) -> None:
        if handle in self.closed:
            raise AssertionError("native handle closed twice")
        if handle in self._files:
            descriptor, _path = self._files.pop(handle)
            if descriptor >= 0:
                os.close(descriptor)
        elif handle in self._directories:
            self._directories.pop(handle)
        else:
            raise AssertionError("unknown native handle")
        self.closed.add(handle)


def _windows_store(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dispose_failures: int = 0,
) -> tuple[WorktreeManifestStore, _WindowsManifestApi]:
    store = WorktreeManifestStore(data_root)
    api = _WindowsManifestApi(dispose_failures=dispose_failures)
    store._windows = api
    monkeypatch.setattr(store, "_verify_windows_file", lambda _path: None)
    monkeypatch.setattr(store, "_flush_root", lambda: None)
    return store, api


def test_windows_create_retains_handle_and_retries_native_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, api = _windows_store(tmp_path / "data", monkeypatch, dispose_failures=1)
    manifest = _manifest(tmp_path)

    store.create(manifest)

    assert api.link_saw_open_handle
    assert api.dispose_calls == 2
    store._windows = None
    assert store.load(manifest.project_id, manifest.branch) == manifest
    assert list(store._root.glob(".manifest-*.tmp")) == []


def test_windows_create_fails_closed_when_native_cleanup_never_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, api = _windows_store(tmp_path / "data", monkeypatch, dispose_failures=3)
    manifest = _manifest(tmp_path)
    target = store.path_for(manifest.project_id, manifest.branch)
    expected = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )

    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.create(manifest)

    assert api.dispose_calls == 3
    assert api.closed
    assert target.read_text() == expected
    assert target.stat().st_nlink == 2
    assert list(store._root.glob(".manifest-*.tmp"))


def test_windows_failed_save_uses_identity_bound_native_stage_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, api = _windows_store(tmp_path / "data", monkeypatch)
    manifest = _manifest(tmp_path)
    store.create(manifest)
    original = store.load(manifest.project_id, manifest.branch)
    updated = manifest.model_copy(
        update={"completed_checkpoints": ("manifest.created", "worktree.created")}
    )
    original_replace = os.replace

    rename_calls: list[tuple[Path, Path]] = []
    staging_at_failure: list[Path] = []

    def fail_replace(source: Path, target: Path) -> None:
        if source.name.startswith(".manifest-"):
            rename_calls.append((source, target))
            assert source.exists()
            staging_at_failure.append(source)
            raise OSError("injected rename failure")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.save(updated)

    assert store.load(manifest.project_id, manifest.branch) == original
    assert rename_calls
    assert staging_at_failure[0] in api.disposed_paths
    assert list(store._root.glob(".manifest-*.tmp")) == []


def test_windows_failed_save_preserves_foreign_stage_when_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, api = _windows_store(tmp_path / "data", monkeypatch)
    manifest = _manifest(tmp_path)
    store.create(manifest)
    original = store.load(manifest.project_id, manifest.branch)
    updated = manifest.model_copy(
        update={"completed_checkpoints": ("manifest.created", "worktree.created")}
    )
    foreign: list[Path] = []
    original_replace = os.replace

    def replace_stage_then_fail(source: Path, _target: Path) -> None:
        if source.name.startswith(".manifest-"):
            original_identity = (source.stat().st_dev, source.stat().st_ino)
            foreign_stage = source.with_name(f"{source.name}.foreign")
            foreign_stage.write_bytes(b"foreign")
            assert (foreign_stage.stat().st_dev, foreign_stage.stat().st_ino) != original_identity
            original_replace(foreign_stage, source)
            foreign.append(source)
        raise OSError("injected rename failure")

    monkeypatch.setattr(os, "replace", replace_stage_then_fail)
    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.save(updated)

    assert store.load(manifest.project_id, manifest.branch) == original
    assert foreign and foreign[0].read_bytes() == b"foreign"
    assert foreign[0] not in api.disposed_paths


def test_manifest_target_link_is_rejected(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    target = store.path_for(manifest.project_id, manifest.branch)
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("{}")
    try:
        target.symlink_to(outside)
    except OSError, NotImplementedError:
        pytest.skip("links unavailable")
    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.load(manifest.project_id, manifest.branch)
    assert outside.read_text() == "{}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_manifest_is_owner_only_on_posix(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    store.create(manifest)
    assert store.path_for(manifest.project_id, manifest.branch).stat().st_mode & 0o777 == 0o600


def test_manifest_mutations_flush_the_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    flushes: list[str] = []
    monkeypatch.setattr(store, "_flush_root", lambda: flushes.append("flush"))

    store.create(manifest)
    updated = manifest.model_copy(
        update={"completed_checkpoints": ("manifest.created", "worktree.created")}
    )
    store.save(updated)
    store.delete(updated)

    assert flushes == ["flush", "flush", "flush"]


def test_manifest_root_link_is_rejected(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside-root"
    outside.mkdir()
    try:
        (data_root / "worktrees").symlink_to(outside, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("links unavailable")
    store = WorktreeManifestStore(data_root)

    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.create(_manifest(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL assertion")
def test_manifest_is_owner_only_on_windows(tmp_path: Path) -> None:
    from forge.tools.paths import _WindowsPathApi

    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    store.create(manifest)
    api = _WindowsPathApi()
    parent = api.open_secret_directory(store.path_for(manifest.project_id, manifest.branch).parent)
    handle = None
    try:
        handle = api.open_secret_file(
            parent,
            store.path_for(manifest.project_id, manifest.branch).name,
            access=0x80000000 | 0x00000080 | 0x00020000 | 0x00100000,
            missing_ok=False,
        )
        assert handle is not None
    finally:
        if handle is not None:
            api.close(handle)
        api.close(parent)


def _padded_data_root_for_native_limit(
    base: Path,
    manifest: DeveloperWorktreeManifest,
    target_final_len: int = 250,
) -> Path:
    probe = WorktreeManifestStore(base)
    suffix_length = len(
        str(probe.path_for(manifest.project_id, manifest.branch))
    ) - len(str(base))
    target_root_length = target_final_len - suffix_length
    if len(str(base)) + 2 > target_root_length:
        raise RuntimeError("OS temporary directory is too long for native path regression")
    return base / ("x" * (target_root_length - len(str(base)) - 1))


def test_manifest_publication_with_long_path_near_native_limit(tmp_path: Path) -> None:
    # pytest's basetemp can itself exceed the supported Windows path budget.
    # Use a private OS-temporary ancestor, then measure the actual suffix.
    with tempfile.TemporaryDirectory(prefix="forge-manifest-") as directory:
        manifest = _manifest(tmp_path)
        data_root = _padded_data_root_for_native_limit(
            Path(directory).resolve(), manifest, target_final_len=250
        )
        store = WorktreeManifestStore(data_root)
        final_path = store.path_for(manifest.project_id, manifest.branch)
        old_temp_path = store._root / f".{final_path.name}.{uuid4().hex}.tmp"

        if os.name == "nt":
            assert len(str(final_path.resolve())) <= 259
            assert len(str(old_temp_path.resolve())) >= 260

        store.create(manifest)
        assert store.load(manifest.project_id, manifest.branch) == manifest

        updated = manifest.model_copy(
            update={"completed_checkpoints": ("manifest.created", "worktree.created")}
        )
        store.save(updated)
        assert store.load(manifest.project_id, manifest.branch) == updated

        store.delete(updated)
        assert not store.exists(manifest.project_id, manifest.branch)


def _posix_store(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorktreeManifestStore:
    store = WorktreeManifestStore(data_root)
    store._windows = None
    if os.name == "nt":
        monkeypatch.setattr(store, "_flush_root", lambda: None)
    return store


def test_posix_successive_short_writes_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _posix_store(tmp_path / "data", monkeypatch)
    manifest = _manifest(tmp_path)
    original_write = os.write
    write_calls = 0

    def short_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal write_calls
        write_calls += 1
        view = memoryview(data)
        chunk = view[: min(len(view), 47)]
        return original_write(fd, chunk)

    monkeypatch.setattr(os, "write", short_write)

    store.create(manifest)
    assert store.load(manifest.project_id, manifest.branch) == manifest
    assert write_calls > 1

    create_calls = write_calls
    updated = manifest.model_copy(
        update={"completed_checkpoints": ("manifest.created", "worktree.created")}
    )
    store.save(updated)
    assert write_calls > create_calls + 1
    assert store.load(manifest.project_id, manifest.branch) == updated
    assert list(store._root.glob(".manifest-*.tmp")) == []


def test_posix_zero_progress_write_raises_and_cleans_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _posix_store(tmp_path / "data", monkeypatch)
    manifest = _manifest(tmp_path)
    target = store.path_for(manifest.project_id, manifest.branch)

    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.create(manifest)

    assert not target.exists()
    assert list(store._root.glob(".manifest-*.tmp")) == []


def test_posix_write_oserror_raises_and_cleans_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _posix_store(tmp_path / "data", monkeypatch)
    manifest = _manifest(tmp_path)
    target = store.path_for(manifest.project_id, manifest.branch)
    original_write = os.write
    calls = 0

    def fail_after_first_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("disk full")
        view = memoryview(data)
        return original_write(fd, view[: min(len(view), 47)])

    monkeypatch.setattr(os, "write", fail_after_first_write)

    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.create(manifest)

    assert not target.exists()
    assert list(store._root.glob(".manifest-*.tmp")) == []


def test_posix_failed_save_preserves_previous_valid_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _posix_store(tmp_path / "data", monkeypatch)
    manifest = _manifest(tmp_path)
    store.create(manifest)
    assert store.load(manifest.project_id, manifest.branch) == manifest

    updated = manifest.model_copy(
        update={"completed_checkpoints": ("manifest.created", "worktree.created")}
    )

    original_write = os.write

    def fail_write(fd: int, data: bytes | memoryview) -> int:
        raise OSError("injected write failure")

    monkeypatch.setattr(os, "write", fail_write)

    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.save(updated)

    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.save(updated)

    monkeypatch.setattr(os, "write", original_write)
    assert store.load(manifest.project_id, manifest.branch) == manifest
    assert list(store._root.glob(".manifest-*.tmp")) == []
