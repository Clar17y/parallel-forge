"""Canonical, no-follow repository path and file-access boundaries."""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path, PurePath, PureWindowsPath
from typing import BinaryIO, NamedTuple

from forge.application.ports.repository import PathEscape, RepositoryAccessDenied

_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class _WindowsIdentity(NamedTuple):
    volume_serial: int
    file_index_high: int
    file_index_low: int


class _DirectoryAccess(NamedTuple):
    path: Path
    launch_path: str
    capability: int


if os.name == "nt":
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("attributes", wintypes.DWORD),
            ("creation_time", _FileTime),
            ("last_access_time", _FileTime),
            ("last_write_time", _FileTime),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("link_count", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )


class _WindowsPathApi:
    """Small handle-only Windows API surface used while a path is inspected."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows path API is unavailable on this platform")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        self._create_file.restype = ctypes.c_void_p
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = (ctypes.c_void_p,)
        self._close_handle.restype = ctypes.c_int
        self._get_file_information = kernel32.GetFileInformationByHandle
        self._get_file_information.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_ByHandleFileInformation),
        )
        self._get_file_information.restype = ctypes.c_int

    @staticmethod
    def _value(handle: ctypes.c_void_p | int) -> int:
        value = handle if isinstance(handle, int) else handle.value
        if value is None or value == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(value)

    def open_directory(self, path: Path) -> int:
        handle = self._open(
            path,
            access=_FILE_READ_ATTRIBUTES,
            flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            info = self.information(handle)
            if (
                int(info.attributes) & _FILE_ATTRIBUTE_REPARSE_POINT
                or not int(info.attributes) & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise RepositoryAccessDenied("repository directory is not a regular directory")
            return handle
        except BaseException:
            self.close(handle)
            raise

    def open_regular(self, path: Path) -> int:
        handle = self._open(
            path,
            access=_GENERIC_READ,
            flags=_FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            info = self.information(handle)
            if int(info.attributes) & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                raise RepositoryAccessDenied("repository path is not a regular file")
            return handle
        except BaseException:
            self.close(handle)
            raise

    def _open(self, path: Path, *, access: int, flags: int) -> int:
        ctypes.set_last_error(0)
        raw = self._create_file(
            str(path),
            access,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        try:
            return self._value(raw)
        except OSError:
            raise RepositoryAccessDenied("repository path is unavailable") from None

    def information(self, handle: int) -> _ByHandleFileInformation:
        result = _ByHandleFileInformation()
        if not self._get_file_information(handle, ctypes.byref(result)):
            raise RepositoryAccessDenied("repository path information is unavailable")
        return result

    def identity(self, handle: int) -> _WindowsIdentity:
        info = self.information(handle)
        return _WindowsIdentity(
            int(info.volume_serial), int(info.file_index_high), int(info.file_index_low)
        )

    def close(self, handle: int) -> None:
        if handle != _INVALID_HANDLE_VALUE:
            self._close_handle(handle)

    def as_stream(self, handle: int) -> BinaryIO:
        import msvcrt

        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        return os.fdopen(descriptor, "rb", closefd=True)


class CanonicalRoot:
    """Pin one canonical repository root and expose no-follow file primitives."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        try:
            configured = Path(root)
        except TypeError, ValueError:
            raise RepositoryAccessDenied("repository root is unavailable") from None
        if not configured.is_absolute() or not configured.anchor:
            raise RepositoryAccessDenied("repository root must be absolute")
        _reject_link_components(configured)
        try:
            canonical = configured.resolve(strict=True)
        except OSError, RuntimeError:
            raise RepositoryAccessDenied("repository root is unavailable") from None
        if not canonical.is_dir():
            raise RepositoryAccessDenied("repository root is not a directory")
        _reject_link_components(canonical)
        self._path = canonical
        self._windows = _WindowsPathApi() if os.name == "nt" else None
        try:
            self._identity = self._identity_for_path()
        except OSError, RepositoryAccessDenied:
            raise RepositoryAccessDenied("repository root identity is unavailable") from None

    @property
    def path(self) -> Path:
        """Return the canonical absolute root path."""

        return self._path

    @property
    def identity(self) -> tuple[int, ...]:
        """Return the pinned filesystem identity for diagnostics and tests."""

        return self._identity

    def normalize(self, value: str | os.PathLike[str], *, allow_root: bool = False) -> str:
        """Validate and normalize one repository-relative path."""

        parts = _relative_parts(value, allow_root=allow_root)
        return "/".join(parts) if parts else "."

    def contains(self, value: str | os.PathLike[str], *, allow_root: bool = False) -> bool:
        """Return whether a value is a valid repository-relative path."""

        try:
            self.normalize(value, allow_root=allow_root)
        except PathEscape:
            return False
        return True

    def matches(
        self,
        candidate: str | os.PathLike[str],
        configured: str | os.PathLike[str],
    ) -> bool:
        """Match a configured path exactly or on a repository path boundary."""

        try:
            candidate_parts = _relative_parts(candidate)
            configured_parts = _relative_parts(configured)
        except PathEscape:
            return False
        if os.name == "nt":
            candidate_parts = tuple(part.casefold() for part in candidate_parts)
            configured_parts = tuple(part.casefold() for part in configured_parts)
        return candidate_parts[: len(configured_parts)] == configured_parts

    def is_exact_or_descendant(
        self,
        candidate: str | os.PathLike[str],
        configured: str | os.PathLike[str],
    ) -> bool:
        """Descriptive alias for ``matches`` used by exclusion callers."""

        return self.matches(candidate, configured)

    def stat_file(self, value: str | os.PathLike[str]) -> os.stat_result:
        """Stat one contained regular file without following links."""

        normalized = self.normalize(value)
        if os.name == "nt":
            return self._stat_windows(normalized)
        return self._stat_posix(normalized)

    @contextlib.contextmanager
    def open_read(self, value: str | os.PathLike[str]) -> Iterator[BinaryIO]:
        """Open one contained regular file while its no-follow boundary is held."""

        normalized = self.normalize(value)
        if os.name == "nt":
            with self._open_windows(normalized) as stream:
                yield stream
            return
        with self._open_posix(normalized) as stream:
            yield stream

    def read_bytes(self, value: str | os.PathLike[str]) -> bytes:
        """Read one contained regular file through the safe open primitive."""

        with self.open_read(value) as stream:
            return stream.read()

    @contextlib.contextmanager
    def open_directory(self, value: str | os.PathLike[str] = ".") -> Iterator[Path]:
        """Open one contained directory without following links or reparses."""

        normalized = self.normalize(value, allow_root=True)
        with self._open_directory(normalized) as access:
            yield access.path

    def list_directory(
        self, value: str | os.PathLike[str] = "."
    ) -> tuple[tuple[str, os.stat_result], ...]:
        """List one contained directory while its no-follow capability is held."""

        normalized = self.normalize(value, allow_root=True)
        with self._open_directory(normalized) as access:
            try:
                if os.name == "nt":
                    with os.scandir(access.path) as entries:
                        return tuple(
                            (entry.name, entry.stat(follow_symlinks=False)) for entry in entries
                        )

                try:
                    names = os.listdir(access.capability)
                except OSError, TypeError:
                    proc_fd = Path("/proc/self/fd") / str(access.capability)
                    if not proc_fd.parent.is_dir():
                        raise RepositoryAccessDenied(
                            "safe POSIX directory enumeration is unavailable"
                        )
                    names = os.listdir(proc_fd)
                return tuple(
                    (
                        name,
                        os.stat(name, dir_fd=access.capability, follow_symlinks=False),
                    )
                    for name in names
                )
            except OSError, TypeError, ValueError:
                raise RepositoryAccessDenied("repository directory enumeration failed") from None

    @contextlib.contextmanager
    def _open_directory(self, normalized: str) -> Iterator[_DirectoryAccess]:
        if os.name == "nt":
            with self._open_windows_directory(normalized) as access:
                yield access
            return
        with self._open_posix_directory(normalized) as access:
            yield access

    def _identity_for_path(self) -> tuple[int, ...]:
        if self._windows is not None:
            handle = self._windows.open_directory(self._path)
            try:
                identity = self._windows.identity(handle)
            finally:
                self._windows.close(handle)
            return identity
        metadata = os.stat(self._path, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RepositoryAccessDenied("repository root is not a directory")
        return (int(metadata.st_dev), int(metadata.st_ino))

    def _revalidate_root(self) -> None:
        _reject_link_components(self._path)
        try:
            if self._identity_for_path() != self._identity:
                raise RepositoryAccessDenied("repository root identity changed")
        except OSError, RuntimeError:
            raise RepositoryAccessDenied("repository root identity changed") from None

    def _stat_posix(self, normalized: str) -> os.stat_result:
        with self._open_posix_descriptor(normalized) as descriptor:
            return os.fstat(descriptor)

    @contextlib.contextmanager
    def _open_posix_directory(self, normalized: str) -> Iterator[_DirectoryAccess]:
        if not _O_DIRECTORY or not _O_NOFOLLOW:
            raise RepositoryAccessDenied("safe POSIX path capabilities are unavailable")
        self._revalidate_root()
        root_descriptor: int | None = None
        directory_descriptors: list[int] = []
        try:
            root_descriptor = os.open(
                self._path,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            )
            if _fd_identity(root_descriptor) != self._identity:
                raise RepositoryAccessDenied("repository root identity changed")
            current = root_descriptor
            directory_descriptors.append(root_descriptor)
            if normalized != ".":
                for part in normalized.split("/"):
                    child = os.open(
                        part,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                        dir_fd=current,
                    )
                    if not stat.S_ISDIR(os.fstat(child).st_mode):
                        os.close(child)
                        raise RepositoryAccessDenied("repository path is not a directory")
                    directory_descriptors.append(child)
                    current = child
            self._revalidate_root()
            path = self._path if normalized == "." else self._path.joinpath(*normalized.split("/"))
            proc_fd_root = Path("/proc/self/fd")
            launch_path = str(proc_fd_root / str(current)) if proc_fd_root.is_dir() else str(path)
            yield _DirectoryAccess(path, launch_path, current)
            self._revalidate_root()
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository directory is unavailable") from None
        finally:
            for descriptor in reversed(directory_descriptors):
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    @contextlib.contextmanager
    def _open_posix(self, normalized: str) -> Iterator[BinaryIO]:
        with self._open_posix_descriptor(normalized) as descriptor:
            stream = os.fdopen(os.dup(descriptor), "rb", closefd=True)
            try:
                yield stream
            finally:
                stream.close()

    @contextlib.contextmanager
    def _open_posix_descriptor(self, normalized: str) -> Iterator[int]:
        if not _O_DIRECTORY or not _O_NOFOLLOW:
            raise RepositoryAccessDenied("safe POSIX path capabilities are unavailable")
        self._revalidate_root()
        root_descriptor: int | None = None
        directory_descriptors: list[int] = []
        file_descriptor: int | None = None
        try:
            root_descriptor = os.open(
                self._path,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            )
            if _fd_identity(root_descriptor) != self._identity:
                raise RepositoryAccessDenied("repository root identity changed")
            current = root_descriptor
            directory_descriptors.append(root_descriptor)
            parts = normalized.split("/")
            for part in parts[:-1]:
                child = os.open(
                    part,
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=current,
                )
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    os.close(child)
                    raise RepositoryAccessDenied("repository path is not a directory")
                directory_descriptors.append(child)
                current = child
            file_descriptor = os.open(
                parts[-1],
                os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=current,
            )
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise RepositoryAccessDenied("repository path is not a regular file")
            self._revalidate_root()
            yield file_descriptor
            self._revalidate_root()
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository path is unavailable") from None
        finally:
            if file_descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(file_descriptor)
            for descriptor in reversed(directory_descriptors):
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def _stat_windows(self, normalized: str) -> os.stat_result:
        with self._open_windows(normalized) as stream:
            return os.fstat(stream.fileno())

    @contextlib.contextmanager
    def _open_windows_directory(self, normalized: str) -> Iterator[_DirectoryAccess]:
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        self._revalidate_root()
        handles: list[int] = []
        try:
            root_handle = api.open_directory(self._path)
            handles.append(root_handle)
            if api.identity(root_handle) != self._identity:
                raise RepositoryAccessDenied("repository root identity changed")
            current = self._path
            if normalized != ".":
                for part in normalized.split("/"):
                    current = current / part
                    handles.append(api.open_directory(current))
            self._revalidate_root()
            yield _DirectoryAccess(current, str(current), handles[-1])
            self._revalidate_root()
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository directory is unavailable") from None
        finally:
            for handle in reversed(handles):
                api.close(handle)

    @contextlib.contextmanager
    def _open_windows(self, normalized: str) -> Iterator[BinaryIO]:
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        self._revalidate_root()
        handles: list[int] = []
        stream: BinaryIO | None = None
        final_handle: int | None = None
        try:
            root_handle = api.open_directory(self._path)
            handles.append(root_handle)
            if api.identity(root_handle) != self._identity:
                raise RepositoryAccessDenied("repository root identity changed")
            current = self._path
            parts = normalized.split("/")
            for part in parts[:-1]:
                current = current / part
                child = api.open_directory(current)
                handles.append(child)
            final_handle = api.open_regular(current / parts[-1])
            stream = api.as_stream(final_handle)
            final_handle = None
            self._revalidate_root()
            yield stream
            self._revalidate_root()
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository path is unavailable") from None
        finally:
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
            if final_handle is not None:
                api.close(final_handle)
            for handle in reversed(handles):
                api.close(handle)


def _relative_parts(value: str | os.PathLike[str], *, allow_root: bool = False) -> tuple[str, ...]:
    """Return safe slash-separated components without touching the filesystem."""

    if isinstance(value, PurePath):
        if value.is_absolute():
            raise PathEscape("repository paths must be relative")
        if "\\" in str(value) and not (os.name == "nt" and isinstance(value, PureWindowsPath)):
            raise PathEscape("repository path uses a forbidden separator")
        raw_parts = tuple(str(part) for part in value.parts)
        if not raw_parts:
            if allow_root:
                return ()
            raise PathEscape("repository path must not be empty")
        raw = "/".join(raw_parts)
    else:
        try:
            raw_value = os.fspath(value)
        except TypeError, ValueError:
            raise PathEscape("repository path is invalid") from None
        if not isinstance(raw_value, str):
            raise PathEscape("repository paths must be text")
        raw = raw_value

    if "\x00" in raw or "\\" in raw:
        raise PathEscape("repository path uses a forbidden separator")
    if not raw:
        if allow_root:
            return ()
        raise PathEscape("repository path must not be empty")
    if raw == "." and allow_root:
        return ()
    if raw.startswith(("/", "//")) or _DRIVE_PREFIX.match(raw):
        raise PathEscape("repository paths must be relative")
    parts = tuple(raw.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise PathEscape("repository path contains traversal")
    if any("\x00" in part or "\\" in part for part in parts):
        raise PathEscape("repository path uses a forbidden separator")
    return parts


def _reject_link_components(path: Path) -> None:
    """Reject links/reparse points in every configured root component."""

    current = Path(path.anchor)
    if not current:
        raise RepositoryAccessDenied("repository root is unavailable")
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository root is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RepositoryAccessDenied("repository path contains a link")


def _fd_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return (int(metadata.st_dev), int(metadata.st_ino))


__all__ = ["CanonicalRoot", "PathEscape", "RepositoryAccessDenied"]
