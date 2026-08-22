"""Verified asynchronous filesystem storage for content-addressed artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from pathlib import Path
from typing import Final

from forge.domain.artifact import ArtifactDescriptor, canonical_storage_pointer


class ArtifactIntegrityError(RuntimeError):
    """A blob is missing, linked, nonregular, or does not match its digest."""


class ArtifactStoreError(RuntimeError):
    """A safe artifact publication could not be completed."""


_HASH_CHUNK: Final[int] = 1024 * 1024
_DEFAULT_BOUNDING_POLICY: Final[str] = "none"


class FilesystemArtifactStore:
    """Publish immutable blobs below one configured root directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).absolute()

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
            data, max_bytes=max_bytes, bounding_policy=bounding_policy
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
        _validate_digest(digest)
        return await asyncio.to_thread(self._read_verified_sync, digest)

    async def verify(self, digest: str) -> bool:
        _validate_digest(digest)
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
        target = self._safe_target(digest, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._reject_link_or_nonregular_parent(target.parent)
        if target.exists():
            self._read_verified_sync(digest)
            return self._descriptor(
                digest,
                target,
                media_type,
                len(data),
                truncated,
                original_count,
                policy,
            )

        temporary = target.with_name(f".{target.name}.{secrets.token_hex(12)}.tmp")
        created = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temporary, flags, 0o600)
            created = True
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            if _hash_file(temporary) != digest:
                raise ArtifactIntegrityError("temporary artifact failed digest verification")
            try:
                # A hard link atomically publishes without replacing a concurrent winner.
                os.link(temporary, target)
            except FileExistsError:
                self._read_verified_sync(digest)
            except OSError as error:
                raise ArtifactStoreError(
                    "artifact publication could not be made exclusive"
                ) from error
            else:
                _fsync_directory(target.parent)
            return self._descriptor(
                digest,
                target,
                media_type,
                len(data),
                truncated,
                original_count,
                policy,
            )
        finally:
            if created:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _descriptor(
        self,
        digest: str,
        target: Path,
        media_type: str,
        byte_count: int,
        truncated: bool,
        original_count: int,
        policy: str,
    ) -> ArtifactDescriptor:
        return ArtifactDescriptor(
            digest=digest,
            media_type=media_type,
            byte_count=byte_count,
            storage_path=target,
            truncated=truncated,
            original_byte_count=original_count,
            truncation_policy=policy,
        )

    def _read_verified_sync(self, digest: str) -> bytes:
        target = self._safe_target(digest, for_write=False)
        self._reject_link_or_nonregular(target)
        try:
            with target.open("rb") as handle:
                data = handle.read()
        except (FileNotFoundError, OSError) as error:
            raise ArtifactIntegrityError("artifact blob is unavailable") from error
        if hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactIntegrityError("artifact blob failed digest verification")
        return data

    def _safe_target(self, digest: str, *, for_write: bool) -> Path:
        del for_write
        _validate_digest(digest)
        if not self._root.exists():
            self._root.mkdir(parents=True, exist_ok=True)
        self._reject_link_or_nonregular_parent(self._root)
        target = self._root / Path(canonical_storage_pointer(digest))
        try:
            resolved_parent = target.parent.resolve(strict=False)
            resolved_root = self._root.resolve(strict=True)
        except OSError as error:
            raise ArtifactIntegrityError("artifact root cannot be resolved") from error
        if resolved_parent != resolved_root and not resolved_parent.is_relative_to(resolved_root):
            raise ArtifactIntegrityError("artifact path escapes the configured root")
        self._reject_link_or_nonregular_parent(target.parent)
        return target

    @staticmethod
    def _reject_link_or_nonregular(path: Path) -> None:
        try:
            stat_result = path.lstat()
        except FileNotFoundError as error:
            raise ArtifactIntegrityError("artifact blob is missing") from error
        if path.is_symlink() or not path.is_file():
            raise ArtifactIntegrityError("artifact path is not a regular file")
        if getattr(stat_result, "st_file_attributes", 0) & 0x400:
            raise ArtifactIntegrityError("artifact path is a reparse point")

    def _reject_link_or_nonregular_parent(self, path: Path) -> None:
        current = path
        while current != self._root.parent:
            if current.exists():
                try:
                    stat_result = current.lstat()
                except OSError as error:
                    raise ArtifactIntegrityError("artifact path cannot be inspected") from error
                if current.is_symlink() or not current.is_dir():
                    raise ArtifactIntegrityError("artifact path contains a link or non-directory")
                if getattr(stat_result, "st_file_attributes", 0) & 0x400:
                    raise ArtifactIntegrityError("artifact path contains a reparse point")
            if current == self._root:
                break
            current = current.parent


def _validate_digest(digest: str) -> None:
    if not isinstance(digest, str) or len(digest) != 64 or digest != digest.lower():
        raise ValueError("artifact digest must be lowercase hexadecimal SHA-256")
    try:
        int(digest, 16)
    except ValueError as error:
        raise ValueError("artifact digest must be lowercase hexadecimal SHA-256") from error


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
    except OSError as error:
        raise ArtifactIntegrityError("artifact temporary file could not be read") from error
    return digest.hexdigest()


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


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["ArtifactIntegrityError", "ArtifactStoreError", "FilesystemArtifactStore"]
