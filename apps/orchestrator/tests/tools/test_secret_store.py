"""Focused contract tests for the local secret store."""

from __future__ import annotations

import ctypes
import os
import re
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from forge.application.ports.worktrees import SecretStorePort
from forge.tools import secrets as secrets_module
from forge.tools.paths import _WindowsPathApi
from forge.tools.secrets import (
    LocalSecretStore,
    SecretAlreadyExistsError,
    SecretNotFoundError,
    SecretStoreError,
    SecretStoreIntegrityError,
)


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError, NotImplementedError:
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            shell=False,
        )
        if result.returncode == 0 and link.is_dir():
            return
    pytest.skip("the current host cannot create a directory link or junction")


def _make_file_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=False)
    except OSError, NotImplementedError:
        pytest.skip("the current host cannot create a file symlink")


def test_constructor_requires_existing_absolute_data_root(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError):
        LocalSecretStore(tmp_path / "missing")
    with pytest.raises(SecretStoreError):
        LocalSecretStore(Path("relative-data-root"))


@pytest.mark.parametrize(
    "secret_id",
    (
        "",
        ".",
        "..",
        "UPPER",
        "mixedCase",
        "with space",
        "with/slash",
        r"with\\slash",
        "unicode-é",
        "nul\x00byte",
        "con",
        "prn",
        "com1",
        "lpt9",
        "a" * 129,
    ),
)
def test_secret_id_is_one_lowercase_opaque_filename_component(
    tmp_path: Path, secret_id: str
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))

    with pytest.raises(SecretStoreError):
        store.create(secret_id, b"value")


def test_absent_secret_namespace_has_exact_idempotent_read_semantics(tmp_path: Path) -> None:
    store = LocalSecretStore(_data_root(tmp_path))

    assert store.exists("forge_run_absent") is False
    store.delete("forge_run_absent")
    with pytest.raises(SecretNotFoundError):
        store.read("forge_run_absent")
    assert not (store._root / "secrets").exists()  # type: ignore[attr-defined]


def test_linked_data_root_component_is_rejected_without_outside_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-data"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside")
    linked = tmp_path / "linked-data"
    _make_directory_link(linked, outside)

    with pytest.raises(SecretStoreError):
        LocalSecretStore(linked)
    assert sentinel.read_bytes() == b"outside"
    assert not (outside / "secrets").exists()


def test_linked_secret_root_is_rejected_for_every_operation(
    tmp_path: Path,
) -> None:
    root = _data_root(tmp_path)
    outside = tmp_path / "outside-secrets"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside")
    _make_directory_link(root / "secrets", outside)
    store = LocalSecretStore(root)

    for operation in (
        lambda: store.create("forge_run_linked_root", b"replacement"),
        lambda: store.read("forge_run_linked_root"),
        lambda: store.exists("forge_run_linked_root"),
        lambda: store.delete("forge_run_linked_root"),
    ):
        with pytest.raises(SecretStoreError):
            operation()
        assert sentinel.read_bytes() == b"outside"


def test_linked_secret_target_is_rejected_for_every_operation(
    tmp_path: Path,
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    store.create("forge_run_seed", b"seed")
    outside = tmp_path / "outside-target"
    outside.write_bytes(b"outside")
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    target = secrets_root / "forge_run_linked_target"
    _make_file_link(target, outside)

    for operation in (
        lambda: store.create("forge_run_linked_target", b"replacement"),
        lambda: store.read("forge_run_linked_target"),
        lambda: store.exists("forge_run_linked_target"),
        lambda: store.delete("forge_run_linked_target"),
    ):
        with pytest.raises(SecretStoreError):
            operation()
        assert outside.read_bytes() == b"outside"


def test_secret_store_round_trip_duplicate_and_absent_delete_are_exact_and_opaque(
    tmp_path: Path,
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    assert isinstance(store, SecretStorePort)

    secret_id = "forge_run_abc123"
    secret = b"Bearer highly-sensitive-value"
    store.create(secret_id, secret)

    assert store.exists(secret_id) is True
    assert store.read(secret_id) == secret

    with pytest.raises(SecretAlreadyExistsError) as duplicate:
        store.create(secret_id, b"replacement-secret")
    assert "replacement-secret" not in str(duplicate.value)
    assert secret_id not in str(duplicate.value)
    assert store.read(secret_id) == secret

    missing_id = "forge_run_missing"
    assert store.exists(missing_id) is False
    store.delete(missing_id)
    with pytest.raises(SecretNotFoundError) as missing:
        store.read(missing_id)
    assert secret_id not in str(missing.value)
    assert "highly-sensitive-value" not in repr(store)


def test_secret_bytes_are_required_and_bounded_without_disclosure(tmp_path: Path) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    with pytest.raises(SecretStoreError):
        store.create("forge_run_type", "not-bytes")  # type: ignore[arg-type]
    with pytest.raises(SecretStoreError):
        store.create("forge_run_large", b"x" * (64 * 1024 + 1))

    assert not (store._root / "secrets").exists()  # type: ignore[attr-defined]
    assert not re.search(r"x{16,}", repr(store))


def test_hard_linked_target_is_rejected_without_mutating_outside_sentinel(
    tmp_path: Path,
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    secret_id = "forge_run_linked"
    sentinel = b"outside-sentinel-bytes"
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(sentinel)
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    secrets_root.mkdir()
    target = secrets_root / secret_id
    os.link(outside, target)
    metadata = os.stat(target, follow_symlinks=False)
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink >= 2

    for operation in (
        lambda: store.create(secret_id, b"replacement"),
        lambda: store.read(secret_id),
        lambda: store.exists(secret_id),
        lambda: store.delete(secret_id),
    ):
        with pytest.raises(SecretStoreError):
            operation()
        assert outside.read_bytes() == sentinel


def test_concurrent_duplicate_creates_have_one_winner_and_no_temp_leftovers(
    tmp_path: Path,
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    secret_id = "forge_run_concurrent"
    payloads = tuple(f"winner-{index}".encode() for index in range(24))

    def create(payload: bytes) -> tuple[str, bytes, BaseException | None]:
        try:
            store.create(secret_id, payload)
        except SecretStoreError as error:
            return "error", payload, error
        return "success", payload, None

    with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
        results = tuple(executor.map(create, payloads))

    successes = [payload for status, payload, _ in results if status == "success"]
    errors = [error for status, _, error in results if status == "error"]
    assert len(successes) == 1
    assert len(errors) == len(payloads) - 1
    assert all(isinstance(error, SecretAlreadyExistsError) for error in errors)
    assert store.read(secret_id) == successes[0]
    assert not tuple((store._root / "secrets").glob(".secret-*.tmp"))  # type: ignore[attr-defined]


@pytest.mark.skipif(os.name != "posix", reason="POSIX publication race contract")
def test_posix_duplicate_create_waits_for_transient_publication_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    secret_id = "forge_run_publication_race"
    linked = threading.Event()
    observed = threading.Event()
    release = threading.Event()
    original_link = secrets_module.os.link
    original_lstat = secrets_module.os.lstat

    def delayed_link(*args: object, **kwargs: object) -> None:
        original_link(*args, **kwargs)  # type: ignore[arg-type]
        linked.set()
        assert release.wait(timeout=5)

    def delayed_lstat(*args: object, **kwargs: object) -> os.stat_result:
        metadata = original_lstat(*args, **kwargs)  # type: ignore[arg-type]
        if args and args[0] == secret_id and int(metadata.st_nlink) > 1 and not observed.is_set():
            observed.set()
            assert release.wait(timeout=5)
        return metadata

    monkeypatch.setattr(secrets_module.os, "link", delayed_link)
    monkeypatch.setattr(secrets_module.os, "lstat", delayed_lstat)

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(store.create, secret_id, b"winner")
        assert linked.wait(timeout=5)
        duplicate = executor.submit(store.create, secret_id, b"duplicate")
        assert observed.wait(timeout=5)
        release.set()
        winner.result(timeout=5)
        with pytest.raises(SecretAlreadyExistsError):
            duplicate.result(timeout=5)

    assert store.read(secret_id) == b"winner"
    assert not tuple((store._root / "secrets").glob(".secret-*.tmp"))  # type: ignore[attr-defined]


@pytest.mark.skipif(os.name != "posix", reason="POSIX hardlink race contract")
def test_posix_persistent_hardlink_inserted_after_absent_check_is_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    secret_id = "forge_run_persistent_race"
    outside = tmp_path / "outside-sentinel"
    sentinel = b"outside-sentinel"
    outside.write_bytes(sentinel)
    outside.chmod(0o600)
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    target = secrets_root / secret_id
    injected = False
    original_classify = secrets_module._classify_posix_existing_target

    def inject_hardlink(
        secrets_fd: int,
        current_id: str,
        *,
        missing_ok: bool = False,
        allow_transient_links: bool = False,
    ) -> SecretStoreError | None:
        nonlocal injected
        result = original_classify(
            secrets_fd,
            current_id,
            missing_ok=missing_ok,
            allow_transient_links=allow_transient_links,
        )
        if current_id == secret_id and result is None and not injected:
            os.link(outside, target)
            injected = True
        return result

    monkeypatch.setattr(secrets_module, "_classify_posix_existing_target", inject_hardlink)
    with pytest.raises(SecretStoreIntegrityError):
        store.create(secret_id, b"replacement")

    assert injected
    assert outside.read_bytes() == sentinel
    assert target.stat(follow_symlinks=False).st_nlink >= 2
    assert not tuple(secrets_root.glob(".secret-*.tmp"))


def test_secret_bearing_backend_os_error_is_redacted_and_cleaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    if backend is None:
        pytest.skip("Windows backend seam is unavailable on this platform")
    marker = "injected-secret-bearing-os-error"
    secret = b"secret-value-that-must-not-escape"

    def fail(_secret_id: str, _secret: bytes) -> None:
        raise OSError(f"{marker}: {_secret!r}")

    monkeypatch.setattr(backend, "create", fail)
    with pytest.raises(SecretStoreError) as failure:
        store.create("forge_run_injected", secret)

    error = failure.value
    assert marker not in str(error)
    assert marker not in repr(error)
    assert marker not in str(error.__cause__)
    assert marker not in str(error.__context__)
    assert secret.decode() not in str(error)
    assert secret.decode() not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not secrets_root.exists() or not tuple(secrets_root.glob(".secret-*.tmp"))


def test_injected_write_failure_is_redacted_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    marker = "secret-bearing-write-error"

    def fail_write(_handle: int, _secret: bytes) -> None:
        raise OSError(marker)

    if store._windows is not None:  # type: ignore[attr-defined]
        monkeypatch.setattr(store._windows._api, "write_secret", fail_write)  # type: ignore[attr-defined]
    else:
        monkeypatch.setattr(secrets_module, "_write_posix", fail_write)

    with pytest.raises(SecretStoreError) as failure:
        store.create("forge_run_write_failure", b"secret-value")

    error = failure.value
    assert marker not in str(error)
    assert marker not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not (secrets_root / "forge_run_write_failure").exists()
    assert not tuple(secrets_root.glob(".secret-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_secret_directory_and_file_have_protected_owner_only_dacls(
    tmp_path: Path,
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    secret_id = "forge_run_acl"
    store.create(secret_id, b"protected-value")
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]

    api = _WindowsPathApi()
    directory_handle = api.open_directory(secrets_root)
    file_handle = api.open_regular(secrets_root / secret_id)
    try:
        api.verify_owner_only_dacl(directory_handle)
        api.verify_owner_only_dacl(file_handle)
    finally:
        api.close(file_handle)
        api.close(directory_handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL repair contract")
def test_windows_existing_secret_directory_acl_is_repaired_before_use(
    tmp_path: Path,
) -> None:
    root = _data_root(tmp_path)
    secrets_root = root / "secrets"
    secrets_root.mkdir()
    store = LocalSecretStore(root)

    store.create("forge_run_repaired_acl", b"protected-value")

    api = _WindowsPathApi()
    directory_handle = api.open_directory(secrets_root)
    file_handle = api.open_regular(secrets_root / "forge_run_repaired_acl")
    try:
        api.verify_owner_only_dacl(directory_handle)
        api.verify_owner_only_dacl(file_handle)
    finally:
        api.close(file_handle)
        api.close(directory_handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows publication seam")
def test_windows_prepublication_failure_is_redacted_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    marker = "secret-bearing-publication-error"

    def fail() -> None:
        raise OSError(marker)

    monkeypatch.setattr(backend, "_before_publish", fail, raising=False)
    with pytest.raises(SecretStoreError) as failure:
        store.create("forge_run_publish", b"secret-value")

    error = failure.value
    assert marker not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not (secrets_root / "forge_run_publish").exists()
    assert not tuple(secrets_root.glob(".secret-*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
@pytest.mark.parametrize("operation", ("create", "read", "exists", "delete"))
def test_posix_existing_secret_with_unsafe_mode_is_rejected_without_repair(
    tmp_path: Path, operation: str
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    secret_id = "forge_run_mode"
    original = b"original-secret"
    store.create(secret_id, original)
    target = store._root / "secrets" / secret_id  # type: ignore[attr-defined]
    target.chmod(0o644)

    with pytest.raises(SecretStoreError):
        if operation == "create":
            store.create(secret_id, b"replacement")
        else:
            getattr(store, operation)(secret_id)

    assert stat.S_IMODE(target.stat(follow_symlinks=False).st_mode) == 0o644
    assert target.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow open contract")
def test_posix_existing_target_is_opened_nonblocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    secret_id = "forge_run_nonblock"
    store.create(secret_id, b"value")
    observed: list[int] = []
    original_open = os.open

    def recording_open(
        path: str | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object
    ) -> int:
        if path == secret_id:
            observed.append(flags)
        return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", recording_open)
    assert store.exists(secret_id) is True
    assert observed
    assert all(flags & os.O_NONBLOCK for flags in observed)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_posix_secret_modes_are_exact(tmp_path: Path) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    store.create("forge_run_modes", b"value")
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]

    assert stat.S_IMODE(secrets_root.stat(follow_symlinks=False).st_mode) == 0o700
    assert (
        stat.S_IMODE((secrets_root / "forge_run_modes").stat(follow_symlinks=False).st_mode)
        == 0o600
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX exact-delete contract")
def test_posix_delete_revalidates_identity_after_the_final_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    secret_id = "forge_run_delete_swap"
    original = b"original-secret"
    outside_value = b"outside-sentinel"
    store.create(secret_id, original)
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    target = secrets_root / secret_id
    moved = secrets_root / "moved-secret"
    outside = tmp_path / "outside"
    outside.write_bytes(outside_value)

    def swap() -> None:
        target.rename(moved)
        target.symlink_to(outside)

    monkeypatch.setattr(store, "_before_delete", swap, raising=False)
    with pytest.raises(SecretStoreError):
        store.delete(secret_id)

    assert moved.read_bytes() == original
    assert target.is_symlink()
    assert outside.read_bytes() == outside_value


@pytest.mark.skipif(os.name != "posix", reason="POSIX cleanup contract")
def test_posix_publication_and_one_shot_cleanup_failures_are_redacted_and_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    marker = "secret-bearing-posix-error"
    original_unlink = os.unlink
    cleanup_attempts = 0

    def fail_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError(marker)

    def flaky_unlink(path: str | bytes | os.PathLike[str], *args: object, **kwargs: object) -> None:
        nonlocal cleanup_attempts
        if str(path).startswith(".secret-"):
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise OSError(marker)
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", fail_link)
    monkeypatch.setattr(os, "unlink", flaky_unlink)
    with pytest.raises(SecretStoreError) as failure:
        store.create("forge_run_cleanup", b"secret-value")

    error = failure.value
    assert marker not in str(error)
    assert marker not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert cleanup_attempts >= 2
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not (secrets_root / "forge_run_cleanup").exists()
    assert not tuple(secrets_root.glob(".secret-*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX cleanup contract")
def test_posix_successful_publication_retries_temp_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    original_unlink = os.unlink
    cleanup_attempts = 0

    def flaky_unlink(path: str | bytes | os.PathLike[str], *args: object, **kwargs: object) -> None:
        nonlocal cleanup_attempts
        if str(path).startswith(".secret-"):
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise OSError("one-shot cleanup failure")
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", flaky_unlink)
    store.create("forge_run_cleanup_success", b"value")

    assert cleanup_attempts >= 2
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not tuple(secrets_root.glob(".secret-*.tmp"))
    assert store.read("forge_run_cleanup_success") == b"value"


@pytest.mark.skipif(os.name != "posix", reason="POSIX durability contract")
def test_posix_post_link_directory_flush_failure_is_truthful_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    store.create("forge_run_seed", b"seed")
    marker = "secret-bearing-directory-flush-error"
    original_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(marker)
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(SecretStoreError, match="publication outcome is uncertain") as failure:
        store.create("forge_run_uncertain", b"recoverable-value")

    error = failure.value
    assert marker not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not tuple(secrets_root.glob(".secret-*.tmp"))
    assert store.read("forge_run_uncertain") == b"recoverable-value"


def test_local_secret_store_repr_does_not_include_bound_path(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    store = LocalSecretStore(root)

    assert "LocalSecretStore" in repr(store)
    assert str(root) not in repr(store)


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle cleanup contract")
def test_windows_temp_cleanup_disposes_retained_handle_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]
    events: list[tuple[str, int]] = []
    created: list[int] = []
    original_create = api.create_secret_file
    original_dispose = api.dispose
    original_close = api.close

    def record_create(path: Path, name: str) -> int:
        handle = original_create(path, name)
        created.append(handle)
        return handle

    def record_dispose(handle: int) -> None:
        events.append(("dispose", handle))
        original_dispose(handle)

    def record_close(handle: int) -> None:
        events.append(("close", handle))
        original_close(handle)

    def fail_publish() -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(api, "create_secret_file", record_create)
    monkeypatch.setattr(api, "dispose", record_dispose)
    monkeypatch.setattr(api, "close", record_close)
    monkeypatch.setattr(backend, "_before_publish", fail_publish)

    with pytest.raises(SecretStoreError):
        store.create("forge_run_cleanup_handle", b"value")

    assert created
    temp_handle = created[0]
    assert ("dispose", temp_handle) in events
    assert events.index(("dispose", temp_handle)) < events.index(("close", temp_handle))
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not tuple(secrets_root.glob(".secret-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle cleanup contract")
def test_windows_temp_cleanup_retries_transient_dispose_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]
    original_dispose = api.dispose
    attempts = 0

    def flaky_dispose(handle: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("transient disposal failure")
        original_dispose(handle)

    monkeypatch.setattr(api, "dispose", flaky_dispose)
    monkeypatch.setattr(
        backend,
        "_before_publish",
        lambda: (_ for _ in ()).throw(OSError("injected publication failure")),
    )

    with pytest.raises(SecretStoreError):
        store.create("forge_run_cleanup_retry", b"value")

    assert attempts >= 3
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not tuple(secrets_root.glob(".secret-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows cleanup error contract")
def test_windows_temp_cleanup_failure_is_not_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]
    marker = "secret-bearing-cleanup-failure"

    def fail_dispose(_handle: int) -> None:
        raise OSError(marker)

    monkeypatch.setattr(api, "dispose", fail_dispose)
    monkeypatch.setattr(
        backend,
        "_before_publish",
        lambda: (_ for _ in ()).throw(OSError("injected publication failure")),
    )

    with pytest.raises(SecretStoreError) as failure:
        store.create("forge_run_cleanup_error", b"value")

    assert "cleanup" in str(failure.value)
    assert marker not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace flush contract")
def test_windows_directory_flush_failure_is_uncertain_after_valid_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]
    original_flush = api._flush_file_buffers  # type: ignore[attr-defined]
    directory_attribute = 0x00000010

    def fail_directory_flush(handle: int) -> bool:
        if int(api.information(handle).attributes) & directory_attribute:
            return False
        return bool(original_flush(handle))

    monkeypatch.setattr(api, "_flush_file_buffers", fail_directory_flush)  # type: ignore[attr-defined]

    with pytest.raises(SecretStoreError, match="publication outcome is uncertain"):
        store.create("forge_run_flush_error", b"recoverable-value")

    assert store.read("forge_run_flush_error") == b"recoverable-value"
    secrets_root = store._root / "secrets"  # type: ignore[attr-defined]
    assert not tuple(secrets_root.glob(".secret-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows directory flush contract")
@pytest.mark.parametrize("error_code", (1, 50))
def test_windows_directory_flush_only_known_unsupported_errors_are_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: int
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]

    def unsupported(_handle: int) -> bool:
        ctypes.set_last_error(error_code)
        return False

    monkeypatch.setattr(api, "_flush_file_buffers", unsupported)  # type: ignore[attr-defined]
    api.flush_secret_directory(0)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory flush contract")
def test_windows_directory_flush_surfaces_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]

    def failure(_handle: int) -> bool:
        ctypes.set_last_error(6)
        return False

    monkeypatch.setattr(api, "_flush_file_buffers", failure)  # type: ignore[attr-defined]
    with pytest.raises(OSError):
        api.flush_secret_directory(0)


@pytest.mark.skipif(os.name != "nt", reason="Windows duplicate publication contract")
def test_windows_duplicate_retries_transient_two_link_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]
    original_link = api.link_secret
    linked = threading.Event()
    release = threading.Event()

    def delayed_link(source: Path, target: Path) -> None:
        original_link(source, target)
        linked.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(api, "link_secret", delayed_link)
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(store.create, "forge_run_race", b"winner")
        assert linked.wait(timeout=5)
        duplicate = executor.submit(store.create, "forge_run_race", b"duplicate")
        release.set()
        winner.result(timeout=5)
        with pytest.raises(SecretAlreadyExistsError):
            duplicate.result(timeout=5)

    assert store.read("forge_run_race") == b"winner"


@pytest.mark.skipif(os.name != "nt", reason="Windows identity cleanup contract")
def test_windows_secrets_handle_closes_when_identity_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    (store._root / "secrets").mkdir()  # type: ignore[attr-defined]
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]
    original_identity = api.identity
    original_close = api.close
    calls = 0
    secret_handle: list[int] = []
    closed: list[int] = []
    identity_failed = False
    marker = "secret-bearing-identity-failure"

    def fail_identity(handle: int) -> tuple[int, ...]:
        nonlocal calls, identity_failed
        calls += 1
        if calls == 2:
            secret_handle.append(handle)
            identity_failed = True
            raise OSError(marker)
        return tuple(original_identity(handle))

    def record_close(handle: int) -> None:
        if identity_failed:
            closed.append(handle)
        original_close(handle)

    monkeypatch.setattr(api, "identity", fail_identity)
    monkeypatch.setattr(api, "close", record_close)

    with pytest.raises(SecretStoreError) as failure:
        store.exists("forge_run_identity_failure")

    assert marker not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert secret_handle
    assert closed.count(secret_handle[0]) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows identity cleanup contract")
def test_windows_secrets_handle_closes_when_identity_lock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    (store._root / "secrets").mkdir()  # type: ignore[attr-defined]
    backend = store._windows  # type: ignore[attr-defined]
    assert backend is not None
    api = backend._api  # type: ignore[attr-defined]
    original_close = api.close
    closed: list[int] = []

    def record_close(handle: int) -> None:
        closed.append(handle)
        original_close(handle)

    class FailingLock:
        def __enter__(self) -> None:
            raise OSError("secret-bearing-identity-lock-failure")

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(api, "close", record_close)
    monkeypatch.setattr(backend, "_identity_lock", FailingLock())  # type: ignore[attr-defined]

    with pytest.raises(SecretStoreError) as failure:
        store.exists("forge_run_identity_lock_failure")

    assert "secret-bearing" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert len(closed) >= 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX namespace durability contract")
def test_posix_temp_unlink_precedes_final_directory_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    events: list[str] = []
    original_fsync = os.fsync
    original_unlink = os.unlink

    def record_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            events.append("fsync-directory")
        original_fsync(descriptor)

    def record_unlink(
        path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
    ) -> None:
        if str(path).startswith(".secret-"):
            events.append("unlink-temp")
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "unlink", record_unlink)
    store.create("forge_run_fsync_order", b"value")

    assert events.index("unlink-temp") < len(events) - 1 - events[::-1].index("fsync-directory")


@pytest.mark.skipif(os.name != "posix", reason="POSIX namespace durability contract")
def test_posix_post_cleanup_directory_flush_failure_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalSecretStore(_data_root(tmp_path))
    original_fsync = os.fsync
    original_unlink = os.unlink
    temp_removed = False

    def record_unlink(
        path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
    ) -> None:
        nonlocal temp_removed
        if str(path).startswith(".secret-"):
            temp_removed = True
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    def fail_after_cleanup(descriptor: int) -> None:
        if temp_removed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("secret-bearing-post-cleanup-fsync")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "unlink", record_unlink)
    monkeypatch.setattr(os, "fsync", fail_after_cleanup)

    with pytest.raises(SecretStoreError, match="publication outcome is uncertain"):
        store.create("forge_run_fsync_failure", b"recoverable")

    assert store.read("forge_run_fsync_failure") == b"recoverable"
