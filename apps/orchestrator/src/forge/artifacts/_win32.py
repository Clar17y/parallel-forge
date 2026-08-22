"""Handle-based Windows filesystem operations for the artifact store."""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from forge.artifacts._errors import ArtifactIntegrityError, ArtifactStoreError

if sys.platform != "win32":

    class WindowsArtifactIO:
        """Fail closed when imported on a non-Windows platform."""

        def __init__(self, root: Path) -> None:
            del root
            raise ArtifactStoreError("Windows artifact I/O is unavailable on this platform")

else:
    import ctypes
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_READONLY = 0x00000001
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_BEGIN = 0
    _FILE_DISPOSITION_INFO_CLASS = 4
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_SHARING_VIOLATION = 32
    _ERROR_FILE_EXISTS = 80
    _ERROR_ALREADY_EXISTS = 183
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _READ_CHUNK = 1024 * 1024

    class _FILETIME(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("attributes", wintypes.DWORD),
            ("creation_time", _FILETIME),
            ("last_access_time", _FILETIME),
            ("last_write_time", _FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("link_count", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = (("delete_file", ctypes.c_ubyte),)

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CREATE_FILE = _KERNEL32.CreateFileW
    _CREATE_FILE.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CREATE_FILE.restype = wintypes.HANDLE
    _CLOSE_HANDLE = _KERNEL32.CloseHandle
    _CLOSE_HANDLE.argtypes = (wintypes.HANDLE,)
    _CLOSE_HANDLE.restype = wintypes.BOOL
    _GET_FILE_INFO = _KERNEL32.GetFileInformationByHandle
    _GET_FILE_INFO.argtypes = (wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION))
    _GET_FILE_INFO.restype = wintypes.BOOL
    _READ_FILE = _KERNEL32.ReadFile
    _READ_FILE.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    _READ_FILE.restype = wintypes.BOOL
    _WRITE_FILE = _KERNEL32.WriteFile
    _WRITE_FILE.argtypes = (
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    _WRITE_FILE.restype = wintypes.BOOL
    _FLUSH_FILE = _KERNEL32.FlushFileBuffers
    _FLUSH_FILE.argtypes = (wintypes.HANDLE,)
    _FLUSH_FILE.restype = wintypes.BOOL
    _SET_POINTER = _KERNEL32.SetFilePointerEx
    _SET_POINTER.argtypes = (
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    )
    _SET_POINTER.restype = wintypes.BOOL
    _SET_FILE_INFO = _KERNEL32.SetFileInformationByHandle
    _SET_FILE_INFO.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _SET_FILE_INFO.restype = wintypes.BOOL

    def _win_error(code: int | None = None) -> OSError:
        return ctypes.WinError(code if code is not None else ctypes.get_last_error())

    def _handle_value(handle: int | ctypes.c_void_p) -> int:
        value = ctypes.cast(handle, ctypes.c_void_p).value
        if value is None:
            raise ArtifactStoreError("Windows returned an invalid null handle")
        return int(value)

    def _close(handle: int) -> None:
        if handle != _INVALID_HANDLE_VALUE:
            _CLOSE_HANDLE(wintypes.HANDLE(handle))

    def _create_file(
        path: Path,
        *,
        access: int,
        share: int,
        creation: int,
        flags: int,
        missing_ok: bool = False,
    ) -> int | None:
        ctypes.set_last_error(0)
        raw = _CREATE_FILE(str(path), access, share, None, creation, flags, None)
        handle = _handle_value(raw)
        if handle != _INVALID_HANDLE_VALUE:
            return handle
        error = ctypes.get_last_error()
        if missing_ok and error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
            return None
        raise _win_error(error)

    def _information(handle: int) -> _BY_HANDLE_FILE_INFORMATION:
        result = _BY_HANDLE_FILE_INFORMATION()
        if not _GET_FILE_INFO(wintypes.HANDLE(handle), ctypes.byref(result)):
            raise _win_error()
        return result

    def _identity(handle: int) -> tuple[int, int, int]:
        info = _information(handle)
        return (int(info.volume_serial), int(info.file_index_high), int(info.file_index_low))

    def _require_directory(handle: int) -> None:
        attributes = int(_information(handle).attributes)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT or not attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise ArtifactIntegrityError("artifact path contains a link or non-directory")

    def _require_regular(handle: int) -> None:
        attributes = int(_information(handle).attributes)
        if attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
            raise ArtifactIntegrityError("artifact path is not a regular file")

    def _seek_start(handle: int) -> None:
        if not _SET_POINTER(wintypes.HANDLE(handle), 0, None, _FILE_BEGIN):
            raise _win_error()

    def _read_all(handle: int) -> bytes:
        _seek_start(handle)
        chunks: list[bytes] = []
        while True:
            buffer = ctypes.create_string_buffer(_READ_CHUNK)
            count = wintypes.DWORD()
            if not _READ_FILE(
                wintypes.HANDLE(handle),
                buffer,
                _READ_CHUNK,
                ctypes.byref(count),
                None,
            ):
                raise _win_error()
            if count.value == 0:
                break
            chunks.append(buffer.raw[: count.value])
        return b"".join(chunks)

    def _write_all(handle: int, data: bytes) -> None:
        _seek_start(handle)
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            chunk = bytes(view[offset : offset + _READ_CHUNK])
            buffer = ctypes.create_string_buffer(chunk)
            count = wintypes.DWORD()
            if not _WRITE_FILE(
                wintypes.HANDLE(handle),
                buffer,
                len(chunk),
                ctypes.byref(count),
                None,
            ):
                raise _win_error()
            if count.value <= 0:
                raise ArtifactStoreError("artifact temporary file write made no progress")
            offset += int(count.value)
        if not _FLUSH_FILE(wintypes.HANDLE(handle)):
            raise _win_error()

    def _verify(handle: int, digest: str) -> bytes:
        data = _read_all(handle)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactIntegrityError("artifact blob failed digest verification")
        return data

    def _mark_delete(handle: int) -> None:
        disposition = _FILE_DISPOSITION_INFO(True)
        if not _SET_FILE_INFO(
            wintypes.HANDLE(handle),
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise _win_error()

    def _open_directory(path: Path, *, missing_ok: bool = False) -> int | None:
        handle = _create_file(
            path,
            access=_FILE_READ_ATTRIBUTES,
            share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            creation=_OPEN_EXISTING,
            flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            missing_ok=missing_ok,
        )
        if handle is None:
            return None
        try:
            _require_directory(handle)
        except BaseException:
            _close(handle)
            raise
        return handle

    def _open_regular_once(path: Path, *, missing_ok: bool = False) -> int | None:
        handle = _create_file(
            path,
            access=_GENERIC_READ,
            share=_FILE_SHARE_READ,
            creation=_OPEN_EXISTING,
            flags=(
                _FILE_ATTRIBUTE_NORMAL
                | _FILE_FLAG_SEQUENTIAL_SCAN
                | _FILE_FLAG_BACKUP_SEMANTICS
                | _FILE_FLAG_OPEN_REPARSE_POINT
            ),
            missing_ok=missing_ok,
        )
        if handle is None:
            return None
        try:
            _require_regular(handle)
        except BaseException:
            _close(handle)
            raise
        return handle

    def _open_regular_retry(path: Path, *, missing_ok: bool = False) -> int | None:
        deadline = time.monotonic() + 2.0
        while True:
            try:
                return _open_regular_once(path, missing_ok=missing_ok)
            except OSError as error:
                if getattr(error, "winerror", None) != _ERROR_SHARING_VIOLATION:
                    raise
                if time.monotonic() >= deadline:
                    raise ArtifactStoreError("artifact winner remained locked") from error
                time.sleep(0.01)

    def _create_temp(path: Path) -> int:
        try:
            handle = _create_file(
                path,
                access=_GENERIC_READ | _GENERIC_WRITE | _DELETE,
                share=0,
                creation=_CREATE_NEW,
                flags=_FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            )
        except OSError as error:
            if getattr(error, "winerror", None) in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
                raise ArtifactStoreError("artifact temp name collision") from error
            raise
        assert handle is not None
        try:
            _require_regular(handle)
        except BaseException:
            _close(handle)
            raise
        return handle

    @contextlib.contextmanager
    def _delete_temp_on_close(handle: int) -> Iterator[int]:
        try:
            yield handle
        finally:
            try:
                _mark_delete(handle)
            finally:
                _close(handle)

    class _WindowsLayout:
        def __init__(self, handles: list[int], root_handle: int, target: Path) -> None:
            self.handles = handles
            self.root_handle = root_handle
            self.target = target

        def close(self) -> None:
            for handle in reversed(self.handles):
                _close(handle)

    class WindowsArtifactIO:
        """Pin a no-reparse namespace while each Windows operation runs."""

        def __init__(self, root: Path) -> None:
            self._root = root.absolute()
            self._root_identity: tuple[int, int, int] | None = None
            self._identity_lock = threading.Lock()

        def put(
            self,
            digest: str,
            data: bytes,
            *,
            before_publish: Callable[[Path], None],
        ) -> Path:
            with self._layout(digest=digest, create=True) as layout:
                existing = _open_regular_retry(layout.target, missing_ok=True)
                if existing is not None:
                    try:
                        _verify(existing, digest)
                    finally:
                        _close(existing)
                    return layout.target

                temp = layout.target.with_name(f".{layout.target.name}.{os.urandom(12).hex()}.tmp")
                temp_handle = _create_temp(temp)
                linked = False
                with _delete_temp_on_close(temp_handle):
                    _write_all(temp_handle, data)
                    _verify(temp_handle, digest)
                    before_publish(layout.target)
                    try:
                        os.link(temp, layout.target)
                    except FileExistsError:
                        pass
                    except OSError as error:
                        raise ArtifactStoreError(
                            "artifact publication could not be made exclusive"
                        ) from error
                    else:
                        linked = True

                if linked:
                    winner = _open_regular_retry(layout.target)
                else:
                    winner = _open_regular_retry(layout.target, missing_ok=True)
                    if winner is None:
                        raise ArtifactIntegrityError("artifact winner disappeared")
                assert winner is not None
                try:
                    _verify(winner, digest)
                finally:
                    _close(winner)
                return layout.target

        def read(
            self,
            digest: str,
            *,
            before_open: Callable[[Path], None],
        ) -> bytes:
            with self._layout(digest=digest, create=False) as layout:
                before_open(layout.target)
                handle = _open_regular_retry(layout.target, missing_ok=True)
                if handle is None:
                    raise ArtifactIntegrityError("artifact blob is unavailable")
                try:
                    return _verify(handle, digest)
                finally:
                    _close(handle)

        @contextlib.contextmanager
        def _layout(self, *, digest: str, create: bool) -> Iterator[_WindowsLayout]:
            handles: list[int] = []
            try:
                if not self._root.anchor:
                    raise ArtifactStoreError("artifact root must be absolute")
                root_handle = self._open_or_create_root(create=create, handles=handles)

                identity = _identity(root_handle)
                with self._identity_lock:
                    if self._root_identity is None:
                        self._root_identity = identity
                    elif self._root_identity != identity:
                        raise ArtifactIntegrityError("artifact root identity changed")

                current = self._root / "sha256"
                self._open_or_create_directory(current, create=create, handles=handles)
                current /= digest[:2]
                self._open_or_create_directory(current, create=create, handles=handles)
                target = current / f"{digest[2:]}.blob"
                yield _WindowsLayout(handles, root_handle, target)
            finally:
                for handle in reversed(handles):
                    _close(handle)

        def _open_or_create_root(self, *, create: bool, handles: list[int]) -> int:
            handle = _open_directory(self._root, missing_ok=True)
            if handle is None and create:
                try:
                    self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
                except OSError as error:
                    raise ArtifactStoreError("artifact root could not be created") from error
                handle = _open_directory(self._root, missing_ok=True)
            if handle is None:
                raise ArtifactIntegrityError("artifact root is unavailable")
            handles.append(handle)
            return handle

        @staticmethod
        def _open_or_create_directory(
            path: Path,
            *,
            create: bool,
            handles: list[int],
        ) -> int:
            handle = _open_directory(path, missing_ok=True)
            if handle is None and create:
                try:
                    os.mkdir(path, 0o700)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise ArtifactStoreError("artifact directory could not be created") from error
                handle = _open_directory(path, missing_ok=True)
            if handle is None:
                raise ArtifactIntegrityError("artifact namespace is unavailable")
            handles.append(handle)
            return handle


__all__ = ["WindowsArtifactIO"]
