"""Windows children owned by a kill-on-close Job before their first instruction."""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
from ctypes import wintypes as w
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Protocol

if sys.platform != "win32":
    raise ImportError("Windows process primitives are available only on Windows")

import msvcrt

K = ctypes.WinDLL("kernel32", use_last_error=True)
P = ctypes.c_void_p
H = w.HANDLE


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", P), ("inherit", w.BOOL)]


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("reserved", w.LPWSTR),
        ("desktop", w.LPWSTR),
        ("title", w.LPWSTR),
        ("x", w.DWORD),
        ("y", w.DWORD),
        ("xsize", w.DWORD),
        ("ysize", w.DWORD),
        ("xchars", w.DWORD),
        ("ychars", w.DWORD),
        ("fill", w.DWORD),
        ("flags", w.DWORD),
        ("show", w.WORD),
        ("reserved_size", w.WORD),
        ("reserved_ptr", P),
        ("stdin", H),
        ("stdout", H),
        ("stderr", H),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("info", StartupInfo), ("attributes", P)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("process", H), ("thread", H), ("pid", w.DWORD), ("tid", w.DWORD)]


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", w.DWORD),
        ("min_working", ctypes.c_size_t),
        ("max_working", ctypes.c_size_t),
        ("active_limit", w.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


def _api(name: str, args: tuple[object, ...], result: object) -> Any:
    fn = getattr(K, name)
    fn.argtypes = args
    fn.restype = result
    return fn


_close = _api("CloseHandle", (H,), w.BOOL)
_create_job = _api("CreateJobObjectW", (P, w.LPCWSTR), H)
_set_job = _api("SetInformationJobObject", (H, ctypes.c_int, P, w.DWORD), w.BOOL)
_assign = _api("AssignProcessToJobObject", (H, H), w.BOOL)
_kill_job = _api("TerminateJobObject", (H, w.UINT), w.BOOL)
_kill_process = _api("TerminateProcess", (H, w.UINT), w.BOOL)
_pipe = _api("CreatePipe", (ctypes.POINTER(H), ctypes.POINTER(H), P, w.DWORD), w.BOOL)
_handle_flags = _api("SetHandleInformation", (H, w.DWORD, w.DWORD), w.BOOL)
_init_attrs = _api(
    "InitializeProcThreadAttributeList",
    (P, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)),
    w.BOOL,
)
_update_attrs = _api(
    "UpdateProcThreadAttribute", (P, w.DWORD, ctypes.c_size_t, P, ctypes.c_size_t, P, P), w.BOOL
)
_delete_attrs = _api("DeleteProcThreadAttributeList", (P,), None)
_create = _api(
    "CreateProcessW", (w.LPCWSTR, w.LPWSTR, P, P, w.BOOL, w.DWORD, P, w.LPCWSTR, P, P), w.BOOL
)
_resume = _api("ResumeThread", (H,), w.DWORD)
_wait = _api("WaitForSingleObject", (H, w.DWORD), w.DWORD)
_exit_code = _api("GetExitCodeProcess", (H, ctypes.POINTER(w.DWORD)), w.BOOL)
_times = _api("GetProcessTimes", (H, P, P, P, P), w.BOOL)
_open = _api("OpenProcess", (w.DWORD, w.BOOL, w.DWORD), H)
_create_file = _api("CreateFileW", (w.LPCWSTR, w.DWORD, w.DWORD, P, w.DWORD, w.DWORD, H), H)
_windows_directory = _api("GetSystemWindowsDirectoryW", (w.LPWSTR, w.UINT), w.UINT)


class _PinnedFile(Protocol):
    @property
    def argument_placeholder(self) -> str: ...

    @property
    def argument_prefix(self) -> str: ...

    @property
    def path(self) -> str | os.PathLike[str]: ...

    @property
    def digest(self) -> str: ...


def _locked_verified_file(path: str, digest: str) -> BinaryIO:
    """Open bytes with write/delete sharing denied and verify that held handle."""

    handle = _create_file(path, 0x80000000, 1, None, 3, 0x80, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW")
    descriptor = -1
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None
        stream = os.fdopen(descriptor, "rb", buffering=0)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if handle:
            _close(handle)
    try:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError("pinned file is not regular")
        observed = hashlib.sha256()
        while payload := stream.read(1024 * 1024):
            observed.update(payload)
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or not hmac.compare_digest(observed.hexdigest(), digest):
            raise OSError("pinned file identity differs")
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def system_environment() -> dict[str, str]:
    """Required Windows loader/socket metadata, read from the OS rather than env."""
    buffer = ctypes.create_unicode_buffer(32768)
    count = _windows_directory(buffer, len(buffer))
    _check(count and count < len(buffer), "GetSystemWindowsDirectoryW")
    return {"SystemRoot": buffer.value}


def _check(ok: object, operation: str) -> None:
    if not ok:
        # No argv/environment or operating-system message containing paths.
        raise OSError(ctypes.get_last_error(), operation)


def _token(handle: int) -> str:
    times = [w.FILETIME() for _ in range(4)]
    _check(_times(handle, *(ctypes.byref(t) for t in times)), "GetProcessTimes")
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def identity_token(pid: int) -> str | None:
    """None means absent; permission/query failures must remain uncertain."""
    handle = _open(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise OSError(ctypes.get_last_error(), "OpenProcess")
    try:
        code = w.DWORD()
        _check(_exit_code(handle, ctypes.byref(code)), "GetExitCodeProcess")
        return _token(handle) if code.value == 259 else None
    finally:
        _close(handle)


class OwnedWindowsProcess:
    def __init__(
        self,
        pi: ProcessInfo,
        job: int,
        streams: tuple[BinaryIO, BinaryIO, BinaryIO],
        pinned_files: tuple[BinaryIO, ...] = (),
        pinned_paths: dict[str, str] | None = None,
    ):
        self.process, self.thread, self.pid = pi.process, pi.thread, pi.pid
        self.job: int | None = job
        self.stdin, self.stdout, self.stderr = streams
        self._pinned_files = pinned_files
        self.pinned_paths = MappingProxyType(dict(pinned_paths or {}))
        self._token = _token(self.process)

    def token(self) -> str:
        return self._token

    def resume(self) -> None:
        if _resume(self.thread) == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "ResumeThread")
        _close(self.thread)
        self.thread = None

    def wait(self, seconds: float) -> int:
        state = _wait(self.process, max(0, min(int(seconds * 1000), 0xFFFFFFFE)))
        if state == 258:
            raise TimeoutError("process settlement deadline")
        if state != 0:
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject")
        code = w.DWORD()
        _check(_exit_code(self.process, ctypes.byref(code)), "GetExitCodeProcess")
        return int(code.value)

    def terminate_tree(self) -> None:
        if self.job:
            _check(_kill_job(self.job, 1), "TerminateJobObject")

    def close(self) -> None:
        # Closing the job first guarantees descendants release inherited pipe ends.
        if self.job:
            _close(self.job)
            self.job = None
        for stream in (self.stdin, self.stdout, self.stderr):
            stream.close()
        for stream in self._pinned_files:
            stream.close()
        self._pinned_files = ()
        for name in ("thread", "process"):
            handle = getattr(self, name)
            if handle:
                _close(handle)
                setattr(self, name, None)


def launch_suspended(
    argv: tuple[str, ...],
    cwd: str,
    environment: dict[str, str],
    *,
    executable_digest: str | None = None,
    pinned_files: tuple[_PinnedFile, ...] = (),
) -> OwnedWindowsProcess:
    """Synchronous ownership transition; any failure also disposes a suspended child."""
    handles: list[int] = []
    streams: list[BinaryIO] = []
    locked_files: list[BinaryIO] = []
    pinned_paths: dict[str, str] = {}
    job = None
    pi = ProcessInfo()
    attributes = None
    try:
        if executable_digest is not None:
            locked_files.append(_locked_verified_file(argv[0], executable_digest))
        resolved_argv = list(argv)
        for pinned in pinned_files:
            locked_files.append(_locked_verified_file(str(pinned.path), pinned.digest))
            path = Path(pinned.path).as_posix()
            pinned_paths[pinned.argument_placeholder] = path
            index = resolved_argv.index(pinned.argument_placeholder)
            resolved_argv[index] = pinned.argument_prefix + json.dumps(path)
        argv = tuple(resolved_argv)
        job = _create_job(None, None)
        _check(job, "CreateJobObjectW")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(
            _set_job(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)), "SetInformationJobObject"
        )
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, True)
        pairs: list[tuple[int, int]] = []
        for _ in range(3):
            read, write = H(), H()
            _check(
                _pipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(sa), 0), "CreatePipe"
            )
            assert read.value is not None and write.value is not None
            handles.extend((read.value, write.value))
            pairs.append((read.value, write.value))
        child_ends = (pairs[0][0], pairs[1][1], pairs[2][1])
        parent_ends = (pairs[0][1], pairs[1][0], pairs[2][0])
        for handle in parent_ends:
            _check(_handle_flags(handle, 1, 0), "SetHandleInformation")
        size = ctypes.c_size_t()
        _init_attrs(None, 1, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        pending_attributes = ctypes.cast(buffer, P)
        _check(
            _init_attrs(pending_attributes, 1, 0, ctypes.byref(size)),
            "InitializeProcThreadAttributeList",
        )
        attributes = pending_attributes
        child_list = (H * 3)(*child_ends)
        _check(
            _update_attrs(
                attributes, 0, 0x20002, child_list, ctypes.sizeof(child_list), None, None
            ),
            "UpdateProcThreadAttribute",
        )
        si = StartupInfoEx()
        si.info.cb = ctypes.sizeof(si)
        si.info.flags = 0x100  # STARTF_USESTDHANDLES
        si.info.stdin, si.info.stdout, si.info.stderr = child_ends
        si.attributes = attributes
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        env = ctypes.create_unicode_buffer(
            "\0".join(
                f"{k}={v}" for k, v in sorted(environment.items(), key=lambda pair: pair[0].upper())
            )
            + "\0\0"
        )
        flags = 4 | 0x400 | 0x80000 | 0x08000000
        _check(
            _create(
                argv[0],
                command,
                None,
                None,
                True,
                flags,
                env,
                cwd,
                ctypes.byref(si),
                ctypes.byref(pi),
            ),
            "CreateProcessW",
        )
        for handle in child_ends:
            _close(handle)
            handles.remove(handle)
        _check(_assign(job, pi.process), "AssignProcessToJobObject")
        for index, handle in enumerate(parent_ends):
            fd = msvcrt.open_osfhandle(
                handle, os.O_BINARY | (os.O_WRONLY if index == 0 else os.O_RDONLY)
            )
            handles.remove(handle)  # ownership transferred to the descriptor
            try:
                streams.append(os.fdopen(fd, "wb" if index == 0 else "rb", buffering=0))
            except BaseException:
                os.close(fd)
                raise
        owned = OwnedWindowsProcess(
            pi,
            job,
            (streams[0], streams[1], streams[2]),
            tuple(locked_files),
            pinned_paths,
        )
        job = None
        pi = ProcessInfo()
        streams = []
        locked_files = []
        return owned
    except BaseException:
        if pi.process:
            _kill_process(pi.process, 1)  # assignment may not have succeeded
            _wait(pi.process, 2000)
        raise
    finally:
        if attributes:
            _delete_attrs(attributes)
        for handle in handles:
            _close(handle)
        for stream in streams:
            stream.close()
        for stream in locked_files:
            stream.close()
        for remaining_handle in (pi.thread, pi.process, job):
            if remaining_handle:
                _close(remaining_handle)
