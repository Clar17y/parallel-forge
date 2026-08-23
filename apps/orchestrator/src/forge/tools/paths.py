"""Canonical, no-follow repository path and file-access boundaries."""

from __future__ import annotations

import contextlib
import contextvars
import ctypes
import os
import re
import stat
import sys
from collections.abc import Iterator
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any, BinaryIO, NamedTuple

from forge.application.ports.repository import PathEscape, RepositoryAccessDenied

_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_TARGET_QUARANTINE_NAME = ".forge-quarantine"
_REGISTRATION_QUARANTINE_NAME = ".forge-worktree-quarantine"
_QUARANTINE_SEAL = object()
_QUARANTINE_MAX_DEPTH = 128
_QUARANTINE_MAX_ENTRIES = 100_000
_QUARANTINE_MAX_COMPONENT_BYTES = 255
_GITDIR_MAX_BYTES = 4096
_REMOVAL_LIVE = "live"
_REMOVAL_STALE_REGISTRATION = "stale-registration"
_REMOVAL_ABSENT = "absent"

_LINUX_AT_EMPTY_PATH = 0x1000
_LINUX_STATX_MNT_ID = 0x1000
_LINUX_STATX_BUFFER_SIZE = 256
_LINUX_STATX_MASK_OFFSET = 0
_LINUX_STATX_MNT_ID_OFFSET = 144

if sys.platform == "linux":
    try:
        _LINUX_STATX = ctypes.CDLL(None, use_errno=True).statx
        _LINUX_STATX.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        _LINUX_STATX.restype = ctypes.c_int
    except AttributeError, OSError:
        _LINUX_STATX = None
else:
    _LINUX_STATX = None


def _require_windows_native_pointer_size(pointer_size: int | None = None) -> None:
    actual_size = ctypes.sizeof(ctypes.c_void_p) if pointer_size is None else pointer_size
    if actual_size != 8:
        raise RepositoryAccessDenied("Windows native filesystem ABI is unsupported")


class _WindowsIdentity(NamedTuple):
    volume_serial: int
    file_index_high: int
    file_index_low: int


_ACCESS_SEAL = object()


class _QuarantineHandle(NamedTuple):
    capability: int
    identity: tuple[int, ...]


class _QuarantineEntry(NamedTuple):
    name: str
    path: Path
    parent: _QuarantineHandle
    handle: _QuarantineHandle


class _WindowsQuarantineNode(NamedTuple):
    identity: tuple[int, ...]
    kind: str


class _QuarantineAccess:
    """Owner-sealed capabilities for one exact, one-way quarantine operation."""

    __slots__ = (
        "_git",
        "_live",
        "_metadata_parent",
        "_mutation_bound",
        "_owner",
        "_registration",
        "_registration_deleted",
        "_registration_gitdir_content",
        "_registration_gitdir_proof",
        "_registration_gitdir_proof_retired",
        "_registration_initially_present",
        "_registration_moved",
        "_registration_path",
        "_registration_probe",
        "_registration_quarantine",
        "_registration_quarantine_parent",
        "_registration_quarantine_path",
        "_registration_root_retired",
        "_resources",
        "_root",
        "_sealed",
        "_target",
        "_target_deleted",
        "_target_initially_present",
        "_target_moved",
        "_target_path",
        "_target_probe",
        "_target_quarantine",
        "_target_quarantine_parent",
        "_target_quarantine_path",
        "_target_root_retired",
        "_worktree_parent",
    )

    def __init__(
        self,
        *,
        seal: object,
        owner: object,
        resources: list[int],
        root: _QuarantineHandle,
        git: _QuarantineHandle,
        worktree_parent: _QuarantineHandle,
        metadata_parent: _QuarantineHandle,
        target_path: Path,
        registration_path: Path | None,
        target: _QuarantineEntry | None,
        registration: _QuarantineEntry | None,
        target_initially_present: bool,
        registration_initially_present: bool,
        target_quarantine_parent: _QuarantineHandle,
        registration_quarantine_parent: _QuarantineHandle,
        target_quarantine_path: Path,
        registration_quarantine_path: Path | None,
        registration_gitdir_content: bytes | None = None,
        registration_gitdir_proof: _QuarantineHandle | None = None,
        mutation_bound: bool = True,
        target_probe: _QuarantineHandle | None = None,
        registration_probe: _QuarantineHandle | None = None,
    ) -> None:
        if seal is not _QUARANTINE_SEAL:
            raise TypeError("quarantine capability is internal")
        self._owner = owner
        self._resources = resources
        self._root = root
        self._git = git
        self._worktree_parent = worktree_parent
        self._metadata_parent = metadata_parent
        self._target_path = target_path
        self._registration_path = registration_path
        self._target = target
        self._registration = registration
        self._target_probe = target_probe
        self._registration_probe = registration_probe
        self._target_initially_present = target_initially_present
        self._registration_initially_present = registration_initially_present
        self._registration_gitdir_content = registration_gitdir_content
        self._registration_gitdir_proof = registration_gitdir_proof
        self._registration_gitdir_proof_retired = False
        self._target_quarantine_parent = target_quarantine_parent
        self._registration_quarantine_parent = registration_quarantine_parent
        self._target_quarantine_path = target_quarantine_path
        self._registration_quarantine_path = registration_quarantine_path
        self._target_quarantine: _QuarantineEntry | None = None
        self._registration_quarantine: _QuarantineEntry | None = None
        self._target_moved = False
        self._target_deleted = False
        self._target_root_retired = False
        self._registration_moved = False
        self._registration_deleted = False
        self._registration_root_retired = False
        self._mutation_bound = mutation_bound
        self._live = True
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("quarantine capability is immutable")
        object.__setattr__(self, name, value)

    @property
    def target_path(self) -> Path:
        return self._target_path

    @property
    def registration_path(self) -> Path:
        if self._registration_path is None:
            raise RepositoryAccessDenied("registration source is unavailable")
        return self._registration_path

    @property
    def target_quarantine_path(self) -> Path:
        return self._target_quarantine_path

    @property
    def registration_quarantine_path(self) -> Path:
        if self._registration_quarantine_path is None:
            raise RepositoryAccessDenied("registration quarantine is unavailable")
        return self._registration_quarantine_path

    def _retain(self, capability: int) -> None:
        self._resources.append(capability)

    def _retire(self, capability: int) -> None:
        """Forget one handle that was deliberately closed after disposition."""

        with contextlib.suppress(ValueError):
            self._resources.remove(capability)

    def _retire_target_root(self, capability: int) -> None:
        if self._target is None or capability != self._target.handle.capability:
            raise RepositoryAccessDenied("repository quarantine root capability is invalid")
        self._retire(capability)
        object.__setattr__(self, "_target_root_retired", True)

    def _retire_registration_root(self, capability: int) -> None:
        if self._registration is None or capability != self._registration.handle.capability:
            raise RepositoryAccessDenied("repository quarantine root capability is invalid")
        self._retire(capability)
        object.__setattr__(self, "_registration_root_retired", True)

    def _release(self, close: Any) -> None:
        object.__setattr__(self, "_live", False)
        for capability in reversed(self._resources):
            with contextlib.suppress(OSError, ValueError):
                close(capability)
        self._resources.clear()


class _DirectoryAccess:
    """An owner-sealed, live capability retained for one controlled operation."""

    __slots__ = (
        "_live",
        "_owner",
        "_sealed",
        "capability",
        "git_capability",
        "git_identity",
        "git_path",
        "identity",
        "normalized",
        "path",
        "root_identity",
        "root_path",
    )

    def __init__(
        self,
        *,
        seal: object,
        owner: object,
        path: Path,
        capability: int,
        root_path: Path,
        root_identity: tuple[int, ...],
        identity: tuple[int, ...],
        normalized: str,
        git_path: Path | None = None,
        git_capability: int | None = None,
        git_identity: tuple[int, ...] | None = None,
    ) -> None:
        if seal is not _ACCESS_SEAL:
            raise TypeError("directory capability is internal")
        self._owner = owner
        self.path = path
        self.capability = capability
        self.root_path = root_path
        self.root_identity = root_identity
        self.identity = identity
        self.normalized = normalized
        self.git_path = git_path
        self.git_capability = git_capability
        self.git_identity = git_identity
        self._live = True
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("directory capability is immutable")
        object.__setattr__(self, name, value)


_ACTIVE_DIRECTORY_ACCESS: contextvars.ContextVar[_DirectoryAccess | None] = contextvars.ContextVar(
    "forge_active_directory_access", default=None
)


if os.name == "nt":
    from ctypes import wintypes

    _DELETE = 0x00010000
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_LIST_DIRECTORY = 0x00000001
    _FILE_ADD_SUBDIRECTORY = 0x00000004
    _READ_CONTROL = 0x00020000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_ALREADY_EXISTS = 183
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_RENAME_INFORMATION_CLASS = 10
    _FILE_NAMES_INFORMATION_CLASS = 12
    _FILE_DISPOSITION_INFORMATION_EX_CLASS = 21
    _FILE_DISPOSITION_DELETE = 0x00000001
    _FILE_DISPOSITION_POSIX_SEMANTICS = 0x00000002
    _FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE = 0x00000010
    _FILE_DISPOSITION_FLAGS = (
        _FILE_DISPOSITION_DELETE
        | _FILE_DISPOSITION_POSIX_SEMANTICS
        | _FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE
    )
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _SYNCHRONIZE = 0x00100000
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _STATUS_SUCCESS = 0x00000000
    _STATUS_BUFFER_OVERFLOW = 0x80000005
    _STATUS_NO_MORE_FILES = 0x80000006
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SECURITY_DESCRIPTOR_REVISION = 1
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _SE_DACL_PROTECTED = 0x1000
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _INHERITED_ACE = 0x10
    _FILE_ALL_ACCESS = 0x001F01FF
    _LOCAL_SYSTEM_SID = "S-1-5-18"

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

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.DWORD),
            ("security_descriptor", ctypes.c_void_p),
            ("inherit_handle", wintypes.BOOL),
        )

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

    class _TokenUser(ctypes.Structure):
        _fields_ = (("user", _SidAndAttributes),)

    class _AclHeader(ctypes.Structure):
        _fields_ = (
            ("revision", wintypes.BYTE),
            ("sbz1", wintypes.BYTE),
            ("size", wintypes.WORD),
            ("ace_count", wintypes.WORD),
            ("sbz2", wintypes.WORD),
        )

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = (
            ("replace_if_exists", wintypes.BOOLEAN),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
        )

    class _IoStatusUnion(ctypes.Union):
        _fields_ = (("status", ctypes.c_long), ("pointer", ctypes.c_void_p))

    class _IoStatusBlock(ctypes.Structure):
        _anonymous_ = ("status_or_pointer",)
        _fields_ = (
            ("status_or_pointer", _IoStatusUnion),
            ("information", ctypes.c_size_t),
        )

    class _UnicodeString(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        )

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(_UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", ctypes.c_void_p),
            ("security_quality_of_service", ctypes.c_void_p),
        )

    class _FileDispositionInfoEx(ctypes.Structure):
        _fields_ = (("flags", wintypes.DWORD),)

    _FILE_RENAME_NAME_OFFSET = _FileRenameInfo.file_name_length.offset + ctypes.sizeof(
        wintypes.DWORD
    )


class _WindowsPathApi:
    """Small handle-only Windows API surface used while a path is inspected."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows path API is unavailable on this platform")
        _require_windows_native_pointer_size()
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
        self._read_file = kernel32.ReadFile
        self._read_file.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )
        self._read_file.restype = wintypes.BOOL
        self._set_file_pointer_ex = kernel32.SetFilePointerEx
        self._set_file_pointer_ex.argtypes = (
            ctypes.c_void_p,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.DWORD,
        )
        self._set_file_pointer_ex.restype = wintypes.BOOL
        self._get_file_information = kernel32.GetFileInformationByHandle
        self._get_file_information.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_ByHandleFileInformation),
        )
        self._get_file_information.restype = ctypes.c_int
        self._create_directory = kernel32.CreateDirectoryW
        self._create_directory.argtypes = (
            ctypes.c_wchar_p,
            ctypes.POINTER(_SecurityAttributes),
        )
        self._create_directory.restype = ctypes.c_int
        self._local_free = kernel32.LocalFree
        self._local_free.argtypes = (ctypes.c_void_p,)
        self._local_free.restype = ctypes.c_void_p
        self._get_current_process = kernel32.GetCurrentProcess
        self._get_current_process.argtypes = ()
        self._get_current_process.restype = ctypes.c_void_p
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._open_process_token = advapi32.OpenProcessToken
        self._open_process_token.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._open_process_token.restype = ctypes.c_int
        self._get_token_information = advapi32.GetTokenInformation
        self._get_token_information.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._get_token_information.restype = ctypes.c_int
        self._convert_sid_to_string = advapi32.ConvertSidToStringSidW
        self._convert_sid_to_string.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._convert_sid_to_string.restype = ctypes.c_int
        self._convert_string_security_descriptor = (
            advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        )
        self._convert_string_security_descriptor.argtypes = (
            ctypes.c_wchar_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        )
        self._convert_string_security_descriptor.restype = ctypes.c_int
        self._get_security_info = advapi32.GetSecurityInfo
        self._get_security_info.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._get_security_info.restype = wintypes.DWORD
        self._get_security_descriptor_control = advapi32.GetSecurityDescriptorControl
        self._get_security_descriptor_control.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.WORD),
        )
        self._get_security_descriptor_control.restype = ctypes.c_int
        self._get_security_descriptor_dacl = advapi32.GetSecurityDescriptorDacl
        self._get_security_descriptor_dacl.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        )
        self._get_security_descriptor_dacl.restype = ctypes.c_int
        self._get_ace = advapi32.GetAce
        self._get_ace.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._get_ace.restype = ctypes.c_int
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._nt_set_file_information = ntdll.NtSetInformationFile
        self._nt_set_file_information.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        self._nt_set_file_information.restype = ctypes.c_long
        self._nt_query_directory_file = ntdll.NtQueryDirectoryFile
        self._nt_query_directory_file.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            wintypes.BOOLEAN,
            ctypes.c_void_p,
            wintypes.BOOLEAN,
        )
        self._nt_query_directory_file.restype = ctypes.c_long
        self._nt_open_file = ntdll.NtOpenFile
        self._nt_open_file.argtypes = (
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        self._nt_open_file.restype = ctypes.c_long
        self._rtl_nt_status_to_dos_error = ntdll.RtlNtStatusToDosError
        self._rtl_nt_status_to_dos_error.argtypes = (ctypes.c_long,)
        self._rtl_nt_status_to_dos_error.restype = ctypes.c_ulong
        self._set_file_information_by_handle: Any | None = None
        try:
            self._set_file_information_by_handle = kernel32.SetFileInformationByHandle
        except AttributeError:
            self._set_file_information_by_handle = None
        else:
            self._set_file_information_by_handle.argtypes = (
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            self._set_file_information_by_handle.restype = wintypes.BOOL

    @staticmethod
    def _value(handle: ctypes.c_void_p | int) -> int:
        value = handle if isinstance(handle, int) else handle.value
        if value is None or value == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(value)

    def open_directory(self, path: Path) -> int:
        handle = self._open(
            path,
            access=_FILE_LIST_DIRECTORY,
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

    def open_directory_for_verification(self, path: Path) -> int:
        """Reopen a moved directory while a DELETE source handle remains live."""

        handle = self._open(
            path,
            access=_FILE_LIST_DIRECTORY,
            flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            share=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        )
        try:
            info = self.information(handle)
            if (
                int(info.attributes) & _FILE_ATTRIBUTE_REPARSE_POINT
                or not int(info.attributes) & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise RepositoryAccessDenied("repository quarantine destination is not a directory")
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

    def open_directory_for_rename(self, path: Path) -> int:
        handle = self._open(
            path,
            access=_DELETE | _FILE_READ_ATTRIBUTES,
            flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            info = self.information(handle)
            if (
                int(info.attributes) & _FILE_ATTRIBUTE_REPARSE_POINT
                or not int(info.attributes) & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise RepositoryAccessDenied("repository path is not a regular directory")
            return handle
        except BaseException:
            self.close(handle)
            raise

    def open_quarantine_parent(self, path: Path) -> int:
        """Open a fixed destination namespace with only child-add rights."""

        handle = self._open(
            path,
            access=(
                _FILE_LIST_DIRECTORY
                | _FILE_ADD_SUBDIRECTORY
                | _FILE_READ_ATTRIBUTES
                | _READ_CONTROL
            ),
            flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            info = self.information(handle)
            if (
                int(info.attributes) & _FILE_ATTRIBUTE_REPARSE_POINT
                or not int(info.attributes) & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise RepositoryAccessDenied("repository quarantine root is not a directory")
            self.verify_owner_only_dacl(handle)
            return handle
        except BaseException:
            self.close(handle)
            raise

    def open_mutation_lock(self, path: Path) -> int:
        """Create/open the exact repository mutation lock without sharing."""

        ctypes.set_last_error(0)
        raw = self._create_file(
            str(path),
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        try:
            handle = self._value(raw)
        except OSError:
            raise RepositoryAccessDenied("Git worktree operation is busy") from None
        try:
            info = self.information(handle)
            if int(info.attributes) & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                raise RepositoryAccessDenied("Git worktree lock is not a regular file")
            return handle
        except BaseException:
            self.close(handle)
            raise

    def create_secure_directory(self, path: Path) -> bool:
        """Create one quarantine root with an atomically applied owner-only DACL."""

        descriptor: int | None = None
        try:
            descriptor = self._create_owner_only_security_descriptor()
            attributes = _SecurityAttributes(
                ctypes.sizeof(_SecurityAttributes), ctypes.c_void_p(descriptor), False
            )
            ctypes.set_last_error(0)
            if self._create_directory(str(path), ctypes.byref(attributes)):
                return True
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if descriptor is not None:
                self._local_free(ctypes.c_void_p(descriptor))

    def verify_owner_only_dacl(self, handle: int) -> None:
        """Verify a retained directory handle has exactly the expected protected DACL."""

        _user_buffer, user_sid = self._current_user_sid()
        user_sid_text = self._sid_to_string(user_sid)
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = int(
            self._get_security_info(
                handle,
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
        )
        if result != 0:
            if descriptor.value:
                self._local_free(descriptor)
            raise RepositoryAccessDenied("repository quarantine root permissions are unsafe")
        try:
            if not owner.value or not dacl.value or not descriptor.value:
                raise RepositoryAccessDenied("repository quarantine root permissions are unsafe")
            control = wintypes.WORD()
            revision = wintypes.WORD()
            if (
                not self._get_security_descriptor_control(
                    descriptor, ctypes.byref(control), ctypes.byref(revision)
                )
                or not int(control.value) & _SE_DACL_PROTECTED
            ):
                raise RepositoryAccessDenied("repository quarantine root permissions are unsafe")
            present = wintypes.BOOL()
            defaulted = wintypes.BOOL()
            dacl_from_descriptor = ctypes.c_void_p()
            if (
                not self._get_security_descriptor_dacl(
                    descriptor,
                    ctypes.byref(present),
                    ctypes.byref(dacl_from_descriptor),
                    ctypes.byref(defaulted),
                )
                or not present.value
                or defaulted.value
                or dacl_from_descriptor.value != dacl.value
            ):
                raise RepositoryAccessDenied("repository quarantine root permissions are unsafe")
            if self._sid_to_string(int(owner.value)) != user_sid_text:
                raise RepositoryAccessDenied("repository quarantine root permissions are unsafe")
            acl = ctypes.cast(dacl, ctypes.POINTER(_AclHeader)).contents
            if acl.ace_count != 2:
                raise RepositoryAccessDenied("repository quarantine root permissions are unsafe")
            sid_texts: list[str] = []
            for index in range(int(acl.ace_count)):
                ace = ctypes.c_void_p()
                if not self._get_ace(dacl, index, ctypes.byref(ace)) or not ace.value:
                    raise RepositoryAccessDenied(
                        "repository quarantine root permissions are unsafe"
                    )
                header = ctypes.string_at(ace.value, 8)
                ace_type = header[0]
                ace_flags = header[1]
                ace_size = int.from_bytes(header[2:4], "little")
                mask = int.from_bytes(header[4:8], "little")
                if (
                    ace_type != _ACCESS_ALLOWED_ACE_TYPE
                    or ace_flags & _INHERITED_ACE
                    or ace_flags != 0
                    or ace_size < 8
                    or mask != _FILE_ALL_ACCESS
                ):
                    raise RepositoryAccessDenied(
                        "repository quarantine root permissions are unsafe"
                    )
                sid_texts.append(self._sid_to_string(ace.value + 8))
            if set(sid_texts) != {user_sid_text, _LOCAL_SYSTEM_SID}:
                raise RepositoryAccessDenied("repository quarantine root permissions are unsafe")
        finally:
            self._local_free(descriptor)

    def _current_user_sid(self) -> tuple[Any, int]:
        token = ctypes.c_void_p()
        if not self._open_process_token(
            self._get_current_process(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise RepositoryAccessDenied("repository quarantine security is unavailable")
        try:
            required = wintypes.DWORD()
            self._get_token_information(token, _TOKEN_USER, None, 0, ctypes.byref(required))
            if not required.value:
                raise RepositoryAccessDenied("repository quarantine security is unavailable")
            buffer = ctypes.create_string_buffer(required.value)
            if not self._get_token_information(
                token,
                _TOKEN_USER,
                ctypes.cast(buffer, ctypes.c_void_p),
                required,
                ctypes.byref(required),
            ):
                raise RepositoryAccessDenied("repository quarantine security is unavailable")
            sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.user.sid
            if not sid:
                raise RepositoryAccessDenied("repository quarantine security is unavailable")
            return buffer, int(sid)
        finally:
            if token.value not in (None, _INVALID_HANDLE_VALUE):
                self._close_handle(token)

    def _sid_to_string(self, sid: int) -> str:
        result = ctypes.c_void_p()
        if not self._convert_sid_to_string(ctypes.c_void_p(sid), ctypes.byref(result)):
            if result.value:
                self._local_free(result)
            raise RepositoryAccessDenied("repository quarantine security is unavailable")
        try:
            if not result.value:
                raise RepositoryAccessDenied("repository quarantine security is unavailable")
            return ctypes.wstring_at(result.value)
        finally:
            self._local_free(result)

    def _create_owner_only_security_descriptor(self) -> int:
        _user_buffer, user_sid = self._current_user_sid()
        user_sid_text = self._sid_to_string(user_sid)
        sddl = f"O:{user_sid_text}D:P(A;;FA;;;SY)(A;;FA;;;{user_sid_text})"
        descriptor = ctypes.c_void_p()
        descriptor_size = wintypes.ULONG()
        try:
            if (
                not self._convert_string_security_descriptor(
                    sddl,
                    _SECURITY_DESCRIPTOR_REVISION,
                    ctypes.byref(descriptor),
                    ctypes.byref(descriptor_size),
                )
                or not descriptor.value
            ):
                raise RepositoryAccessDenied("repository quarantine security is unavailable")
            return int(descriptor.value)
        except BaseException:
            if descriptor.value:
                self._local_free(descriptor)
            raise

    def rename_directory(self, handle: int, parent_handle: int, name: str) -> None:
        encoded = name.encode("utf-16-le")
        if len(encoded) > 255 * 2:
            raise RepositoryAccessDenied("repository quarantine name is too long")
        info_size = _FILE_RENAME_NAME_OFFSET
        info_buffer = ctypes.create_string_buffer(ctypes.sizeof(_FileRenameInfo) + len(encoded) + 2)
        info = _FileRenameInfo.from_buffer(info_buffer)
        info.replace_if_exists = 0
        info.root_directory = wintypes.HANDLE(parent_handle)
        info.file_name_length = len(encoded)
        ctypes.memmove(ctypes.addressof(info_buffer) + info_size, encoded, len(encoded))
        io_status = _IoStatusBlock()
        status = int(
            self._nt_set_file_information(
                handle,
                ctypes.byref(io_status),
                ctypes.cast(info_buffer, ctypes.c_void_p),
                len(info_buffer),
                _FILE_RENAME_INFORMATION_CLASS,
            )
        )
        if status == 0:
            return
        dos_error = int(self._rtl_nt_status_to_dos_error(status))
        raise OSError(dos_error or 1, "native quarantine rename failed")

    @staticmethod
    def _parse_file_names_information(data: bytes, information_length: int) -> tuple[str, ...]:
        """Parse one strict ``FILE_NAMES_INFORMATION`` response buffer."""

        if information_length < 0 or information_length > len(data):
            raise RepositoryAccessDenied("native directory enumeration is malformed")
        limit = information_length
        offset = 0
        names: list[str] = []
        if limit == 0:
            return ()
        while True:
            if limit - offset < 12:
                raise RepositoryAccessDenied("native directory enumeration is malformed")
            next_offset = int.from_bytes(data[offset : offset + 4], "little")
            name_length = int.from_bytes(data[offset + 8 : offset + 12], "little")
            name_start = offset + 12
            name_end = name_start + name_length
            if name_length % 2 or name_end > limit:
                raise RepositoryAccessDenied("native directory enumeration is malformed")
            try:
                name = data[name_start:name_end].decode("utf-16-le", errors="strict")
            except UnicodeDecodeError:
                raise RepositoryAccessDenied("native directory enumeration is malformed") from None
            names.append(name)
            if next_offset == 0:
                if name_end != limit:
                    raise RepositoryAccessDenied("native directory enumeration is malformed")
                return tuple(names)
            if next_offset < 12 + name_length or next_offset % 4:
                raise RepositoryAccessDenied("native directory enumeration is malformed")
            next_position = offset + next_offset
            if next_position > limit:
                raise RepositoryAccessDenied("native directory enumeration is malformed")
            if any(data[name_end:next_position]):
                raise RepositoryAccessDenied("native directory enumeration is malformed")
            offset = next_position

    def enumerate_names(self, handle: int) -> tuple[str, ...]:
        """Enumerate validated direct children using only an opened directory handle."""

        names: list[str] = []
        seen: set[str] = set()
        restart_scan = True
        buffer_size = 64 * 1024
        while True:
            buffer = ctypes.create_string_buffer(buffer_size)
            io_status = _IoStatusBlock()
            status = int(
                self._nt_query_directory_file(
                    handle,
                    None,
                    None,
                    None,
                    ctypes.byref(io_status),
                    ctypes.cast(buffer, ctypes.c_void_p),
                    buffer_size,
                    _FILE_NAMES_INFORMATION_CLASS,
                    False,
                    None,
                    restart_scan,
                )
            )
            restart_scan = False
            normalized_status = status & 0xFFFFFFFF
            if normalized_status == _STATUS_NO_MORE_FILES:
                return tuple(names)
            if normalized_status not in {_STATUS_SUCCESS, _STATUS_BUFFER_OVERFLOW}:
                self._raise_native_status(status, "native directory enumeration failed")
            information_length = int(io_status.information)
            if information_length <= 0 or information_length > buffer_size:
                raise RepositoryAccessDenied("native directory enumeration is malformed")
            for name in self._parse_file_names_information(
                ctypes.string_at(buffer, information_length), information_length
            ):
                if name in {".", ".."}:
                    continue
                try:
                    _validate_quarantine_component(name)
                except PathEscape, UnicodeError, ValueError:
                    raise RepositoryAccessDenied(
                        "native directory enumeration contains an invalid name"
                    ) from None
                if name in seen:
                    raise RepositoryAccessDenied("native directory enumeration is duplicated")
                seen.add(name)
                names.append(name)
            if normalized_status == _STATUS_SUCCESS:
                continue

    def open_child(self, parent_handle: int, name: str, *, list_handle: bool = False) -> int:
        """Open one validated child relative to a retained directory handle."""

        return self._open_child(
            parent_handle,
            name,
            access=(
                _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                if list_handle
                else _DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
            ),
            share=(
                _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
                if list_handle
                else _FILE_SHARE_READ | _FILE_SHARE_WRITE
            ),
        )

    def open_proof_child(self, parent_handle: int, name: str) -> int:
        """Open one regular proof child for read/delete sharing only."""

        return self._open_child(
            parent_handle,
            name,
            access=_FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            share=_FILE_SHARE_READ | _FILE_SHARE_DELETE,
        )

    def _open_child(self, parent_handle: int, name: str, *, access: int, share: int) -> int:
        """Open one validated child with an explicit native capability policy."""

        try:
            name = _validate_quarantine_component(name)
            encoded = name.encode("utf-16-le", errors="strict")
        except PathEscape, UnicodeError, ValueError:
            raise RepositoryAccessDenied("native child name is invalid") from None
        if len(encoded) > 0xFFFE:
            raise RepositoryAccessDenied("native child name is too long")
        name_buffer = ctypes.create_unicode_buffer(name)
        unicode_name = _UnicodeString(
            len(encoded),
            len(encoded) + ctypes.sizeof(wintypes.WCHAR),
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        object_attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            wintypes.HANDLE(parent_handle),
            ctypes.pointer(unicode_name),
            _OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        options = _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
        raw_handle = ctypes.c_void_p()
        io_status = _IoStatusBlock()
        status = int(
            self._nt_open_file(
                ctypes.byref(raw_handle),
                access,
                ctypes.byref(object_attributes),
                ctypes.byref(io_status),
                share,
                options,
            )
        )
        if status & 0xFFFFFFFF != _STATUS_SUCCESS:
            self._raise_native_status(status, "native relative child open failed")
        try:
            child = self._value(raw_handle)
            self.information(child)
            return child
        except BaseException:
            raw_value = raw_handle.value
            if raw_value is not None and raw_value != _INVALID_HANDLE_VALUE:
                self.close(int(raw_value))
            raise

    def assert_child_absent(self, parent_handle: int, name: str) -> None:
        """Prove one validated child is absent relative to a retained parent."""

        handle: int | None = None
        try:
            handle = self.open_child(parent_handle, name)
        except OSError as error:
            if error.errno in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                return
            raise RepositoryAccessDenied("native child absence is unavailable") from None
        except RepositoryAccessDenied:
            raise RepositoryAccessDenied("native child absence is unavailable") from None
        finally:
            if handle is not None:
                self.close(handle)
        raise RepositoryAccessDenied("native child is still present")

    def verify_regular_child(self, parent_handle: int, name: str) -> None:
        """Verify one normal child through a retained parent handle."""

        handle: int | None = None
        try:
            handle = self.open_child(parent_handle, name)
            info = self.information(handle)
            if int(info.attributes) & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                raise RepositoryAccessDenied("native child is not a regular file")
        except OSError:
            raise RepositoryAccessDenied("native regular child is unavailable") from None
        finally:
            if handle is not None:
                self.close(handle)

    def dispose(self, handle: int) -> None:
        """Mark one already-opened exact handle for POSIX-style deletion."""

        setter = self._set_file_information_by_handle
        if setter is None:
            raise RepositoryAccessDenied("native file disposition is unavailable")
        info = _FileDispositionInfoEx(_FILE_DISPOSITION_FLAGS)
        ctypes.set_last_error(0)
        if not setter(
            handle,
            _FILE_DISPOSITION_INFORMATION_EX_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise RepositoryAccessDenied("native file disposition failed")

    def _raise_native_status(self, status: int, message: str) -> None:
        dos_error = int(self._rtl_nt_status_to_dos_error(status))
        if dos_error:
            raise OSError(dos_error, message)
        raise RepositoryAccessDenied(message)

    def _open(
        self,
        path: Path,
        *,
        access: int,
        flags: int,
        share: int | None = None,
    ) -> int:
        if share is None:
            share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
        ctypes.set_last_error(0)
        raw = self._create_file(
            str(path),
            access,
            share,
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

    def read_bounded(self, handle: int, maximum: int = _GITDIR_MAX_BYTES) -> bytes:
        """Read one opened regular file without reopening its lexical path."""

        info = self.information(handle)
        size = (int(info.size_high) << 32) | int(info.size_low)
        if size > maximum:
            raise RepositoryAccessDenied("repository proof is oversized")
        if size == 0:
            return b""
        if not self._set_file_pointer_ex(handle, 0, None, 0):
            raise RepositoryAccessDenied("repository proof is unavailable")
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if (
            not self._read_file(
                handle,
                ctypes.cast(buffer, ctypes.c_void_p),
                size,
                ctypes.byref(read),
                None,
            )
            or int(read.value) != size
        ):
            raise RepositoryAccessDenied("repository proof is unavailable")
        after = self.information(handle)
        after_size = (int(after.size_high) << 32) | int(after.size_low)
        if after_size != size:
            raise RepositoryAccessDenied("repository proof changed")
        return bytes(buffer.raw[:size])

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
        self._access_owner = object()
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

    @contextlib.contextmanager
    def _create_directory(
        self, parent: str | os.PathLike[str], leaf: str
    ) -> Iterator[_DirectoryAccess]:
        """Create one exact directory below a pinned parent and retain its capability.

        This is intentionally an internal primitive for controlled Git creation.  It
        creates missing parent directories only below this canonical root, refuses an
        existing final leaf, and keeps the root, parent, and final directory handles
        open until the caller releases the context.
        """

        normalized_parent = self.normalize(parent, allow_root=True)
        if not leaf or any(part in {"", ".", ".."} for part in leaf.split("/")):
            raise PathEscape("repository directory leaf is invalid")
        if "\\" in leaf or "/" in leaf or "\x00" in leaf:
            raise PathEscape("repository directory leaf is invalid")
        normalized = leaf if normalized_parent == "." else f"{normalized_parent}/{leaf}"
        self.normalize(normalized)
        if os.name == "nt":
            with self._create_windows_directory(normalized_parent, leaf, normalized) as access:
                active_token = _ACTIVE_DIRECTORY_ACCESS.set(access)
                try:
                    yield access
                finally:
                    object.__setattr__(access, "_live", False)
                    _ACTIVE_DIRECTORY_ACCESS.reset(active_token)
            return
        with self._create_posix_directory(normalized_parent, leaf, normalized) as access:
            active_token = _ACTIVE_DIRECTORY_ACCESS.set(access)
            try:
                yield access
            finally:
                object.__setattr__(access, "_live", False)
                _ACTIVE_DIRECTORY_ACCESS.reset(active_token)

    @contextlib.contextmanager
    def _open_worktree_quarantine(
        self, target_leaf: str, registration_basename: str
    ) -> Iterator[_QuarantineAccess]:
        """Pin and immediately bind one target for exact quarantine moves."""

        with self._prepare_worktree_quarantine(target_leaf, registration_basename) as access:
            self._bind_worktree_quarantine(access)
            yield access

    @contextlib.contextmanager
    def _prepare_worktree_quarantine(
        self, target_leaf: str, registration_basename: str
    ) -> Iterator[_QuarantineAccess]:
        """Prepare one live target while allowing safe Git inspection."""

        target_name = _validate_quarantine_component(target_leaf)
        registration_name = _validate_quarantine_component(registration_basename)
        self._revalidate_root()
        if os.name == "nt":
            with self._open_windows_worktree_removal(
                target_name,
                registration_name,
                mode=_REMOVAL_LIVE,
                prepared=True,
            ) as access:
                try:
                    yield access
                finally:
                    access._release(self._windows.close if self._windows else os.close)
            return
        with self._open_posix_worktree_removal(
            target_name,
            registration_name,
            mode=_REMOVAL_LIVE,
            prepared=True,
        ) as access:
            try:
                yield access
            finally:
                access._release(os.close)

    def _bind_worktree_quarantine(self, access: _QuarantineAccess) -> None:
        """Promote one prepared live access to mutation authority exactly once."""

        access = self._accept_quarantine_access(access)
        if access._mutation_bound:
            raise RepositoryAccessDenied("repository quarantine is already mutation-bound")
        if (
            not access._target_initially_present
            or not access._registration_initially_present
            or access._target is None
            or access._registration is None
            or access._target_probe is None
            or access._registration_probe is None
        ):
            raise RepositoryAccessDenied("repository quarantine is not a live prepared access")
        try:
            if os.name == "nt":
                api = self._windows
                if api is None:
                    raise RepositoryAccessDenied("Windows path capabilities are unavailable")
                api.assert_child_absent(
                    access._target_quarantine_parent.capability,
                    access._target_path.name,
                )
                api.assert_child_absent(
                    access._registration_quarantine_parent.capability,
                    access.registration_path.name,
                )
                self._verify_windows_registration_state(access)
                target_handle = api.open_directory_for_rename(access.target_path)
                registration_handle: int | None = None
                try:
                    registration_handle = api.open_directory_for_rename(access.registration_path)
                    target_identity = tuple(api.identity(target_handle))
                    registration_identity = tuple(api.identity(registration_handle))
                    if target_identity != access._target_probe.identity:
                        raise RepositoryAccessDenied("repository target identity changed")
                    if registration_identity != access._registration_probe.identity:
                        raise RepositoryAccessDenied("repository registration identity changed")
                    object.__setattr__(
                        access,
                        "_target",
                        _QuarantineEntry(
                            access._target.name,
                            access._target.path,
                            access._target.parent,
                            _QuarantineHandle(target_handle, target_identity),
                        ),
                    )
                    object.__setattr__(
                        access,
                        "_registration",
                        _QuarantineEntry(
                            access._registration.name,
                            access._registration.path,
                            access._registration.parent,
                            _QuarantineHandle(registration_handle, registration_identity),
                        ),
                    )
                    self._verify_windows_registration_state(access)
                    access._retain(target_handle)
                    access._retain(registration_handle)
                    object.__setattr__(access, "_mutation_bound", True)
                    return
                except BaseException:
                    api.close(target_handle)
                    if registration_handle is not None:
                        api.close(registration_handle)
                    raise

            self._reject_posix_live_collisions(access)
            self._verify_posix_registration_state(access)
            if access._target.handle.identity != access._target_probe.identity:
                raise RepositoryAccessDenied("repository target identity changed")
            if access._registration.handle.identity != access._registration_probe.identity:
                raise RepositoryAccessDenied("repository registration identity changed")
            self._assert_posix_entry_present(access._target)
            self._assert_posix_entry_present(access._registration)
            self._verify_posix_registration_state(access)
            object.__setattr__(access, "_mutation_bound", True)
        except RepositoryAccessDenied:
            raise
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository quarantine binding failed") from None

    def _accept_mutation_access(self, access: _QuarantineAccess) -> _QuarantineAccess:
        access = self._accept_quarantine_access(access)
        if not access._mutation_bound:
            raise RepositoryAccessDenied("repository quarantine is not mutation-bound")
        return access

    @contextlib.contextmanager
    def _open_stale_registration_quarantine(
        self, target_leaf: str, registration_basename: str
    ) -> Iterator[_QuarantineAccess]:
        """Pin one exact stale registration while its target remains absent."""

        target_name = _validate_quarantine_component(target_leaf)
        registration_name = _validate_quarantine_component(registration_basename)
        self._revalidate_root()
        if os.name == "nt":
            with self._open_windows_worktree_removal(
                target_name, registration_name, mode=_REMOVAL_STALE_REGISTRATION
            ) as access:
                try:
                    yield access
                    self._verify_worktree_removal_state(access)
                finally:
                    access._release(self._windows.close if self._windows else os.close)
            return
        with self._open_posix_worktree_removal(
            target_name, registration_name, mode=_REMOVAL_STALE_REGISTRATION
        ) as access:
            try:
                yield access
                self._verify_worktree_removal_state(access)
            finally:
                access._release(os.close)

    @contextlib.contextmanager
    def _inspect_absent_worktree_removal(self, target_leaf: str) -> Iterator[_QuarantineAccess]:
        """Inspect one exact absent target under the retained mutation lock."""

        target_name = _validate_quarantine_component(target_leaf)
        self._revalidate_root()
        if os.name == "nt":
            with self._open_windows_worktree_removal(
                target_name, None, mode=_REMOVAL_ABSENT
            ) as access:
                try:
                    yield access
                    self._verify_worktree_removal_state(access)
                finally:
                    access._release(self._windows.close if self._windows else os.close)
            return
        with self._open_posix_worktree_removal(target_name, None, mode=_REMOVAL_ABSENT) as access:
            try:
                yield access
                self._verify_worktree_removal_state(access)
            finally:
                access._release(os.close)

    def _verify_worktree_removal_state(self, access: _QuarantineAccess) -> _QuarantineAccess:
        """Revalidate one sealed removal state and its authoritative absences."""

        access = self._accept_quarantine_access(access)
        if access._target_initially_present and not access._target_deleted:
            return access
        if os.name == "nt":
            api = self._windows
            if api is None:
                raise RepositoryAccessDenied("Windows path capabilities are unavailable")
            api.assert_child_absent(access._worktree_parent.capability, access._target_path.name)
            api.assert_child_absent(
                access._target_quarantine_parent.capability, access._target_path.name
            )
        else:
            self._assert_posix_name_absent(
                access._worktree_parent.capability, access._target_path.name
            )
            self._assert_posix_name_absent(
                access._target_quarantine_parent.capability, access._target_path.name
            )
        return access

    def _verify_windows_registration_state(self, access: _QuarantineAccess) -> None:
        """Revalidate one pinned Windows registration and its exact marker content."""

        if (
            access._registration is None
            or access._registration_gitdir_proof is None
            or access._registration_gitdir_content is None
        ):
            raise RepositoryAccessDenied("registration source is unavailable")
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        registration_handle = access._registration.handle.capability
        api.assert_child_absent(registration_handle, "locked")
        proof_handle = access._registration_gitdir_proof.capability
        reopened: int | None = None
        try:
            reopened = api.open_proof_child(registration_handle, "gitdir")
            reopened_info = api.information(reopened)
            if int(reopened_info.attributes) & (
                _FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise RepositoryAccessDenied("repository proof is not a regular file")
            if tuple(api.identity(reopened)) != access._registration_gitdir_proof.identity:
                raise RepositoryAccessDenied("repository proof identity changed")
            proof_info = api.information(proof_handle)
            if int(proof_info.attributes) & (
                _FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise RepositoryAccessDenied("repository proof is not a regular file")
            content = api.read_bounded(proof_handle)
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository proof is unavailable") from None
        finally:
            if reopened is not None:
                api.close(reopened)
        if content != access._registration_gitdir_content:
            raise RepositoryAccessDenied("repository proof changed")
        _validate_gitdir_content(
            content,
            access.registration_path,
            access._target_path / ".git",
        )

    def _retire_windows_registration_gitdir_proof(self, access: _QuarantineAccess) -> None:
        """Close the leaf proof only after the final handle-relative recheck."""

        proof = access._registration_gitdir_proof
        if proof is None or access._registration_gitdir_proof_retired:
            raise RepositoryAccessDenied("repository proof capability is unavailable")
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        try:
            api.close(proof.capability)
        except OSError:
            raise RepositoryAccessDenied("repository proof capability cleanup failed") from None
        access._retire(proof.capability)
        object.__setattr__(access, "_registration_gitdir_proof", None)
        object.__setattr__(access, "_registration_gitdir_proof_retired", True)

    def _reacquire_windows_registration_gitdir_proof(
        self,
        access: _QuarantineAccess,
        expected_identity: tuple[int, ...],
    ) -> None:
        """Restore a retired proof after a registration move failed before completion."""

        if (
            access._registration is None
            or access._registration_gitdir_content is None
            or access._registration_gitdir_proof is not None
            or not access._registration_gitdir_proof_retired
        ):
            raise RepositoryAccessDenied("repository proof recovery state is invalid")
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        proof_handle: int | None = None
        try:
            proof_handle = api.open_proof_child(
                access._registration.handle.capability,
                "gitdir",
            )
            proof_info = api.information(proof_handle)
            if int(proof_info.attributes) & (
                _FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise RepositoryAccessDenied("repository proof is not a regular file")
            proof_identity = tuple(api.identity(proof_handle))
            if proof_identity != expected_identity:
                raise RepositoryAccessDenied("repository proof identity changed")
            content = api.read_bounded(proof_handle)
            if content != access._registration_gitdir_content:
                raise RepositoryAccessDenied("repository proof changed")
            _validate_gitdir_content(
                content,
                access.registration_path,
                access._target_path / ".git",
            )
        except OSError, RepositoryAccessDenied, ValueError:
            if proof_handle is not None:
                api.close(proof_handle)
            raise RepositoryAccessDenied("repository proof recovery failed") from None
        access._retain(proof_handle)
        object.__setattr__(
            access,
            "_registration_gitdir_proof",
            _QuarantineHandle(proof_handle, proof_identity),
        )
        object.__setattr__(access, "_registration_gitdir_proof_retired", False)

    def _reject_posix_live_collisions(self, access: _QuarantineAccess) -> None:
        self._assert_posix_name_absent(
            access._target_quarantine_parent.capability,
            access._target_path.name,
        )
        self._assert_posix_name_absent(
            access._registration_quarantine_parent.capability,
            access.registration_path.name,
        )

    @staticmethod
    def _verify_posix_registration_state(access: _QuarantineAccess) -> None:
        if (
            access._registration is None
            or access._registration_gitdir_proof is None
            or access._registration_gitdir_content is None
        ):
            raise RepositoryAccessDenied("registration source is unavailable")
        _reject_posix_registration_lock(access._registration.handle.capability)
        reopened: int | None = None
        try:
            reopened = _open_posix_gitdir(access._registration.handle.capability)
            if _fd_identity(reopened) != access._registration_gitdir_proof.identity:
                raise RepositoryAccessDenied("repository proof identity changed")
            content = _read_posix_bounded(access._registration_gitdir_proof.capability)
        finally:
            if reopened is not None:
                with contextlib.suppress(OSError):
                    os.close(reopened)
        if content != access._registration_gitdir_content:
            raise RepositoryAccessDenied("repository proof changed")
        _validate_gitdir_content(
            content,
            access.registration_path,
            access._target_path / ".git",
        )

    def _accept_quarantine_access(self, access: _QuarantineAccess) -> _QuarantineAccess:
        """Accept only this root's live, owner-sealed quarantine capability."""

        if (
            not isinstance(access, _QuarantineAccess)
            or not access._live
            or access._owner is not self._access_owner
        ):
            raise RepositoryAccessDenied("repository quarantine capability is not trusted")
        try:
            self._revalidate_root()
            for label, pinned in (
                ("repository root", access._root),
                ("Git metadata", access._git),
                ("worktree parent", access._worktree_parent),
                ("metadata parent", access._metadata_parent),
                ("target quarantine parent", access._target_quarantine_parent),
                (
                    "registration quarantine parent",
                    access._registration_quarantine_parent,
                ),
            ):
                self._verify_quarantine_handle(label, pinned)
            if os.name == "nt":
                self._verify_windows_quarantine_parent(
                    access._target_quarantine_path.parent,
                    access._target_quarantine_parent,
                )
                self._verify_windows_quarantine_parent(
                    (
                        access._registration_quarantine_path.parent
                        if access._registration_quarantine_path is not None
                        else self._path / ".git" / _REGISTRATION_QUARANTINE_NAME
                    ),
                    access._registration_quarantine_parent,
                )
            if access._target is not None and (os.name != "nt" or not access._target_root_retired):
                self._verify_quarantine_handle("target", access._target.handle)
            if access._registration is not None and (
                os.name != "nt" or not access._registration_root_retired
            ):
                self._verify_quarantine_handle("registration", access._registration.handle)
            if access._target_probe is not None:
                self._verify_quarantine_handle("target probe", access._target_probe)
            if access._registration_probe is not None:
                self._verify_quarantine_handle("registration probe", access._registration_probe)
            if access._registration_gitdir_proof is not None:
                self._verify_quarantine_file_handle(
                    "registration gitdir proof", access._registration_gitdir_proof
                )
            elif (
                access._registration_initially_present
                and not access._registration_gitdir_proof_retired
            ):
                raise RepositoryAccessDenied("registration gitdir proof is unavailable")
            if access._target_quarantine is not None:
                self._verify_quarantine_handle(
                    "target quarantine", access._target_quarantine.handle
                )
            if access._registration_quarantine is not None:
                self._verify_quarantine_handle(
                    "registration quarantine", access._registration_quarantine.handle
                )
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository quarantine capability is stale") from None
        return access

    def _verify_quarantine_handle(self, label: str, pinned: _QuarantineHandle) -> None:
        try:
            if os.name == "nt":
                api = self._windows
                if api is None:
                    raise RepositoryAccessDenied("Windows path capabilities are unavailable")
                identity = tuple(api.identity(pinned.capability))
            else:
                metadata = os.fstat(pinned.capability)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise RepositoryAccessDenied(f"{label} capability is not a directory")
                identity = (int(metadata.st_dev), int(metadata.st_ino))
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied(f"{label} capability is stale") from None
        if identity != pinned.identity:
            raise RepositoryAccessDenied(f"{label} identity changed")

    def _verify_quarantine_file_handle(self, label: str, pinned: _QuarantineHandle) -> None:
        try:
            if os.name == "nt":
                api = self._windows
                if api is None:
                    raise RepositoryAccessDenied("Windows path capabilities are unavailable")
                info = api.information(pinned.capability)
                if int(info.attributes) & (
                    _FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY
                ):
                    raise RepositoryAccessDenied(f"{label} capability is not a regular file")
                identity = tuple(api.identity(pinned.capability))
            else:
                metadata = os.fstat(pinned.capability)
                if not stat.S_ISREG(metadata.st_mode):
                    raise RepositoryAccessDenied(f"{label} capability is not a regular file")
                identity = (int(metadata.st_dev), int(metadata.st_ino))
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied(f"{label} capability is stale") from None
        if identity != pinned.identity:
            raise RepositoryAccessDenied(f"{label} identity changed")

    def _quarantine_target(self, access: _QuarantineAccess) -> None:
        """Move the exact opened target into its deterministic quarantine root."""

        access = self._accept_mutation_access(access)
        if not access._target_initially_present or access._target is None:
            raise RepositoryAccessDenied("target source is unavailable")
        if access._target_moved or access._target_quarantine is not None:
            raise RepositoryAccessDenied("target quarantine transition already completed")
        if os.name == "nt":
            self._quarantine_windows_entry(
                access,
                access._target,
                access._target_quarantine_parent,
                access._target_quarantine_path,
            )
        else:
            self._quarantine_posix_entry(
                access,
                access._target,
                access._target_quarantine_parent,
                access._target_quarantine_path,
            )
        object.__setattr__(access, "_target_moved", True)

    def _quarantine_registration(self, access: _QuarantineAccess) -> None:
        """Move the exact registration only after the live target is absent."""

        access = self._accept_mutation_access(access)
        if access._registration is None or not access._registration_initially_present:
            raise RepositoryAccessDenied("registration source is unavailable")
        if not access._target_deleted and access._target_initially_present:
            raise RepositoryAccessDenied("target quarantine deletion has not completed")
        self._verify_worktree_removal_state(access)
        if os.name == "nt":
            self._verify_windows_registration_state(access)
        else:
            self._verify_posix_registration_state(access)
        if access._registration_moved or access._registration_quarantine is not None:
            raise RepositoryAccessDenied("registration quarantine transition already completed")
        registration_quarantine_path = access.registration_quarantine_path
        if os.name == "nt":
            if os.path.lexists(access.registration_path):
                proof = access._registration_gitdir_proof
                if proof is None:
                    raise RepositoryAccessDenied("registration source is unavailable")
                self._retire_windows_registration_gitdir_proof(access)
                try:
                    self._quarantine_windows_entry(
                        access,
                        access._registration,
                        access._registration_quarantine_parent,
                        registration_quarantine_path,
                    )
                except RepositoryAccessDenied:
                    self._reacquire_windows_registration_gitdir_proof(
                        access,
                        proof.identity,
                    )
                    raise
            else:
                raise RepositoryAccessDenied("Git worktree registration is absent")
        else:
            self._assert_posix_entry_present(access._registration)
            self._quarantine_posix_entry(
                access,
                access._registration,
                access._registration_quarantine_parent,
                registration_quarantine_path,
            )
        object.__setattr__(access, "_registration_moved", True)

    def _delete_target_quarantine(self, access: _QuarantineAccess) -> None:
        """Delete a moved target quarantine while retaining its proof entry last."""

        access = self._accept_mutation_access(access)
        if access._target_deleted:
            return
        if access._target is None or not access._target_moved or access._target_quarantine is None:
            raise RepositoryAccessDenied("target quarantine has not completed")
        if os.name == "nt":
            self._delete_windows_quarantine(
                access._target_quarantine,
                root_pin=access._target.handle,
                quarantine_parent=access._target_quarantine_parent,
                proof_name=".git",
                retire_root=access._retire_target_root,
                root_retired=access._target_root_retired,
            )
        else:
            self._delete_posix_quarantine(
                access._target_quarantine,
                proof_name=".git",
            )
        object.__setattr__(access, "_target_deleted", True)

    def _delete_registration_quarantine(self, access: _QuarantineAccess) -> None:
        """Delete a moved registration quarantine after target deletion completes."""

        access = self._accept_mutation_access(access)
        if access._registration_deleted:
            return
        if access._registration is None or not access._registration_initially_present:
            raise RepositoryAccessDenied("registration source is unavailable")
        if not access._target_deleted and access._target_initially_present:
            raise RepositoryAccessDenied("target quarantine deletion has not completed")
        if not access._registration_moved or access._registration_quarantine is None:
            raise RepositoryAccessDenied("registration quarantine has not completed")
        self._verify_worktree_removal_state(access)
        if (
            os.name == "nt"
            and not access._target_initially_present
            and not (access._registration_gitdir_proof_retired)
        ):
            self._verify_windows_registration_state(access)
        if os.name == "nt":
            self._delete_windows_quarantine(
                access._registration_quarantine,
                root_pin=access._registration.handle,
                quarantine_parent=access._registration_quarantine_parent,
                proof_name="gitdir",
                retire_root=access._retire_registration_root,
                root_retired=access._registration_root_retired,
            )
        else:
            self._delete_posix_quarantine(
                access._registration_quarantine,
                proof_name="gitdir",
            )
        object.__setattr__(access, "_registration_deleted", True)

    @staticmethod
    def _windows_quarantine_names(api: _WindowsPathApi, handle: int) -> tuple[str, ...]:
        try:
            names = tuple(api.enumerate_names(handle))
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository quarantine enumeration failed") from None
        seen: set[str] = set()
        for name in names:
            try:
                _validate_quarantine_component(name)
            except PathEscape, UnicodeError, ValueError:
                raise RepositoryAccessDenied(
                    "repository quarantine entry name is invalid"
                ) from None
            if name in seen:
                raise RepositoryAccessDenied("repository quarantine enumeration is duplicated")
            seen.add(name)
        return names

    @staticmethod
    def _windows_quarantine_kind(
        api: _WindowsPathApi, handle: int, root_volume: int
    ) -> _WindowsQuarantineNode:
        try:
            identity = tuple(api.identity(handle))
            information = api.information(handle)
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied(
                "repository quarantine entry state is unavailable"
            ) from None
        if not identity or identity[0] != root_volume:
            raise RepositoryAccessDenied("repository quarantine crosses a volume")
        attributes = int(information.attributes)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            kind = "reparse"
        elif attributes & _FILE_ATTRIBUTE_DIRECTORY:
            kind = "directory"
        else:
            kind = "normal"
        return _WindowsQuarantineNode(identity, kind)

    @staticmethod
    def _close_windows_handle(api: _WindowsPathApi, handle: int | None) -> None:
        if handle is not None:
            with contextlib.suppress(OSError, ValueError):
                api.close(handle)

    @classmethod
    def _windows_quarantine_preflight(
        cls,
        api: _WindowsPathApi,
        parent_handle: int,
        *,
        path: tuple[str, ...],
        depth: int,
        root_volume: int,
        proof_name: str,
        budget: list[int],
        snapshot: dict[tuple[str, ...], dict[str, _WindowsQuarantineNode]],
    ) -> None:
        if depth > _QUARANTINE_MAX_DEPTH:
            raise RepositoryAccessDenied("repository quarantine depth limit exceeded")
        names = cls._windows_quarantine_names(api, parent_handle)
        children = snapshot.setdefault(path, {})
        for name in names:
            budget[0] += 1
            if budget[0] > _QUARANTINE_MAX_ENTRIES:
                raise RepositoryAccessDenied("repository quarantine entry limit exceeded")
            if depth == 0 and name == proof_name:
                continue
            if name in children:
                raise RepositoryAccessDenied("repository quarantine enumeration is duplicated")
            child_handle: int | None = None
            list_handle: int | None = None
            try:
                try:
                    child_handle = api.open_child(parent_handle, name)
                except OSError, RepositoryAccessDenied, ValueError:
                    raise RepositoryAccessDenied(
                        "repository quarantine child open failed"
                    ) from None
                node = cls._windows_quarantine_kind(api, child_handle, root_volume)
                children[name] = node
                if node.kind != "directory":
                    continue
                child_depth = depth + 1
                if child_depth > _QUARANTINE_MAX_DEPTH:
                    raise RepositoryAccessDenied("repository quarantine depth limit exceeded")
                try:
                    list_handle = api.open_child(parent_handle, name, list_handle=True)
                except OSError, RepositoryAccessDenied, ValueError:
                    raise RepositoryAccessDenied(
                        "repository quarantine directory list open failed"
                    ) from None
                list_node = cls._windows_quarantine_kind(api, list_handle, root_volume)
                if list_node.kind != "directory" or list_node.identity != node.identity:
                    raise RepositoryAccessDenied("repository quarantine directory identity changed")
                cls._windows_quarantine_preflight(
                    api,
                    list_handle,
                    path=(*path, name),
                    depth=child_depth,
                    root_volume=root_volume,
                    proof_name=proof_name,
                    budget=budget,
                    snapshot=snapshot,
                )
            finally:
                cls._close_windows_handle(api, list_handle)
                cls._close_windows_handle(api, child_handle)

    @classmethod
    def _windows_quarantine_delete_children(
        cls,
        api: _WindowsPathApi,
        parent_handle: int,
        *,
        path: tuple[str, ...],
        depth: int,
        root_volume: int,
        proof_name: str,
        snapshot: dict[tuple[str, ...], dict[str, _WindowsQuarantineNode]],
    ) -> None:
        if depth > _QUARANTINE_MAX_DEPTH:
            raise RepositoryAccessDenied("repository quarantine depth limit exceeded")
        names = cls._windows_quarantine_names(api, parent_handle)
        expected = snapshot.get(path)
        if expected is None:
            raise RepositoryAccessDenied("repository quarantine tree changed")
        remaining_names = {name for name in names if not (depth == 0 and name == proof_name)}
        if remaining_names != set(expected) or len(remaining_names) != len(names) - (
            1 if depth == 0 and proof_name in names else 0
        ):
            raise RepositoryAccessDenied("repository quarantine tree changed")
        for name in names:
            if depth == 0 and name == proof_name:
                continue
            node = expected.get(name)
            if node is None:
                raise RepositoryAccessDenied("repository quarantine tree changed")
            child_handle: int | None = None
            list_handle: int | None = None
            try:
                try:
                    child_handle = api.open_child(parent_handle, name)
                except OSError, RepositoryAccessDenied, ValueError:
                    raise RepositoryAccessDenied(
                        "repository quarantine child open failed"
                    ) from None
                current = cls._windows_quarantine_kind(api, child_handle, root_volume)
                if current != node:
                    raise RepositoryAccessDenied("repository quarantine entry identity changed")
                if node.kind == "directory":
                    try:
                        list_handle = api.open_child(parent_handle, name, list_handle=True)
                    except OSError, RepositoryAccessDenied, ValueError:
                        raise RepositoryAccessDenied(
                            "repository quarantine directory list open failed"
                        ) from None
                    list_node = cls._windows_quarantine_kind(api, list_handle, root_volume)
                    if list_node.kind != "directory" or list_node.identity != node.identity:
                        raise RepositoryAccessDenied(
                            "repository quarantine directory identity changed"
                        )
                    child_path = (*path, name)
                    cls._windows_quarantine_delete_children(
                        api,
                        list_handle,
                        path=child_path,
                        depth=depth + 1,
                        root_volume=root_volume,
                        proof_name=proof_name,
                        snapshot=snapshot,
                    )
                    cls._close_windows_handle(api, list_handle)
                    list_handle = None
                try:
                    api.dispose(child_handle)
                except OSError, RepositoryAccessDenied, ValueError:
                    raise RepositoryAccessDenied(
                        "repository quarantine entry deletion failed"
                    ) from None
                # Windows keeps a POSIX-dispositioned entry visible until the
                # delete pin is closed. Release that exact pin before the
                # final handle-relative absence check; keeping it open would
                # make a successful disposition look like a failed deletion
                # and would also block the enclosing directory.
                cls._close_windows_handle(api, child_handle)
                child_handle = None
            finally:
                cls._close_windows_handle(api, list_handle)
                cls._close_windows_handle(api, child_handle)
        final_names = cls._windows_quarantine_names(api, parent_handle)
        allowed_names = {proof_name} if depth == 0 else set()
        if set(final_names) != allowed_names:
            raise RepositoryAccessDenied("repository quarantine tree changed")

    def _delete_windows_quarantine(
        self,
        quarantine: _QuarantineEntry,
        *,
        root_pin: _QuarantineHandle,
        quarantine_parent: _QuarantineHandle,
        proof_name: str,
        retire_root: Any | None = None,
        root_retired: bool = False,
    ) -> None:
        """Delete one exact quarantine through retained Windows handles only."""

        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        _require_windows_native_pointer_size()

        def retire_root_pin() -> None:
            self._close_windows_handle(api, root_pin.capability)
            if retire_root is not None:
                retire_root(root_pin.capability)

        try:
            list_identity = tuple(api.identity(quarantine.handle.capability))
            parent_identity = tuple(api.identity(quarantine_parent.capability))
            root_identity = (
                root_pin.identity if root_retired else tuple(api.identity(root_pin.capability))
            )
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository quarantine capability is stale") from None
        if (
            not root_identity
            or not list_identity
            or not parent_identity
            or root_identity != root_pin.identity
            or list_identity != quarantine.handle.identity
            or parent_identity != quarantine_parent.identity
        ):
            raise RepositoryAccessDenied("repository quarantine identity changed")
        if root_identity != list_identity:
            raise RepositoryAccessDenied("repository quarantine identity changed")
        if not root_identity or parent_identity[0] != root_identity[0]:
            raise RepositoryAccessDenied("repository quarantine crosses a volume")
        list_node = self._windows_quarantine_kind(
            api, quarantine.handle.capability, root_identity[0]
        )
        if list_node.kind != "directory" or list_node.identity != root_identity:
            raise RepositoryAccessDenied("repository quarantine identity changed")
        if root_retired:
            if self._windows_quarantine_names(api, quarantine.handle.capability):
                raise RepositoryAccessDenied(
                    "repository quarantine root is nonempty after disposition"
                )
            if quarantine.name in self._windows_quarantine_names(api, quarantine_parent.capability):
                raise RepositoryAccessDenied("repository quarantine root is still present")
            return

        root_node = self._windows_quarantine_kind(api, root_pin.capability, root_identity[0])
        if root_node.kind != "directory" or list_node != root_node:
            raise RepositoryAccessDenied("repository quarantine identity changed")

        root_names = self._windows_quarantine_names(api, quarantine.handle.capability)
        if proof_name not in root_names:
            if root_names:
                raise RepositoryAccessDenied(
                    "repository quarantine proof is absent while content remains"
                )
            if quarantine.name not in self._windows_quarantine_names(
                api, quarantine_parent.capability
            ):
                # The retained root pin is no longer useful once its exact
                # namespace entry is already absent.  Release it so the
                # caller can safely retire the capability before any handle
                # value is reused by a later child open.
                retire_root_pin()
                return
            try:
                api.dispose(root_pin.capability)
            except OSError, RepositoryAccessDenied, ValueError:
                raise RepositoryAccessDenied("repository quarantine root deletion failed") from None
            retire_root_pin()
            if quarantine.name in self._windows_quarantine_names(api, quarantine_parent.capability):
                raise RepositoryAccessDenied("repository quarantine root is still present")
            return

        proof_handle: int | None = None
        try:
            try:
                proof_handle = api.open_child(quarantine.handle.capability, proof_name)
            except OSError, RepositoryAccessDenied, ValueError:
                raise RepositoryAccessDenied("repository quarantine proof is unavailable") from None
            proof_node = self._windows_quarantine_kind(api, proof_handle, root_identity[0])
            if proof_node.kind != "normal":
                raise RepositoryAccessDenied("repository quarantine proof is not regular")
            snapshot: dict[tuple[str, ...], dict[str, _WindowsQuarantineNode]] = {}
            self._windows_quarantine_preflight(
                api,
                quarantine.handle.capability,
                path=(),
                depth=0,
                root_volume=root_identity[0],
                proof_name=proof_name,
                budget=[0],
                snapshot=snapshot,
            )
            self._windows_quarantine_delete_children(
                api,
                quarantine.handle.capability,
                path=(),
                depth=0,
                root_volume=root_identity[0],
                proof_name=proof_name,
                snapshot=snapshot,
            )
            current_proof = self._windows_quarantine_kind(api, proof_handle, root_identity[0])
            if current_proof != proof_node:
                raise RepositoryAccessDenied("repository quarantine proof changed")
            try:
                api.dispose(proof_handle)
            except OSError, RepositoryAccessDenied, ValueError:
                raise RepositoryAccessDenied(
                    "repository quarantine proof deletion failed"
                ) from None
            self._close_windows_handle(api, proof_handle)
            proof_handle = None
            if proof_name in self._windows_quarantine_names(api, quarantine.handle.capability):
                raise RepositoryAccessDenied("repository quarantine proof is still present")
        finally:
            self._close_windows_handle(api, proof_handle)

        if self._windows_quarantine_names(api, quarantine.handle.capability):
            raise RepositoryAccessDenied("repository quarantine is nonempty after proof deletion")
        try:
            api.dispose(root_pin.capability)
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository quarantine root deletion failed") from None
        retire_root_pin()
        if quarantine.name in self._windows_quarantine_names(api, quarantine_parent.capability):
            raise RepositoryAccessDenied("repository quarantine root is still present")

    def _delete_posix_quarantine(
        self,
        quarantine: _QuarantineEntry,
        *,
        proof_name: str,
    ) -> None:
        """Delete one exact quarantine using only retained POSIX directory fds."""

        if not _O_DIRECTORY or not _O_NOFOLLOW:
            raise RepositoryAccessDenied("safe POSIX path capabilities are unavailable")
        root_descriptor = quarantine.handle.capability
        try:
            root_metadata = os.fstat(root_descriptor)
        except OSError:
            raise RepositoryAccessDenied("repository quarantine capability is stale") from None
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise RepositoryAccessDenied("repository quarantine is not a directory")
        root_identity = (int(root_metadata.st_dev), int(root_metadata.st_ino))
        if root_identity != quarantine.handle.identity:
            raise RepositoryAccessDenied("repository quarantine identity changed")
        root_mount_id = _posix_mount_id(root_descriptor)

        proof_descriptor: int | None = None
        try:
            try:
                proof_entry = os.stat(
                    proof_name,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not self._posix_quarantine_is_empty(root_descriptor):
                    raise RepositoryAccessDenied(
                        "repository quarantine proof is absent while content remains"
                    )
                self._remove_posix_quarantine_root(quarantine)
                return
            except OSError:
                raise RepositoryAccessDenied("repository quarantine proof is unavailable") from None
            if not stat.S_ISREG(proof_entry.st_mode):
                raise RepositoryAccessDenied("repository quarantine proof is not regular")
            if int(proof_entry.st_dev) != root_identity[0]:
                raise RepositoryAccessDenied("repository quarantine proof crosses a volume")
            try:
                proof_descriptor = os.open(
                    proof_name,
                    os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=root_descriptor,
                )
            except FileNotFoundError:
                raise RepositoryAccessDenied("repository quarantine proof changed") from None
            except OSError:
                raise RepositoryAccessDenied("repository quarantine proof is unavailable") from None

            try:
                proof_metadata = os.fstat(proof_descriptor)
                if not stat.S_ISREG(proof_metadata.st_mode):
                    raise RepositoryAccessDenied("repository quarantine proof is not regular")
                if int(proof_metadata.st_dev) != root_identity[0]:
                    raise RepositoryAccessDenied("repository quarantine proof crosses a volume")
                proof_identity = (int(proof_metadata.st_dev), int(proof_metadata.st_ino))
                if proof_identity != (int(proof_entry.st_dev), int(proof_entry.st_ino)):
                    raise RepositoryAccessDenied("repository quarantine proof changed")
                budget = [0]
                self._delete_posix_quarantine_children(
                    root_descriptor,
                    proof_name=proof_name,
                    depth=0,
                    root_device=root_identity[0],
                    root_mount_id=root_mount_id,
                    budget=budget,
                    validate_only=True,
                )
                self._delete_posix_quarantine_children(
                    root_descriptor,
                    proof_name=proof_name,
                    depth=0,
                    root_device=root_identity[0],
                    root_mount_id=root_mount_id,
                    budget=budget,
                    validate_only=False,
                )
                try:
                    current_proof = os.stat(
                        proof_name,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise RepositoryAccessDenied("repository quarantine proof changed") from None
                if (
                    not stat.S_ISREG(current_proof.st_mode)
                    or (int(current_proof.st_dev), int(current_proof.st_ino)) != proof_identity
                ):
                    raise RepositoryAccessDenied("repository quarantine proof changed")
                try:
                    os.unlink(proof_name, dir_fd=root_descriptor)
                except OSError:
                    raise RepositoryAccessDenied(
                        "repository quarantine proof deletion failed"
                    ) from None
                self._assert_posix_entry_absent(quarantine.handle, proof_name)
            finally:
                with contextlib.suppress(OSError, ValueError):
                    os.close(proof_descriptor)
                proof_descriptor = None

            if not self._posix_quarantine_is_empty(root_descriptor):
                raise RepositoryAccessDenied(
                    "repository quarantine is nonempty after proof deletion"
                )
            self._remove_posix_quarantine_root(quarantine)
        finally:
            if proof_descriptor is not None:
                with contextlib.suppress(OSError, ValueError):
                    os.close(proof_descriptor)

    def _delete_posix_quarantine_children(
        self,
        parent_descriptor: int,
        *,
        proof_name: str | None,
        depth: int,
        root_device: int,
        root_mount_id: int,
        budget: list[int],
        validate_only: bool,
    ) -> None:
        try:
            entries = os.scandir(parent_descriptor)
        except OSError:
            raise RepositoryAccessDenied("repository quarantine enumeration failed") from None
        try:
            for entry in entries:
                name = entry.name
                try:
                    _validate_quarantine_component(name)
                except PathEscape, UnicodeError, ValueError:
                    raise RepositoryAccessDenied(
                        "repository quarantine entry name is invalid"
                    ) from None
                if validate_only:
                    budget[0] += 1
                    if budget[0] > _QUARANTINE_MAX_ENTRIES:
                        raise RepositoryAccessDenied("repository quarantine entry limit exceeded")
                if proof_name is not None and depth == 0 and name == proof_name:
                    continue
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise RepositoryAccessDenied(
                        "repository quarantine entry state is unavailable"
                    ) from None
                if int(metadata.st_dev) != root_device:
                    raise RepositoryAccessDenied("repository quarantine crosses a volume")
                mode = metadata.st_mode
                if stat.S_ISDIR(mode):
                    child_depth = depth + 1
                    if child_depth > _QUARANTINE_MAX_DEPTH:
                        raise RepositoryAccessDenied("repository quarantine depth limit exceeded")
                    child_descriptor: int | None = None
                    try:
                        try:
                            child_descriptor = os.open(
                                name,
                                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                                dir_fd=parent_descriptor,
                            )
                        except OSError:
                            raise RepositoryAccessDenied(
                                "repository quarantine directory open failed"
                            ) from None
                        child_metadata = os.fstat(child_descriptor)
                        if not stat.S_ISDIR(child_metadata.st_mode):
                            raise RepositoryAccessDenied(
                                "repository quarantine directory identity changed"
                            )
                        child_identity = (
                            int(child_metadata.st_dev),
                            int(child_metadata.st_ino),
                        )
                        if child_identity != (
                            int(metadata.st_dev),
                            int(metadata.st_ino),
                        ):
                            raise RepositoryAccessDenied(
                                "repository quarantine directory identity changed"
                            )
                        if _posix_mount_id(child_descriptor) != root_mount_id:
                            raise RepositoryAccessDenied("repository quarantine crosses a mount")
                        self._delete_posix_quarantine_children(
                            child_descriptor,
                            proof_name=None,
                            depth=child_depth,
                            root_device=root_device,
                            root_mount_id=root_mount_id,
                            budget=budget,
                            validate_only=validate_only,
                        )
                    finally:
                        if child_descriptor is not None:
                            with contextlib.suppress(OSError, ValueError):
                                os.close(child_descriptor)
                    if not validate_only:
                        try:
                            os.rmdir(name, dir_fd=parent_descriptor)
                        except OSError:
                            raise RepositoryAccessDenied(
                                "repository quarantine directory deletion failed"
                            ) from None
                        self._assert_posix_name_absent(parent_descriptor, name)
                elif stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                    if not validate_only:
                        try:
                            os.unlink(name, dir_fd=parent_descriptor)
                        except OSError:
                            raise RepositoryAccessDenied(
                                "repository quarantine entry deletion failed"
                            ) from None
                        self._assert_posix_name_absent(parent_descriptor, name)
                else:
                    raise RepositoryAccessDenied(
                        "repository quarantine contains an unsupported entry"
                    )
        except RepositoryAccessDenied:
            raise
        except OSError:
            raise RepositoryAccessDenied("repository quarantine enumeration failed") from None
        finally:
            with contextlib.suppress(OSError, ValueError):
                entries.close()

    @staticmethod
    def _posix_quarantine_is_empty(descriptor: int) -> bool:
        try:
            with os.scandir(descriptor) as entries:
                return next(entries, None) is None
        except OSError:
            raise RepositoryAccessDenied("repository quarantine enumeration failed") from None

    def _remove_posix_quarantine_root(self, quarantine: _QuarantineEntry) -> None:
        self._assert_posix_entry_present(quarantine)
        try:
            os.rmdir(quarantine.name, dir_fd=quarantine.parent.capability)
        except OSError:
            raise RepositoryAccessDenied("repository quarantine root deletion failed") from None
        self._assert_posix_entry_absent(quarantine.parent, quarantine.name)

    def _quarantine_posix_entry(
        self,
        access: _QuarantineAccess,
        source: _QuarantineEntry,
        quarantine_parent: _QuarantineHandle,
        quarantine_path: Path,
    ) -> None:
        self._assert_posix_entry_present(source)
        self._assert_posix_entry_absent(quarantine_parent, source.name)
        try:
            os.rename(
                source.name,
                source.name,
                src_dir_fd=source.parent.capability,
                dst_dir_fd=quarantine_parent.capability,
            )
        except OSError:
            raise RepositoryAccessDenied("repository quarantine rename failed") from None
        self._assert_posix_entry_absent(source.parent, source.name)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                source.name,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=quarantine_parent.capability,
            )
            identity = _fd_identity(descriptor)
            if identity != source.handle.identity:
                raise RepositoryAccessDenied("repository quarantine identity changed")
        except OSError, ValueError:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise RepositoryAccessDenied(
                "repository quarantine destination is unavailable"
            ) from None
        except RepositoryAccessDenied:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise
        access._retain(descriptor)
        entry = _QuarantineEntry(
            source.name,
            quarantine_path,
            quarantine_parent,
            _QuarantineHandle(descriptor, source.handle.identity),
        )
        if source is access._target:
            object.__setattr__(access, "_target_quarantine", entry)
        else:
            object.__setattr__(access, "_registration_quarantine", entry)

    def _quarantine_windows_entry(
        self,
        access: _QuarantineAccess,
        source: _QuarantineEntry,
        quarantine_parent: _QuarantineHandle,
        quarantine_path: Path,
    ) -> None:
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        self._verify_quarantine_handle(source.name, source.handle)
        if os.path.lexists(quarantine_path):
            raise RepositoryAccessDenied("repository quarantine destination already exists")
        self._verify_windows_quarantine_parent(quarantine_path.parent, quarantine_parent)
        try:
            api.rename_directory(
                source.handle.capability,
                quarantine_parent.capability,
                source.name,
            )
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository quarantine rename failed") from None
        self._verify_windows_quarantine_parent(quarantine_path.parent, quarantine_parent)
        if os.path.lexists(source.path):
            raise RepositoryAccessDenied("repository quarantine source is still present")
        descriptor: int | None = None
        try:
            descriptor = api.open_directory_for_verification(quarantine_path)
            identity = tuple(api.identity(descriptor))
            if identity != source.handle.identity:
                raise RepositoryAccessDenied("repository quarantine identity changed")
        except OSError, RepositoryAccessDenied, ValueError:
            if descriptor is not None:
                with contextlib.suppress(OSError, ValueError):
                    api.close(descriptor)
            raise RepositoryAccessDenied(
                "repository quarantine destination is unavailable"
            ) from None
        if descriptor is None:
            raise RepositoryAccessDenied("repository quarantine destination is unavailable")
        access._retain(descriptor)
        entry = _QuarantineEntry(
            source.name,
            quarantine_path,
            quarantine_parent,
            _QuarantineHandle(descriptor, source.handle.identity),
        )
        if source is access._target:
            object.__setattr__(access, "_target_quarantine", entry)
        else:
            object.__setattr__(access, "_registration_quarantine", entry)

    def _verify_windows_quarantine_parent(self, path: Path, expected: _QuarantineHandle) -> None:
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        del path
        try:
            identity = tuple(api.identity(expected.capability))
            api.verify_owner_only_dacl(expected.capability)
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository quarantine root identity changed") from None
        if identity != expected.identity:
            raise RepositoryAccessDenied("repository quarantine root identity changed")

    def _assert_posix_entry_present(self, entry: _QuarantineEntry) -> None:
        try:
            metadata = os.stat(
                entry.name,
                dir_fd=entry.parent.capability,
                follow_symlinks=False,
            )
        except OSError:
            raise RepositoryAccessDenied("repository quarantine source is absent") from None
        identity = (int(metadata.st_dev), int(metadata.st_ino))
        if identity != entry.handle.identity or not stat.S_ISDIR(metadata.st_mode):
            raise RepositoryAccessDenied("repository quarantine source identity changed")

    @staticmethod
    def _assert_posix_entry_absent(parent: _QuarantineHandle, name: str) -> None:
        CanonicalRoot._assert_posix_name_absent(parent.capability, name)

    @staticmethod
    def _assert_posix_name_absent(parent: int, name: str) -> None:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise RepositoryAccessDenied(
                "repository quarantine source state is unavailable"
            ) from None
        raise RepositoryAccessDenied("repository quarantine source is still present")

    @contextlib.contextmanager
    def _open_posix_worktree_quarantine(
        self, target_name: str, registration_name: str
    ) -> Iterator[_QuarantineAccess]:
        with self._open_posix_worktree_removal(
            target_name, registration_name, mode=_REMOVAL_LIVE
        ) as access:
            yield access

    @contextlib.contextmanager
    def _open_posix_worktree_removal(
        self,
        target_name: str,
        registration_name: str | None,
        *,
        mode: str,
        prepared: bool = False,
    ) -> Iterator[_QuarantineAccess]:
        if not _O_DIRECTORY or not _O_NOFOLLOW:
            raise RepositoryAccessDenied("safe POSIX path capabilities are unavailable")
        if mode not in {_REMOVAL_LIVE, _REMOVAL_STALE_REGISTRATION, _REMOVAL_ABSENT}:
            raise RepositoryAccessDenied("repository removal mode is invalid")
        if mode != _REMOVAL_ABSENT and registration_name is None:
            raise RepositoryAccessDenied("registration source is unavailable")
        resources: list[int] = []
        access: _QuarantineAccess | None = None
        try:
            root_descriptor = os.open(
                self._path,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            )
            resources.append(root_descriptor)
            root_identity = _fd_identity(root_descriptor)
            if root_identity != self._identity:
                raise RepositoryAccessDenied("repository root identity changed")
            git_descriptor = _open_posix_directory_at(root_descriptor, ".git")
            resources.append(git_descriptor)
            lock_descriptor = _open_posix_mutation_lock(git_descriptor)
            resources.append(lock_descriptor)
            worktree_parent_descriptor = _open_posix_directory_at(root_descriptor, ".worktrees")
            resources.append(worktree_parent_descriptor)
            target_path = self._path / ".worktrees" / target_name
            target_descriptor: int | None = None
            if mode == _REMOVAL_LIVE:
                target_descriptor = _open_posix_directory_at(
                    worktree_parent_descriptor, target_name
                )
                resources.append(target_descriptor)
            else:
                self._assert_posix_name_absent(worktree_parent_descriptor, target_name)
            metadata_parent_descriptor = _open_posix_directory_at(git_descriptor, "worktrees")
            resources.append(metadata_parent_descriptor)
            registration_path: Path | None = None
            registration_descriptor: int | None = None
            registration_gitdir_descriptor: int | None = None
            registration_gitdir_content: bytes | None = None
            if registration_name is not None:
                registration_path = self._path / ".git" / "worktrees" / registration_name
                registration_descriptor = _open_posix_directory_at(
                    metadata_parent_descriptor, registration_name
                )
                resources.append(registration_descriptor)
                _reject_posix_registration_lock(registration_descriptor)
                registration_gitdir_descriptor = _open_posix_gitdir(registration_descriptor)
                resources.append(registration_gitdir_descriptor)
                registration_gitdir_content = _validate_gitdir_content(
                    _read_posix_bounded(registration_gitdir_descriptor),
                    registration_path,
                    target_path / ".git",
                )
            target_quarantine_parent = _open_or_create_posix_directory_at(
                worktree_parent_descriptor, _TARGET_QUARANTINE_NAME
            )
            resources.append(target_quarantine_parent)
            registration_quarantine_parent = _open_or_create_posix_directory_at(
                git_descriptor, _REGISTRATION_QUARANTINE_NAME
            )
            resources.append(registration_quarantine_parent)
            _verify_quarantine_parent(target_quarantine_parent)
            _verify_quarantine_parent(registration_quarantine_parent)
            _reject_posix_destination(target_quarantine_parent, target_name)
            if registration_name is not None:
                _reject_posix_destination(registration_quarantine_parent, registration_name)
            volume_descriptors = [
                root_descriptor,
                git_descriptor,
                worktree_parent_descriptor,
                metadata_parent_descriptor,
                target_quarantine_parent,
                registration_quarantine_parent,
            ]
            if target_descriptor is not None:
                volume_descriptors.append(target_descriptor)
            if registration_descriptor is not None:
                volume_descriptors.append(registration_descriptor)
            if registration_gitdir_descriptor is not None:
                volume_descriptors.append(registration_gitdir_descriptor)
            _require_same_posix_volume(*volume_descriptors)
            self._revalidate_root()
            access = _QuarantineAccess(
                seal=_QUARANTINE_SEAL,
                owner=self._access_owner,
                resources=resources,
                root=_QuarantineHandle(root_descriptor, root_identity),
                git=_QuarantineHandle(git_descriptor, _fd_identity(git_descriptor)),
                worktree_parent=_QuarantineHandle(
                    worktree_parent_descriptor, _fd_identity(worktree_parent_descriptor)
                ),
                metadata_parent=_QuarantineHandle(
                    metadata_parent_descriptor, _fd_identity(metadata_parent_descriptor)
                ),
                target_path=target_path,
                registration_path=registration_path,
                target=(
                    _QuarantineEntry(
                        target_name,
                        target_path,
                        _QuarantineHandle(
                            worktree_parent_descriptor, _fd_identity(worktree_parent_descriptor)
                        ),
                        _QuarantineHandle(target_descriptor, _fd_identity(target_descriptor)),
                    )
                    if target_descriptor is not None
                    else None
                ),
                registration=(
                    _QuarantineEntry(
                        registration_name,
                        registration_path,
                        _QuarantineHandle(
                            metadata_parent_descriptor,
                            _fd_identity(metadata_parent_descriptor),
                        ),
                        _QuarantineHandle(
                            registration_descriptor, _fd_identity(registration_descriptor)
                        ),
                    )
                    if registration_name is not None
                    and registration_path is not None
                    and registration_descriptor is not None
                    else None
                ),
                target_initially_present=mode == _REMOVAL_LIVE,
                registration_initially_present=registration_descriptor is not None,
                target_quarantine_parent=_QuarantineHandle(
                    target_quarantine_parent, _fd_identity(target_quarantine_parent)
                ),
                registration_quarantine_parent=_QuarantineHandle(
                    registration_quarantine_parent, _fd_identity(registration_quarantine_parent)
                ),
                target_quarantine_path=self._path
                / ".worktrees"
                / _TARGET_QUARANTINE_NAME
                / target_name,
                registration_quarantine_path=(
                    self._path / ".git" / _REGISTRATION_QUARANTINE_NAME / registration_name
                    if registration_name is not None
                    else None
                ),
                registration_gitdir_content=registration_gitdir_content,
                registration_gitdir_proof=(
                    _QuarantineHandle(
                        registration_gitdir_descriptor,
                        _fd_identity(registration_gitdir_descriptor),
                    )
                    if registration_gitdir_descriptor is not None
                    else None
                ),
                mutation_bound=mode != _REMOVAL_LIVE or not prepared,
                target_probe=(
                    _QuarantineHandle(target_descriptor, _fd_identity(target_descriptor))
                    if prepared and mode == _REMOVAL_LIVE and target_descriptor is not None
                    else None
                ),
                registration_probe=(
                    _QuarantineHandle(
                        registration_descriptor,
                        _fd_identity(registration_descriptor),
                    )
                    if prepared and mode == _REMOVAL_LIVE and registration_descriptor is not None
                    else None
                ),
            )
            yield access
            self._revalidate_root()
        except RepositoryAccessDenied:
            raise
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository quarantine is unavailable") from None
        finally:
            if access is None:
                for descriptor in reversed(resources):
                    with contextlib.suppress(OSError):
                        os.close(descriptor)

    @contextlib.contextmanager
    def _open_windows_worktree_quarantine(
        self, target_name: str, registration_name: str
    ) -> Iterator[_QuarantineAccess]:
        with self._open_windows_worktree_removal(
            target_name, registration_name, mode=_REMOVAL_LIVE
        ) as access:
            yield access

    @contextlib.contextmanager
    def _open_windows_worktree_removal(
        self,
        target_name: str,
        registration_name: str | None,
        *,
        mode: str,
        prepared: bool = False,
    ) -> Iterator[_QuarantineAccess]:
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        if mode not in {_REMOVAL_LIVE, _REMOVAL_STALE_REGISTRATION, _REMOVAL_ABSENT}:
            raise RepositoryAccessDenied("repository removal mode is invalid")
        if prepared and mode != _REMOVAL_LIVE:
            raise RepositoryAccessDenied("repository prepared mode is invalid")
        if mode != _REMOVAL_ABSENT and registration_name is None:
            raise RepositoryAccessDenied("registration source is unavailable")
        resources: list[int] = []
        access: _QuarantineAccess | None = None
        try:
            root_path = self._path
            root_handle = api.open_directory(root_path)
            resources.append(root_handle)
            root_identity = tuple(api.identity(root_handle))
            if root_identity != self._identity:
                raise RepositoryAccessDenied("repository root identity changed")
            git_path = root_path / ".git"
            git_handle = api.open_directory(git_path)
            resources.append(git_handle)
            lock_handle = api.open_mutation_lock(git_path / "forge-worktree.lock")
            resources.append(lock_handle)
            worktree_parent_path = root_path / ".worktrees"
            worktree_parent_handle = api.open_directory(worktree_parent_path)
            resources.append(worktree_parent_handle)
            target_path = worktree_parent_path / target_name
            target_handle: int | None = None
            if mode == _REMOVAL_LIVE:
                target_handle = (
                    api.open_directory_for_verification(target_path)
                    if prepared
                    else api.open_directory_for_rename(target_path)
                )
                resources.append(target_handle)
            else:
                api.assert_child_absent(worktree_parent_handle, target_name)
            metadata_parent_path = git_path / "worktrees"
            metadata_parent_handle = api.open_directory(metadata_parent_path)
            resources.append(metadata_parent_handle)
            registration_path: Path | None = None
            registration_handle: int | None = None
            registration_gitdir_handle: int | None = None
            registration_gitdir_content: bytes | None = None
            if registration_name is not None:
                registration_path = metadata_parent_path / registration_name
                registration_handle = (
                    api.open_directory_for_verification(registration_path)
                    if prepared
                    else api.open_directory_for_rename(registration_path)
                )
                resources.append(registration_handle)
                api.assert_child_absent(registration_handle, "locked")
                registration_gitdir_handle = api.open_proof_child(registration_handle, "gitdir")
                resources.append(registration_gitdir_handle)
                proof_info = api.information(registration_gitdir_handle)
                if int(proof_info.attributes) & (
                    _FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY
                ):
                    raise RepositoryAccessDenied("repository proof is not a regular file")
                registration_gitdir_content = _validate_gitdir_content(
                    api.read_bounded(registration_gitdir_handle),
                    registration_path,
                    target_path / ".git",
                )
            target_quarantine_path = worktree_parent_path / _TARGET_QUARANTINE_NAME
            target_quarantine_parent = _open_or_create_windows_directory(
                api, target_quarantine_path, quarantine_parent=True
            )
            resources.append(target_quarantine_parent)
            registration_quarantine_path = git_path / _REGISTRATION_QUARANTINE_NAME
            registration_quarantine_parent = _open_or_create_windows_directory(
                api, registration_quarantine_path, quarantine_parent=True
            )
            resources.append(registration_quarantine_parent)
            api.assert_child_absent(target_quarantine_parent, target_name)
            if registration_name is not None:
                api.assert_child_absent(registration_quarantine_parent, registration_name)
            git_identity = tuple(api.identity(git_handle))
            worktree_parent_identity = tuple(api.identity(worktree_parent_handle))
            metadata_parent_identity = tuple(api.identity(metadata_parent_handle))
            target_quarantine_parent_identity = tuple(api.identity(target_quarantine_parent))
            registration_quarantine_parent_identity = tuple(
                api.identity(registration_quarantine_parent)
            )
            target_identity = (
                tuple(api.identity(target_handle)) if target_handle is not None else None
            )
            registration_identity = (
                tuple(api.identity(registration_handle))
                if registration_handle is not None
                else None
            )
            registration_gitdir_identity = (
                tuple(api.identity(registration_gitdir_handle))
                if registration_gitdir_handle is not None
                else None
            )
            identities = (
                git_identity,
                worktree_parent_identity,
                metadata_parent_identity,
                target_quarantine_parent_identity,
                registration_quarantine_parent_identity,
                *((target_identity,) if target_identity is not None else ()),
                *((registration_identity,) if registration_identity is not None else ()),
                *(
                    (registration_gitdir_identity,)
                    if registration_gitdir_identity is not None
                    else ()
                ),
            )
            if any(identity[0] != root_identity[0] for identity in identities):
                raise RepositoryAccessDenied("repository quarantine crosses a volume")
            self._revalidate_root()
            access = _QuarantineAccess(
                seal=_QUARANTINE_SEAL,
                owner=self._access_owner,
                resources=resources,
                root=_QuarantineHandle(root_handle, root_identity),
                git=_QuarantineHandle(git_handle, git_identity),
                worktree_parent=_QuarantineHandle(worktree_parent_handle, worktree_parent_identity),
                metadata_parent=_QuarantineHandle(metadata_parent_handle, metadata_parent_identity),
                target_path=target_path,
                registration_path=registration_path,
                target=(
                    _QuarantineEntry(
                        target_name,
                        target_path,
                        _QuarantineHandle(worktree_parent_handle, worktree_parent_identity),
                        _QuarantineHandle(target_handle, target_identity),
                    )
                    if target_handle is not None and target_identity is not None
                    else None
                ),
                registration=(
                    _QuarantineEntry(
                        registration_name,
                        registration_path,
                        _QuarantineHandle(metadata_parent_handle, metadata_parent_identity),
                        _QuarantineHandle(registration_handle, registration_identity),
                    )
                    if registration_name is not None
                    and registration_path is not None
                    and registration_handle is not None
                    and registration_identity is not None
                    else None
                ),
                target_initially_present=mode == _REMOVAL_LIVE,
                registration_initially_present=registration_handle is not None,
                target_quarantine_parent=_QuarantineHandle(
                    target_quarantine_parent, target_quarantine_parent_identity
                ),
                registration_quarantine_parent=_QuarantineHandle(
                    registration_quarantine_parent,
                    registration_quarantine_parent_identity,
                ),
                target_quarantine_path=target_quarantine_path / target_name,
                registration_quarantine_path=(
                    registration_quarantine_path / registration_name
                    if registration_name is not None
                    else None
                ),
                registration_gitdir_content=registration_gitdir_content,
                registration_gitdir_proof=(
                    _QuarantineHandle(
                        registration_gitdir_handle,
                        registration_gitdir_identity,
                    )
                    if registration_gitdir_handle is not None
                    and registration_gitdir_identity is not None
                    else None
                ),
                mutation_bound=mode != _REMOVAL_LIVE or not prepared,
                target_probe=(
                    _QuarantineHandle(target_handle, target_identity)
                    if prepared
                    and mode == _REMOVAL_LIVE
                    and target_handle is not None
                    and target_identity is not None
                    else None
                ),
                registration_probe=(
                    _QuarantineHandle(registration_handle, registration_identity)
                    if prepared
                    and mode == _REMOVAL_LIVE
                    and registration_handle is not None
                    and registration_identity is not None
                    else None
                ),
            )
            yield access
            self._revalidate_root()
        except RepositoryAccessDenied:
            raise
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository quarantine is unavailable") from None
        finally:
            if access is None:
                for handle in reversed(resources):
                    api.close(handle)

    def _active_directory_access(self, normalized: str) -> _DirectoryAccess | None:
        """Return the live operation capability for this exact root and cwd."""

        access = _ACTIVE_DIRECTORY_ACCESS.get()
        if access is None:
            return None
        if access.root_path != self._path:
            return None
        return self._accept_directory_access(normalized, access)

    def _accept_directory_access(
        self, normalized: str, access: _DirectoryAccess
    ) -> _DirectoryAccess:
        """Validate a live owner-sealed capability for this root identity."""

        if (
            not isinstance(access, _DirectoryAccess)
            or not access._live
            or access._owner is not self._access_owner
            or access.root_path != self._path
            or access.root_identity != self._identity
            or access.normalized != normalized
            or access.path != self._path.joinpath(*normalized.split("/"))
        ):
            raise RepositoryAccessDenied("repository directory capability is not trusted")
        if access.git_capability is not None:
            try:
                if os.name == "nt":
                    api = self._windows
                    git_identity = tuple(api.identity(access.git_capability)) if api else None
                else:
                    git_identity = _fd_identity(access.git_capability)
            except OSError, RepositoryAccessDenied, ValueError:
                raise RepositoryAccessDenied("Git metadata capability is not trusted") from None
            if git_identity != access.git_identity:
                raise RepositoryAccessDenied("Git metadata identity changed")
        return access

    def _verify_directory_access(
        self, normalized: str, access: _DirectoryAccess
    ) -> _DirectoryAccess:
        """Verify the retained target handle still has its pinned identity."""

        access = self._accept_directory_access(normalized, access)
        try:
            self._revalidate_root()
            if os.name == "nt":
                api = self._windows
                if api is None:
                    raise RepositoryAccessDenied("Windows path capabilities are unavailable")
                identity = tuple(api.identity(access.capability))
            else:
                metadata = os.fstat(access.capability)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise RepositoryAccessDenied(
                        "repository directory capability is not a directory"
                    )
                identity = (int(metadata.st_dev), int(metadata.st_ino))
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository directory capability is stale") from None
        if identity != access.identity:
            raise RepositoryAccessDenied("repository directory identity changed")
        try:
            if os.name == "nt":
                api = self._windows
                if api is None:
                    raise RepositoryAccessDenied("Windows path capabilities are unavailable")
                path_handle = api.open_directory(access.path)
                try:
                    path_identity = tuple(api.identity(path_handle))
                finally:
                    api.close(path_handle)
            else:
                metadata = os.stat(access.path, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise RepositoryAccessDenied("repository path is not a directory")
                path_identity = (int(metadata.st_dev), int(metadata.st_ino))
        except OSError, RepositoryAccessDenied, ValueError:
            raise RepositoryAccessDenied("repository directory path is stale") from None
        if path_identity != access.identity:
            raise RepositoryAccessDenied("repository directory path identity changed")
        return access

    def _launch_path_for_access(
        self, normalized: str, access: _DirectoryAccess, *, require_fd: bool = False
    ) -> str:
        """Derive a process cwd from the retained capability, never caller data."""

        access = self._accept_directory_access(normalized, access)
        if os.name == "nt":
            return str(access.path)
        proc_fd_root = Path("/proc/self/fd")
        if access.capability < 0:
            raise RepositoryAccessDenied("safe POSIX directory launch is unavailable")
        if not proc_fd_root.is_dir():
            if require_fd:
                raise RepositoryAccessDenied("safe POSIX directory launch is unavailable")
            return str(access.path)
        return str(proc_fd_root / str(access.capability))

    def _pass_fds_for_access(self, normalized: str, access: _DirectoryAccess) -> tuple[int, ...]:
        """Return only the retained metadata descriptor needed by POSIX Git."""

        access = self._accept_directory_access(normalized, access)
        if os.name == "nt" or access.git_capability is None:
            return ()
        return (access.git_capability,)

    def _git_directory_for_access(self, normalized: str, access: _DirectoryAccess) -> str:
        """Return the exact retained source ``.git`` path for Git's environment."""

        access = self._accept_directory_access(normalized, access)
        if access.git_path is None or access.git_capability is None:
            raise RepositoryAccessDenied("Git metadata capability is unavailable")
        if os.name == "nt":
            return str(access.git_path)
        proc_fd_root = Path("/proc/self/fd")
        if not proc_fd_root.is_dir():
            raise RepositoryAccessDenied("safe POSIX metadata launch is unavailable")
        return str(proc_fd_root / str(access.git_capability))

    def _directory_access_matches_path(self, access: _DirectoryAccess) -> bool:
        """Check that the named target still identifies the retained directory."""

        try:
            self._verify_directory_access(access.normalized, access)
            return True
        except OSError, RuntimeError, ValueError, RepositoryAccessDenied:
            return False

    def _directory_access_is_empty(self, access: _DirectoryAccess) -> bool:
        """Check one retained directory without following a replaced pathname."""

        try:
            access = self._verify_directory_access(access.normalized, access)
            if os.name == "nt":
                with os.scandir(access.path) as entries:
                    return next(entries, None) is None
            return not os.listdir(access.capability)
        except OSError, RuntimeError, TypeError, ValueError:
            return False

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
    def _create_posix_directory(
        self, normalized_parent: str, leaf: str, normalized: str
    ) -> Iterator[_DirectoryAccess]:
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
            git_descriptor = os.open(
                ".git",
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=root_descriptor,
            )
            if not stat.S_ISDIR(os.fstat(git_descriptor).st_mode):
                os.close(git_descriptor)
                raise RepositoryAccessDenied("Git metadata path is not a directory")
            directory_descriptors.append(git_descriptor)
            if os.name != "nt":
                # This lock serializes cooperating Forge controllers; raw same-UID
                # namespace changes remain outside the control-plane boundary.
                fcntl_api: Any = __import__("fcntl")

                lock_descriptor = os.open(
                    "forge-worktree.lock",
                    os.O_CREAT | os.O_RDWR | _O_NOFOLLOW | _O_CLOEXEC,
                    mode=0o600,
                    dir_fd=git_descriptor,
                )
                if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
                    os.close(lock_descriptor)
                    raise RepositoryAccessDenied("Git worktree lock is not a regular file")
                try:
                    fcntl_api.flock(lock_descriptor, fcntl_api.LOCK_EX | fcntl_api.LOCK_NB)
                except OSError, ValueError:
                    os.close(lock_descriptor)
                    raise RepositoryAccessDenied("Git worktree operation is busy") from None
                directory_descriptors.append(lock_descriptor)
            for part in () if normalized_parent == "." else normalized_parent.split("/"):
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                        dir_fd=current,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
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

            try:
                os.mkdir(leaf, mode=0o700, dir_fd=current)
            except FileExistsError:
                raise RepositoryAccessDenied("repository directory already exists") from None
            leaf_descriptor = os.open(
                leaf,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=current,
            )
            if not stat.S_ISDIR(os.fstat(leaf_descriptor).st_mode):
                os.close(leaf_descriptor)
                raise RepositoryAccessDenied("repository path is not a directory")
            directory_descriptors.append(leaf_descriptor)
            try:
                metadata_descriptor = os.open(
                    "worktrees",
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=git_descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir("worktrees", mode=0o700, dir_fd=git_descriptor)
                except FileExistsError:
                    pass
                metadata_descriptor = os.open(
                    "worktrees",
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=git_descriptor,
                )
            if not stat.S_ISDIR(os.fstat(metadata_descriptor).st_mode):
                os.close(metadata_descriptor)
                raise RepositoryAccessDenied("Git worktree metadata is not a directory")
            metadata_identity = _fd_identity(metadata_descriptor)
            directory_descriptors.append(metadata_descriptor)
            proc_fd_root = Path("/proc/self/fd")
            if not proc_fd_root.is_dir():
                raise RepositoryAccessDenied("safe POSIX directory launch is unavailable")
            self._revalidate_root()
            access = _DirectoryAccess(
                seal=_ACCESS_SEAL,
                owner=self._access_owner,
                path=self._path.joinpath(*normalized.split("/")),
                capability=leaf_descriptor,
                root_path=self._path,
                root_identity=self._identity,
                identity=_fd_identity(leaf_descriptor),
                normalized=normalized,
                git_path=self._path / ".git",
                git_capability=git_descriptor,
                git_identity=_fd_identity(git_descriptor),
            )
            yield access
            if _fd_identity(metadata_descriptor) != metadata_identity:
                raise RepositoryAccessDenied("Git worktree metadata identity changed")
            self._revalidate_root()
        except RepositoryAccessDenied:
            raise
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository directory is unavailable") from None
        finally:
            for descriptor in reversed(directory_descriptors):
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    @contextlib.contextmanager
    def _create_windows_directory(
        self, normalized_parent: str, leaf: str, normalized: str
    ) -> Iterator[_DirectoryAccess]:
        api = self._windows
        if api is None:
            raise RepositoryAccessDenied("Windows path capabilities are unavailable")
        self._revalidate_root()
        handles: list[int] = []
        try:
            root_handle = api.open_directory(self._path)
            handles.append(root_handle)
            if tuple(api.identity(root_handle)) != self._identity:
                raise RepositoryAccessDenied("repository root identity changed")
            git_path = self._path / ".git"
            git_handle = api.open_directory(git_path)
            handles.append(git_handle)
            lock_handle = api.open_mutation_lock(git_path / "forge-worktree.lock")
            handles.append(lock_handle)
            current = self._path
            for part in () if normalized_parent == "." else normalized_parent.split("/"):
                current = current / part
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                handles.append(api.open_directory(current))
            leaf_path = current / leaf
            try:
                leaf_path.mkdir()
            except FileExistsError:
                raise RepositoryAccessDenied("repository directory already exists") from None
            leaf_handle = api.open_directory(leaf_path)
            handles.append(leaf_handle)
            metadata_path = git_path / "worktrees"
            try:
                metadata_path.mkdir()
            except FileExistsError:
                pass
            metadata_handle = api.open_directory(metadata_path)
            handles.append(metadata_handle)
            metadata_identity = tuple(api.identity(metadata_handle))
            self._revalidate_root()
            yield _DirectoryAccess(
                seal=_ACCESS_SEAL,
                owner=self._access_owner,
                path=leaf_path,
                capability=leaf_handle,
                root_path=self._path,
                root_identity=self._identity,
                identity=tuple(api.identity(leaf_handle)),
                normalized=normalized,
                git_path=git_path,
                git_capability=git_handle,
                git_identity=tuple(api.identity(git_handle)),
            )
            if tuple(api.identity(metadata_handle)) != metadata_identity:
                raise RepositoryAccessDenied("Git worktree metadata identity changed")
            self._revalidate_root()
        except RepositoryAccessDenied:
            raise
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository directory is unavailable") from None
        finally:
            for handle in reversed(handles):
                api.close(handle)

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
            yield _DirectoryAccess(
                seal=_ACCESS_SEAL,
                owner=self._access_owner,
                path=path,
                capability=current,
                root_path=self._path,
                root_identity=self._identity,
                identity=_fd_identity(current),
                normalized=normalized,
            )
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
            yield _DirectoryAccess(
                seal=_ACCESS_SEAL,
                owner=self._access_owner,
                path=current,
                capability=handles[-1],
                root_path=self._path,
                root_identity=self._identity,
                identity=tuple(api.identity(handles[-1])),
                normalized=normalized,
            )
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


def _posix_mount_id(descriptor: int) -> int:
    """Return the kernel mount identity for one open POSIX directory fd."""

    if sys.platform != "linux" or _LINUX_STATX is None:
        raise RepositoryAccessDenied("POSIX mount identity is unavailable")
    result = ctypes.create_string_buffer(_LINUX_STATX_BUFFER_SIZE)
    try:
        status = _LINUX_STATX(
            descriptor,
            b"",
            _LINUX_AT_EMPTY_PATH,
            _LINUX_STATX_MNT_ID,
            ctypes.byref(result),
        )
    except OSError:
        raise RepositoryAccessDenied("POSIX mount identity is unavailable") from None
    if status != 0:
        raise RepositoryAccessDenied("POSIX mount identity is unavailable")
    mask = int.from_bytes(
        result.raw[_LINUX_STATX_MASK_OFFSET : _LINUX_STATX_MASK_OFFSET + 4],
        "little",
    )
    if not mask & _LINUX_STATX_MNT_ID:
        raise RepositoryAccessDenied("POSIX mount identity is unavailable")
    mount_id = int.from_bytes(
        result.raw[_LINUX_STATX_MNT_ID_OFFSET : _LINUX_STATX_MNT_ID_OFFSET + 8],
        "little",
    )
    if mount_id == 0:
        raise RepositoryAccessDenied("POSIX mount identity is unavailable")
    return mount_id


def _validate_quarantine_component(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise PathEscape("repository quarantine name is invalid")
    if any(character in value for character in ("/", "\\", "\x00", ":")):
        raise PathEscape("repository quarantine name is invalid")
    if len(os.fsencode(value)) > _QUARANTINE_MAX_COMPONENT_BYTES:
        raise PathEscape("repository quarantine name is too long")
    if os.name == "nt" and value.rstrip(" .") != value:
        raise PathEscape("repository quarantine name is invalid")
    return value


def _open_posix_directory_at(parent: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=parent,
        )
    except OSError:
        raise RepositoryAccessDenied("repository directory is unavailable") from None
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise RepositoryAccessDenied("repository path is not a directory")
        return descriptor
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


def _open_or_create_posix_directory_at(parent: int, name: str) -> int:
    try:
        return _open_posix_directory_at(parent, name)
    except RepositoryAccessDenied:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError:
            raise RepositoryAccessDenied("repository quarantine root is unavailable") from None
        return _open_posix_directory_at(parent, name)


def _open_posix_mutation_lock(git_descriptor: int) -> int:
    fcntl_api: Any = __import__("fcntl")
    try:
        descriptor = os.open(
            "forge-worktree.lock",
            os.O_CREAT | os.O_RDWR | _O_NOFOLLOW | _O_CLOEXEC,
            mode=0o600,
            dir_fd=git_descriptor,
        )
    except OSError:
        raise RepositoryAccessDenied("Git worktree lock is unavailable") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RepositoryAccessDenied("Git worktree lock is not a regular file")
        fcntl_api.flock(descriptor, fcntl_api.LOCK_EX | fcntl_api.LOCK_NB)
        return descriptor
    except RepositoryAccessDenied:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
    except OSError, ValueError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise RepositoryAccessDenied("Git worktree operation is busy") from None


def _reject_posix_registration_lock(registration: int) -> None:
    try:
        os.stat("locked", dir_fd=registration, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise RepositoryAccessDenied("Git worktree registration lock is unavailable") from None
    raise RepositoryAccessDenied("Git worktree registration is locked")


def _read_posix_bounded(descriptor: int) -> bytes:
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RepositoryAccessDenied("repository proof is not a regular file")
        size = int(metadata.st_size)
        if size > _GITDIR_MAX_BYTES:
            raise RepositoryAccessDenied("repository proof is oversized")
        os.lseek(descriptor, 0, os.SEEK_SET)
        content = os.read(descriptor, size)
        after = os.fstat(descriptor)
    except RepositoryAccessDenied:
        raise
    except OSError:
        raise RepositoryAccessDenied("repository proof is unavailable") from None
    if not stat.S_ISREG(after.st_mode) or int(after.st_size) != size or len(content) != size:
        raise RepositoryAccessDenied("repository proof changed")
    return content


def _reject_existing_link_components(path: Path) -> None:
    """Reject links/reparses in the existing prefix of one marker target."""

    if not path.is_absolute() or not path.anchor:
        raise RepositoryAccessDenied("repository proof path is invalid")
    current = Path(path.anchor)
    missing = False
    for component in path.parts[1:]:
        if component in {"", "."}:
            continue
        if component == "..":
            current = current.parent
            continue
        current /= component
        if missing:
            continue
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            missing = True
            continue
        except OSError, ValueError:
            raise RepositoryAccessDenied("repository proof path is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RepositoryAccessDenied("repository proof path contains a link")


def _normalise_gitdir_path(path: Path) -> Path:
    try:
        _reject_existing_link_components(path)
        return Path(os.path.normpath(os.fspath(path)))
    except RepositoryAccessDenied:
        raise
    except OSError, RuntimeError, TypeError, ValueError:
        raise RepositoryAccessDenied("repository proof path is unavailable") from None


def _validate_gitdir_content(
    content: bytes, registration_path: Path, expected_target: Path
) -> bytes:
    """Validate one bounded Git registration marker against the exact target."""

    if len(content) > _GITDIR_MAX_BYTES:
        raise RepositoryAccessDenied("repository proof is oversized")
    try:
        record = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise RepositoryAccessDenied("repository proof is malformed") from None
    if not record.endswith("\n") or record.count("\n") != 1:
        raise RepositoryAccessDenied("repository proof is malformed")
    registered = record[:-1]
    if not registered or "\r" in registered or "\x00" in registered:
        raise RepositoryAccessDenied("repository proof is malformed")
    registered_path = Path(registered)
    if not registered_path.is_absolute():
        registered_path = registration_path / registered_path
    registered_path = _normalise_gitdir_path(registered_path)
    expected_path = _normalise_gitdir_path(expected_target)
    if os.path.normcase(os.fspath(registered_path)) != os.path.normcase(os.fspath(expected_path)):
        raise RepositoryAccessDenied("repository proof targets a different repository")
    return content


def _open_posix_gitdir(registration: int) -> int:
    try:
        descriptor = os.open(
            "gitdir",
            os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=registration,
        )
    except OSError:
        raise RepositoryAccessDenied("repository proof is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RepositoryAccessDenied("repository proof is not a regular file")
        return descriptor
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


def _verify_quarantine_parent(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RepositoryAccessDenied("repository quarantine root permissions are unsafe")


def _reject_posix_destination(parent: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise RepositoryAccessDenied("repository quarantine destination is unavailable") from None
    raise RepositoryAccessDenied("repository quarantine destination already exists")


def _require_same_posix_volume(*descriptors: int) -> None:
    devices = {int(os.fstat(descriptor).st_dev) for descriptor in descriptors}
    if len(devices) != 1:
        raise RepositoryAccessDenied("repository quarantine crosses a volume")


def _open_or_create_windows_directory(
    api: _WindowsPathApi, path: Path, *, quarantine_parent: bool = False
) -> int:
    opener = api.open_quarantine_parent if quarantine_parent else api.open_directory
    try:
        return opener(path)
    except RepositoryAccessDenied:
        try:
            api.create_secure_directory(path)
        except OSError:
            raise RepositoryAccessDenied("repository quarantine root is unavailable") from None
        return opener(path)


__all__ = ["CanonicalRoot", "PathEscape", "RepositoryAccessDenied"]
