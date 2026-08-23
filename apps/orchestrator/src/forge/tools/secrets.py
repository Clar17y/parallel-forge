"""Small, exact-ID local secret storage with a no-follow filesystem boundary."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final, NoReturn

from forge.application.ports.worktrees import SecretStorePort
from forge.tools.paths import CanonicalRoot, RepositoryAccessDenied

_MAX_SECRET_BYTES: Final[int] = 64 * 1024
_MAX_SECRET_ID_LENGTH: Final[int] = 128
_SECRET_DIRECTORY_MODE: Final[int] = 0o700
_SECRET_FILE_MODE: Final[int] = 0o600
_SECRET_ID = re.compile(r"[a-z0-9_][a-z0-9_-]{0,127}\Z")
_WINDOWS_DEVICE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)

_O_DIRECTORY: Final[int] = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC: Final[int] = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK: Final[int] = getattr(os, "O_NONBLOCK", 0)
_DELETE: Final[int] = 0x00010000
_GENERIC_READ: Final[int] = 0x80000000
_GENERIC_WRITE: Final[int] = 0x40000000
_FILE_READ_ATTRIBUTES: Final[int] = 0x00000080
_READ_CONTROL: Final[int] = 0x00020000
_SYNCHRONIZE: Final[int] = 0x00100000

_ERROR = "secret store operation failed"
_CLEANUP_ERROR = "secret temporary cleanup failed"
_INVALID_ID = "secret identifier is invalid"
_INVALID_VALUE = "secret value is invalid"
_TOO_LARGE = "secret value exceeds the size limit"
_ROOT_ERROR = "secret store data root is unavailable"
_UNSAFE_PATH = "secret path is unsafe"
_UNCERTAIN_DELETE = "secret deletion outcome is uncertain"
_UNCERTAIN_PUBLICATION = "secret publication outcome is uncertain"
_TEMP_CLEANUP_ATTEMPTS: Final[int] = 3
_POSIX_DUPLICATE_ATTEMPTS: Final[int] = 32
_POSIX_DUPLICATE_DELAY: Final[float] = 0.01
_WINDOWS_DUPLICATE_ATTEMPTS: Final[int] = 32
_WINDOWS_DUPLICATE_DELAY: Final[float] = 0.01


class SecretStoreError(RuntimeError):
    """A secret-store operation could not be completed safely."""


class SecretStoreIntegrityError(SecretStoreError):
    """The exact secret namespace or target failed an integrity check."""


class SecretAlreadyExistsError(SecretStoreError):
    """The exact secret identifier is already present."""


class SecretNotFoundError(SecretStoreError):
    """The exact secret identifier is absent."""


class LocalSecretStore(SecretStorePort):
    """Store one bounded secret per protected file below one data root."""

    def __init__(self, data_root: str | os.PathLike[str]) -> None:
        try:
            canonical = CanonicalRoot(data_root)
        except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError:
            raise SecretStoreError(_ROOT_ERROR) from None
        self._root = canonical.path
        self._root_identity = canonical.identity
        self._windows = (
            _WindowsSecretBackend(self._root, self._root_identity) if os.name == "nt" else None
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def create(self, secret_id: str, secret: bytes) -> None:
        _validate_secret_id(secret_id)
        _validate_secret_bytes(secret)
        _invoke_store_operation(
            lambda: (
                self._windows.create(secret_id, secret)
                if self._windows is not None
                else _create_posix(self._root, self._root_identity, secret_id, secret)
                if os.name == "posix"
                else _raise_unsupported()
            )
        )

    def read(self, secret_id: str) -> bytes:
        _validate_secret_id(secret_id)
        return _invoke_store_operation(
            lambda: (
                self._windows.read(secret_id)
                if self._windows is not None
                else _read_posix(self._root, self._root_identity, secret_id)
                if os.name == "posix"
                else _raise_unsupported()
            )
        )

    def exists(self, secret_id: str) -> bool:
        _validate_secret_id(secret_id)
        return _invoke_store_operation(
            lambda: (
                self._windows.exists(secret_id)
                if self._windows is not None
                else _exists_posix(self._root, self._root_identity, secret_id)
                if os.name == "posix"
                else _raise_unsupported()
            )
        )

    def delete(self, secret_id: str) -> None:
        _validate_secret_id(secret_id)
        _invoke_store_operation(
            lambda: (
                self._windows.delete(secret_id)
                if self._windows is not None
                else _delete_posix(self._root, self._root_identity, secret_id, self._before_delete)
                if os.name == "posix"
                else _raise_unsupported()
            )
        )

    def _before_delete(self) -> None:
        """Provide a narrow test seam immediately before POSIX delete validation."""


def _invoke_store_operation[T](operation: Callable[[], T]) -> T:
    """Run a backend operation without retaining an unexpected exception chain."""

    failure: tuple[type[SecretStoreError], str] | None = None
    try:
        return operation()
    except SecretStoreError as error:
        if isinstance(error, SecretAlreadyExistsError):
            error_type: type[SecretStoreError] = SecretAlreadyExistsError
            message = "secret already exists"
        elif isinstance(error, SecretNotFoundError):
            error_type = SecretNotFoundError
            message = "secret was not found"
        elif isinstance(error, SecretStoreIntegrityError):
            error_type = SecretStoreIntegrityError
            message = _UNSAFE_PATH
        else:
            error_type = SecretStoreError
            message = str(error)
            if message not in {
                _ERROR,
                _CLEANUP_ERROR,
                _INVALID_ID,
                _INVALID_VALUE,
                _TOO_LARGE,
                _ROOT_ERROR,
                _UNSAFE_PATH,
                _UNCERTAIN_DELETE,
                _UNCERTAIN_PUBLICATION,
            }:
                message = _ERROR
        failure = (error_type, message)
    except Exception:  # noqa: BLE001 - backend failures are intentionally redacted
        failure = (SecretStoreError, _ERROR)
    if failure is None:
        raise AssertionError("secret store operation did not return")
    error_type, message = failure
    raise error_type(message) from None


def _raise_unsupported() -> NoReturn:
    raise SecretStoreError(_ERROR)


def _validate_secret_id(secret_id: str) -> None:
    if (
        not isinstance(secret_id, str)
        or not secret_id.isascii()
        or len(secret_id) > _MAX_SECRET_ID_LENGTH
        or _SECRET_ID.fullmatch(secret_id) is None
        or secret_id in _WINDOWS_DEVICE_NAMES
    ):
        raise SecretStoreError(_INVALID_ID)


def _validate_secret_bytes(secret: bytes) -> None:
    if type(secret) is not bytes:
        raise SecretStoreError(_INVALID_VALUE)
    if len(secret) > _MAX_SECRET_BYTES:
        raise SecretStoreError(_TOO_LARGE)


def _open_posix_absolute_directory(path: Path) -> int:
    if not path.is_absolute() or not path.anchor or not _O_DIRECTORY or not _O_NOFOLLOW:
        raise SecretStoreError(_ROOT_ERROR)
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    current: int | None = None
    try:
        current = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise SecretStoreIntegrityError(_ROOT_ERROR)
        assert current is not None
        return current
    except SecretStoreError:
        if current is not None:
            with contextlib.suppress(OSError):
                os.close(current)
        raise
    except OSError, RuntimeError, TypeError, ValueError:
        if current is not None:
            with contextlib.suppress(OSError):
                os.close(current)
        raise SecretStoreError(_ROOT_ERROR) from None


@contextlib.contextmanager
def _posix_layout(
    root: Path, root_identity: tuple[int, ...], *, create: bool
) -> Iterator[int | None]:
    root_fd = _open_posix_absolute_directory(root)
    try:
        if _posix_identity(root_fd) != root_identity:
            raise SecretStoreIntegrityError(_ROOT_ERROR)
        secrets_fd = _open_posix_secrets(root_fd, create=create)
        try:
            yield secrets_fd
        finally:
            if secrets_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(secrets_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(root_fd)


def _open_posix_secrets(root_fd: int, *, create: bool) -> int | None:
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    try:
        descriptor = os.open("secrets", flags, dir_fd=root_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir("secrets", _SECRET_DIRECTORY_MODE, dir_fd=root_fd)
        except FileExistsError:
            pass
        except OSError:
            raise SecretStoreError(_ERROR) from None
        try:
            os.fsync(root_fd)
        except OSError:
            raise SecretStoreError(_ERROR) from None
        try:
            descriptor = os.open("secrets", flags, dir_fd=root_fd)
        except OSError:
            raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
        raise SecretStoreError(_ERROR) from None
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise SecretStoreIntegrityError(_UNSAFE_PATH)
        os.fchmod(descriptor, _SECRET_DIRECTORY_MODE)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != _SECRET_DIRECTORY_MODE:
            raise SecretStoreIntegrityError(_UNSAFE_PATH)
        return descriptor
    except SecretStoreError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise SecretStoreError(_ERROR) from None


def _posix_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return (int(metadata.st_dev), int(metadata.st_ino))


def _open_posix_secret(descriptor: int, secret_id: str, *, missing_ok: bool) -> int | None:
    try:
        handle = os.open(
            secret_id,
            os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK,
            dir_fd=descriptor,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SecretNotFoundError("secret was not found") from None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
        raise SecretStoreError(_ERROR) from None
    try:
        metadata = os.fstat(handle)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or int(metadata.st_nlink) != 1
            or stat.S_IMODE(metadata.st_mode) != _SECRET_FILE_MODE
        ):
            raise SecretStoreIntegrityError(_UNSAFE_PATH)
        return handle
    except SecretStoreError:
        with contextlib.suppress(OSError):
            os.close(handle)
        raise
    except OSError:
        with contextlib.suppress(OSError):
            os.close(handle)
        raise SecretStoreError(_ERROR) from None


def _create_posix(
    root: Path, root_identity: tuple[int, ...], secret_id: str, secret: bytes
) -> None:
    temp_name: str | None = None
    temp_fd: int | None = None
    temp_identity: tuple[int, int] | None = None
    published = False
    with _posix_layout(root, root_identity, create=True) as secrets_fd:
        assert secrets_fd is not None
        existing_error = _classify_posix_existing_target(
            secrets_fd,
            secret_id,
            missing_ok=True,
            allow_transient_links=True,
        )
        if existing_error is not None:
            raise existing_error
        try:
            temp_name = f".secret-{os.urandom(16).hex()}.tmp"
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                _SECRET_FILE_MODE,
                dir_fd=secrets_fd,
            )
            os.fchmod(temp_fd, _SECRET_FILE_MODE)
            temp_identity = _posix_identity(temp_fd)
            _write_posix(temp_fd, secret)
            os.fsync(temp_fd)
            try:
                os.link(
                    temp_name,
                    secret_id,
                    src_dir_fd=secrets_fd,
                    dst_dir_fd=secrets_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing_error = _classify_posix_existing_target(
                    secrets_fd, secret_id, allow_transient_links=True
                )
                assert existing_error is not None
                raise existing_error
            published = True
        except SecretStoreError:
            raise
        except OSError:
            raise SecretStoreError(_ERROR) from None
        finally:
            if temp_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(temp_fd)
            if temp_name is not None:
                _cleanup_posix_temp(secrets_fd, temp_name)
        if not published:
            return
        publication_uncertain = False
        try:
            os.fsync(secrets_fd)
        except OSError:
            publication_uncertain = True
        final_fd = _open_posix_secret(secrets_fd, secret_id, missing_ok=False)
        assert final_fd is not None
        try:
            if temp_identity is None or _posix_identity(final_fd) != temp_identity:
                raise SecretStoreIntegrityError(_UNSAFE_PATH)
        finally:
            os.close(final_fd)
        if publication_uncertain:
            raise SecretStoreError(_UNCERTAIN_PUBLICATION)


def _cleanup_posix_temp(secrets_fd: int, temp_name: str) -> None:
    for attempt in range(_TEMP_CLEANUP_ATTEMPTS):
        try:
            os.unlink(temp_name, dir_fd=secrets_fd)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == _TEMP_CLEANUP_ATTEMPTS - 1:
                raise SecretStoreError(_CLEANUP_ERROR) from None


def _classify_posix_existing_target(
    secrets_fd: int,
    secret_id: str,
    *,
    missing_ok: bool = False,
    allow_transient_links: bool = False,
) -> SecretStoreError | None:
    for attempt in range(_POSIX_DUPLICATE_ATTEMPTS):
        try:
            metadata = os.lstat(secret_id, dir_fd=secrets_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            return SecretStoreError(_ERROR)
        except OSError:
            return SecretStoreIntegrityError(_UNSAFE_PATH)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != _SECRET_FILE_MODE
        ):
            return SecretStoreIntegrityError(_UNSAFE_PATH)
        if int(metadata.st_nlink) == 1:
            return SecretAlreadyExistsError("secret already exists")
        if not allow_transient_links:
            return SecretStoreIntegrityError(_UNSAFE_PATH)
        if attempt + 1 < _POSIX_DUPLICATE_ATTEMPTS:
            time.sleep(_POSIX_DUPLICATE_DELAY)
    return SecretStoreIntegrityError(_UNSAFE_PATH)


def _write_posix(descriptor: int, secret: bytes) -> None:
    view = memoryview(secret)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise SecretStoreError(_ERROR)
        offset += written


def _read_posix(root: Path, root_identity: tuple[int, ...], secret_id: str) -> bytes:
    with _posix_layout(root, root_identity, create=False) as secrets_fd:
        if secrets_fd is None:
            raise SecretNotFoundError("secret was not found")
        descriptor = _open_posix_secret(secrets_fd, secret_id, missing_ok=False)
        assert descriptor is not None
        try:
            chunks: list[bytes] = []
            remaining = _MAX_SECRET_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            value = b"".join(chunks)
            if len(value) > _MAX_SECRET_BYTES:
                raise SecretStoreError(_TOO_LARGE)
            return value
        finally:
            os.close(descriptor)


def _exists_posix(root: Path, root_identity: tuple[int, ...], secret_id: str) -> bool:
    with _posix_layout(root, root_identity, create=False) as secrets_fd:
        if secrets_fd is None:
            return False
        descriptor = _open_posix_secret(secrets_fd, secret_id, missing_ok=True)
        if descriptor is None:
            return False
        os.close(descriptor)
        return True


def _delete_posix(
    root: Path,
    root_identity: tuple[int, ...],
    secret_id: str,
    before_delete: Callable[[], None],
) -> None:
    with _posix_layout(root, root_identity, create=False) as secrets_fd:
        if secrets_fd is None:
            return
        descriptor = _open_posix_secret(secrets_fd, secret_id, missing_ok=True)
        if descriptor is None:
            return
        try:
            held = os.fstat(descriptor)
            before_delete()
            try:
                named = os.lstat(secret_id, dir_fd=secrets_fd)
            except OSError:
                raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
            if (
                int(named.st_dev) != int(held.st_dev)
                or int(named.st_ino) != int(held.st_ino)
                or stat.S_IFMT(named.st_mode) != stat.S_IFMT(held.st_mode)
                or int(named.st_nlink) != int(held.st_nlink)
                or stat.S_IMODE(named.st_mode) != stat.S_IMODE(held.st_mode)
            ):
                raise SecretStoreIntegrityError(_UNSAFE_PATH)
            os.unlink(secret_id, dir_fd=secrets_fd)
        except FileNotFoundError:
            raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
        except OSError:
            raise SecretStoreError(_ERROR) from None
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        try:
            os.fsync(secrets_fd)
        except OSError:
            raise SecretStoreError(_UNCERTAIN_DELETE) from None


class _WindowsSecretBackend:
    """Narrow Windows handle adapter used only by :class:`LocalSecretStore`."""

    def __init__(self, root: Path, root_identity: tuple[int, ...]) -> None:
        from forge.tools.paths import _WindowsPathApi

        self._root = root
        self._root_identity = root_identity
        self._secrets = root / "secrets"
        self._api = _WindowsPathApi()
        self._secrets_identity: tuple[int, ...] | None = None
        self._identity_lock = threading.Lock()

    def _before_publish(self) -> None:
        """Testing seam invoked after temp flush and before hard-link publication."""

    def _open_existing_secret(self, secrets_handle: int, secret_id: str) -> int | None:
        last_error: BaseException | None = None
        for attempt in range(_WINDOWS_DUPLICATE_ATTEMPTS):
            try:
                return self._api.open_secret_file(
                    secrets_handle,
                    secret_id,
                    access=_GENERIC_READ | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE,
                    missing_ok=True,
                )
            except (OSError, RepositoryAccessDenied) as error:
                last_error = error
                if attempt + 1 < _WINDOWS_DUPLICATE_ATTEMPTS:
                    time.sleep(_WINDOWS_DUPLICATE_DELAY)
        if last_error is not None:
            if isinstance(last_error, RepositoryAccessDenied):
                raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
            raise last_error
        return None

    def _cleanup_temp(self, secrets_handle: int, temp_handle: int) -> bool:
        disposed = False
        closed = False
        try:
            for attempt in range(_TEMP_CLEANUP_ATTEMPTS):
                try:
                    if not disposed:
                        self._api.dispose(temp_handle)
                        disposed = True
                    if not closed:
                        self._api.close(temp_handle)
                        closed = True
                    try:
                        self._api.flush_secret_directory(secrets_handle)
                    except OSError, RepositoryAccessDenied, SecretStoreError:
                        return True
                    return False
                except OSError, RepositoryAccessDenied, SecretStoreError:
                    if attempt + 1 == _TEMP_CLEANUP_ATTEMPTS:
                        raise SecretStoreError(_CLEANUP_ERROR) from None
                    time.sleep(_WINDOWS_DUPLICATE_DELAY)
        finally:
            if not closed:
                with contextlib.suppress(OSError, RepositoryAccessDenied, SecretStoreError):
                    self._api.close(temp_handle)
        raise SecretStoreError(_CLEANUP_ERROR) from None

    def create(self, secret_id: str, secret: bytes) -> None:
        with self._layout(create=True) as (_root_handle, secrets_handle):
            assert secrets_handle is not None
            target = self._target(secret_id)
            existing = self._open_existing_secret(secrets_handle, secret_id)
            if existing is not None:
                self._api.close(existing)
                raise SecretAlreadyExistsError("secret already exists")

            temp_name = f".secret-{os.urandom(16).hex()}.tmp"
            temp = self._secrets / temp_name
            temp_handle: int | None = None
            temp_identity: tuple[int, ...] | None = None
            published = False
            publication_uncertain = False
            operation_error: BaseException | None = None
            cleanup_flush_failed = False
            try:
                temp_handle = self._api.create_secret_file(temp, temp_name)
                temp_identity = tuple(self._api.identity(temp_handle))
                self._api.write_secret(temp_handle, secret)
                self._before_publish()
                try:
                    self._api.link_secret(temp, target)
                except FileExistsError:
                    pass
                else:
                    published = True
                    try:
                        self._api.flush_secret_directory(secrets_handle)
                    except OSError, RepositoryAccessDenied, SecretStoreError:
                        publication_uncertain = True
            except BaseException as error:  # noqa: BLE001 - cleanup must retain the exact handle
                operation_error = error
            finally:
                if temp_handle is not None:
                    cleanup_handle = temp_handle
                    temp_handle = None
                    try:
                        cleanup_flush_failed = self._cleanup_temp(secrets_handle, cleanup_handle)
                    except SecretStoreError as error:
                        operation_error = error

            if operation_error is not None:
                raise operation_error

            final = self._open_existing_secret(secrets_handle, secret_id)
            if final is None:
                raise SecretStoreError(_ERROR)
            try:
                if not published:
                    raise SecretAlreadyExistsError("secret already exists")
                if temp_identity != tuple(self._api.identity(final)):
                    raise SecretStoreIntegrityError(_UNSAFE_PATH)
            finally:
                self._api.close(final)
            if publication_uncertain or (published and cleanup_flush_failed):
                raise SecretStoreError(_UNCERTAIN_PUBLICATION)

    def read(self, secret_id: str) -> bytes:
        with self._layout(create=False) as (_root_handle, secrets_handle):
            if secrets_handle is None:
                raise SecretNotFoundError("secret was not found")
            try:
                handle = self._api.open_secret_file(
                    secrets_handle,
                    secret_id,
                    access=_GENERIC_READ | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE,
                    missing_ok=True,
                )
            except RepositoryAccessDenied:
                raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
            if handle is None:
                raise SecretNotFoundError("secret was not found")
            try:
                return self._api.read_secret(handle, _MAX_SECRET_BYTES)
            finally:
                self._api.close(handle)

    def exists(self, secret_id: str) -> bool:
        with self._layout(create=False) as (_root_handle, secrets_handle):
            if secrets_handle is None:
                return False
            try:
                handle = self._api.open_secret_file(
                    secrets_handle,
                    secret_id,
                    access=_GENERIC_READ | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE,
                    missing_ok=True,
                )
            except RepositoryAccessDenied:
                raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
            if handle is None:
                return False
            self._api.close(handle)
            return True

    def delete(self, secret_id: str) -> None:
        with self._layout(create=False) as (_root_handle, secrets_handle):
            if secrets_handle is None:
                return
            try:
                handle = self._api.open_secret_file(
                    secrets_handle,
                    secret_id,
                    access=_DELETE | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE,
                    missing_ok=True,
                )
            except RepositoryAccessDenied:
                raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
            if handle is None:
                return
            try:
                self._api.dispose(handle)
            finally:
                self._api.close(handle)
            self._api.flush_secret_directory(secrets_handle)
            try:
                remaining = self._api.open_secret_file(
                    secrets_handle,
                    secret_id,
                    access=_GENERIC_READ | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE,
                    missing_ok=True,
                )
            except RepositoryAccessDenied:
                raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
            if remaining is not None:
                self._api.close(remaining)
                raise SecretStoreError(_ERROR)

    def _target(self, secret_id: str) -> Path:
        return self._secrets / secret_id

    @contextlib.contextmanager
    def _layout(self, *, create: bool) -> Iterator[tuple[int, int | None]]:
        root_handle: int | None = None
        secrets_handle: int | None = None
        try:
            root_handle = self._api.open_directory(self._root)
            if tuple(self._api.identity(root_handle)) != self._root_identity:
                raise SecretStoreIntegrityError(_ROOT_ERROR)
            secrets_handle = self._open_secrets(create=create)
            yield root_handle, secrets_handle
        finally:
            if secrets_handle is not None:
                self._api.close(secrets_handle)
            if root_handle is not None:
                self._api.close(root_handle)

    def _open_secrets(self, *, create: bool) -> int | None:
        try:
            handle = self._api.open_secret_directory(self._secrets)
        except RepositoryAccessDenied:
            if not os.path.lexists(self._secrets):
                if not create:
                    return None
                if self._api.create_secure_directory(self._secrets):
                    handle = self._api.open_secret_directory(self._secrets)
                else:
                    try:
                        self._repair_empty_secrets_directory()
                        handle = self._api.open_secret_directory(self._secrets)
                    except RepositoryAccessDenied:
                        raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
            else:
                try:
                    self._repair_empty_secrets_directory()
                    handle = self._api.open_secret_directory(self._secrets)
                except RepositoryAccessDenied:
                    raise SecretStoreIntegrityError(_UNSAFE_PATH) from None
        try:
            identity = tuple(self._api.identity(handle))
            with self._identity_lock:
                if self._secrets_identity is None:
                    self._secrets_identity = identity
                elif self._secrets_identity != identity:
                    raise SecretStoreIntegrityError(_UNSAFE_PATH)
            return handle
        except BaseException:
            self._api.close(handle)
            raise

    def _repair_empty_secrets_directory(self) -> None:
        repair_handle = self._api.open_secret_directory_for_repair(self._secrets)
        try:
            if self._api.enumerate_names(repair_handle):
                raise RepositoryAccessDenied("secret directory is not empty")
            self._api.repair_owner_only_dacl(repair_handle)
        finally:
            self._api.close(repair_handle)


__all__ = [
    "LocalSecretStore",
    "SecretAlreadyExistsError",
    "SecretNotFoundError",
    "SecretStoreError",
    "SecretStoreIntegrityError",
]
