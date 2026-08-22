"""Verified asynchronous filesystem storage for content-addressed artifacts."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from forge.artifacts._errors import ArtifactIntegrityError, ArtifactStoreError
from forge.domain.artifact import (
    ArtifactDescriptor,
    canonical_storage_pointer,
    validate_artifact_digest,
)

if TYPE_CHECKING:
    from forge.artifacts._win32 import WindowsArtifactIO

_DEFAULT_BOUNDING_POLICY: Final[str] = "none"
_HASH_CHUNK: Final[int] = 1024 * 1024
_O_DIRECTORY: Final[int] = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
_O_TMPFILE: Final[int] = getattr(os, "O_TMPFILE", 0)


@dataclass(slots=True)
class _PosixLayout:
    root_fd: int
    namespace_fd: int
    shard_fd: int
    shard: str


class FilesystemArtifactStore:
    """Publish immutable blobs below one pinned configured root directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).absolute()
        self._root_identity: tuple[int, int] | None = None
        self._identity_lock = threading.Lock()
        self._windows: WindowsArtifactIO | None = None
        if os.name == "nt":
            from forge.artifacts._win32 import WindowsArtifactIO

            self._windows = WindowsArtifactIO(self._root)

    def _before_publish(self, target: Path) -> None:
        """Testing seam invoked while the namespace capability is held."""

        del target

    def _before_read_open(self, target: Path) -> None:
        """Testing seam invoked after namespace acquisition and before file open."""

        del target

    async def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        max_bytes: int | None = None,
        bounding_policy: str = _DEFAULT_BOUNDING_POLICY,
    ) -> ArtifactDescriptor:
        if not isinstance(data, bytes):
            raise TypeError("artifact content must be bytes")
        bounded, truncated, original_count, policy = _bound_bytes(
            data,
            max_bytes=max_bytes,
            bounding_policy=bounding_policy,
        )
        return await asyncio.to_thread(
            self._put_sync,
            bounded,
            media_type,
            truncated,
            original_count,
            policy,
        )

    async def open_bytes(self, digest: str) -> bytes:
        validate_artifact_digest(digest)
        return await asyncio.to_thread(self._read_verified_sync, digest)

    async def verify(self, digest: str) -> bool:
        validate_artifact_digest(digest)
        await asyncio.to_thread(self._read_verified_sync, digest)
        return True

    def _put_sync(
        self,
        data: bytes,
        media_type: str,
        truncated: bool,
        original_count: int,
        policy: str,
    ) -> ArtifactDescriptor:
        digest = hashlib.sha256(data).hexdigest()
        target = self._target_path(digest)
        if self._windows is not None:
            target = self._windows.put(
                digest,
                data,
                before_publish=self._before_publish,
            )
        elif os.name == "posix":
            self._put_posix(digest, data, target, self._before_publish)
        else:
            raise ArtifactStoreError("artifact storage is unsupported on this platform")
        return ArtifactDescriptor(
            digest=digest,
            media_type=media_type,
            byte_count=len(data),
            storage_path=target,
            truncated=truncated,
            original_byte_count=original_count,
            truncation_policy=policy,
        )

    def _read_verified_sync(self, digest: str) -> bytes:
        target = self._target_path(digest)
        if self._windows is not None:
            return self._windows.read(
                digest,
                before_open=self._before_read_open,
            )
        if os.name != "posix":
            raise ArtifactStoreError("artifact storage is unsupported on this platform")
        with self._posix_layout(digest=digest, create=False) as layout:
            self._before_read_open(target)
            descriptor = _open_posix_regular(layout.shard_fd, target.name, missing_ok=True)
            if descriptor is None:
                raise ArtifactIntegrityError("artifact blob is unavailable")
            try:
                data = _read_verified_fd(descriptor, digest)
            finally:
                os.close(descriptor)
            self._verify_posix_layout(layout)
            return data

    def _put_posix(
        self,
        digest: str,
        data: bytes,
        target: Path,
        before_publish: Callable[[Path], None],
    ) -> None:
        target_name = target.name
        temp_fd: int | None = None
        with self._posix_layout(digest=digest, create=True) as layout:
            existing = _open_posix_regular(layout.shard_fd, target_name, missing_ok=True)
            if existing is not None:
                try:
                    _read_verified_fd(existing, digest)
                finally:
                    os.close(existing)
                self._verify_posix_layout(layout)
                return
            try:
                temp_fd = _open_posix_unnamed_temp(layout.shard_fd)
                _require_regular_fd(temp_fd)
                _write_fd(temp_fd, data)
                os.fsync(temp_fd)
                _read_verified_fd(temp_fd, digest)
                before_publish(target)
                self._verify_posix_layout(layout)
                try:
                    _link_posix_unnamed_temp(temp_fd, layout.shard_fd, target_name)
                except FileExistsError:
                    winner = _open_posix_regular(
                        layout.shard_fd,
                        target_name,
                        missing_ok=True,
                    )
                    if winner is None:
                        raise ArtifactIntegrityError("artifact winner disappeared")
                    try:
                        _read_verified_fd(winner, digest)
                    finally:
                        os.close(winner)
                else:
                    os.fsync(layout.shard_fd)

                self._verify_posix_layout(layout)
                winner = _open_posix_regular(layout.shard_fd, target_name, missing_ok=True)
                if winner is None:
                    raise ArtifactIntegrityError("published artifact disappeared")
                try:
                    _read_verified_fd(winner, digest)
                finally:
                    os.close(winner)
            finally:
                if temp_fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(temp_fd)

    @contextlib.contextmanager
    def _posix_layout(self, *, digest: str, create: bool) -> Iterator[_PosixLayout]:
        if not _O_NOFOLLOW or not _O_DIRECTORY:
            raise ArtifactStoreError("POSIX no-follow directory capabilities are unavailable")
        root_fd: int | None = None
        namespace_fd: int | None = None
        shard_fd: int | None = None
        try:
            root_fd = _open_posix_absolute_directory(self._root, create=create)
            self._pin_root_identity(_fd_identity(root_fd))
            namespace_fd = _open_posix_child(root_fd, "sha256", create=create)
            shard = digest[:2]
            shard_fd = _open_posix_child(namespace_fd, shard, create=create)
            yield _PosixLayout(root_fd, namespace_fd, shard_fd, shard)
        finally:
            for descriptor in (shard_fd, namespace_fd, root_fd):
                if descriptor is not None:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)

    def _verify_posix_layout(self, layout: _PosixLayout) -> None:
        root_fd: int | None = None
        namespace_fd: int | None = None
        shard_fd: int | None = None
        try:
            root_fd = _open_posix_absolute_directory(self._root, create=False)
            if _fd_identity(root_fd) != _fd_identity(layout.root_fd):
                raise ArtifactIntegrityError("artifact root identity changed")
            namespace_fd = _open_posix_child(root_fd, "sha256", create=False)
            if _fd_identity(namespace_fd) != _fd_identity(layout.namespace_fd):
                raise ArtifactIntegrityError("artifact namespace identity changed")
            shard_fd = _open_posix_child(namespace_fd, layout.shard, create=False)
            if _fd_identity(shard_fd) != _fd_identity(layout.shard_fd):
                raise ArtifactIntegrityError("artifact shard identity changed")
        finally:
            for descriptor in (shard_fd, namespace_fd, root_fd):
                if descriptor is not None:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)

    def _pin_root_identity(self, identity: tuple[int, int]) -> None:
        with self._identity_lock:
            if self._root_identity is None:
                self._root_identity = identity
            elif self._root_identity != identity:
                raise ArtifactIntegrityError("artifact root identity changed")

    def _target_path(self, digest: str) -> Path:
        return self._root / Path(canonical_storage_pointer(digest))


def _open_posix_absolute_directory(path: Path, *, create: bool) -> int:
    if not path.is_absolute() or not path.anchor:
        raise ArtifactStoreError("artifact root must be absolute")
    current = os.open(path.anchor, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = _open_posix_child(current, part, create=create)
            os.close(current)
            current = child
        return current
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(current)
        raise


def _open_posix_child(parent_fd: int, name: str, *, create: bool) -> int:
    if not name or name in {".", ".."} or "/" in name:
        raise ArtifactIntegrityError("artifact path component is invalid")
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise ArtifactIntegrityError("artifact namespace is unavailable") from None
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise ArtifactStoreError("artifact directory could not be created") from error
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise ArtifactIntegrityError("artifact directory could not be opened safely") from error
    except OSError as error:
        raise ArtifactIntegrityError("artifact directory could not be opened safely") from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ArtifactIntegrityError("artifact path contains a non-directory")
        if created:
            os.fsync(descriptor)
            os.fsync(parent_fd)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_posix_regular(parent_fd: int, name: str, *, missing_ok: bool) -> int | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ArtifactIntegrityError("artifact blob is missing") from None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ArtifactIntegrityError("artifact path is linked or nonregular") from error
        raise ArtifactIntegrityError("artifact blob is unavailable") from error
    try:
        _require_regular_fd(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_regular_fd(descriptor: int) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise ArtifactIntegrityError("artifact path is not a regular file")


def _open_posix_unnamed_temp(parent_fd: int) -> int:
    if not _O_TMPFILE:
        raise ArtifactStoreError("unnamed artifact temp files are unavailable")
    try:
        return os.open(".", os.O_RDWR | _O_TMPFILE, 0o600, dir_fd=parent_fd)
    except OSError as error:
        raise ArtifactStoreError("unnamed artifact temp files are unavailable") from error


def _link_posix_unnamed_temp(descriptor: int, parent_fd: int, name: str) -> None:
    source = f"/proc/self/fd/{descriptor}"
    try:
        os.link(source, name, dst_dir_fd=parent_fd, follow_symlinks=True)
    except FileExistsError:
        raise
    except OSError as error:
        raise ArtifactStoreError("unnamed artifact publication is unavailable") from error


def _fd_identity(descriptor: int) -> tuple[int, int]:
    result = os.fstat(descriptor)
    return (int(result.st_dev), int(result.st_ino))


def _write_fd(descriptor: int, data: bytes) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise ArtifactStoreError("artifact temporary file write made no progress")
        offset += written
    os.ftruncate(descriptor, len(data))


def _read_verified_fd(descriptor: int, digest: str) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    computed = hashlib.sha256()
    while chunk := os.read(descriptor, _HASH_CHUNK):
        chunks.append(chunk)
        computed.update(chunk)
    if computed.hexdigest() != digest:
        raise ArtifactIntegrityError("artifact blob failed digest verification")
    return b"".join(chunks)


def _bound_bytes(
    data: bytes,
    *,
    max_bytes: int | None,
    bounding_policy: str,
) -> tuple[bytes, bool, int, str]:
    if max_bytes is None:
        if bounding_policy not in {"none", "head_tail"}:
            raise ValueError("unknown artifact bounding policy")
        return data, False, len(data), "none"
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("artifact byte bound must be a positive integer")
    if bounding_policy != "head_tail":
        raise ValueError("bounded artifacts require the head_tail policy")
    if len(data) <= max_bytes:
        return data, False, len(data), "none"
    head_count = max_bytes // 2
    tail_count = max_bytes - head_count
    return data[:head_count] + data[-tail_count:], True, len(data), bounding_policy


__all__ = ["ArtifactIntegrityError", "ArtifactStoreError", "FilesystemArtifactStore"]
