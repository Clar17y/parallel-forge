"""Owner-protected recovery manifests for standalone developer worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.resource import ResourceState, WorktreeIdentity

_ERROR = "manifest operation failed"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_CHECKPOINT = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_MAX_BYTES = 128 * 1024
_REPARSE = 0x400


class WorktreeManifestError(RuntimeError):
    """A standalone manifest could not be handled without ambiguity."""

    def __init__(self) -> None:
        super().__init__(_ERROR)


class DeveloperWorktreeManifest(BaseModel):
    """Non-secret exact identity and recovery state for one developer worktree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=1, frozen=True)
    project_id: UUID
    repository_path: str
    branch: str
    worktree_name: str
    worktree_path: str
    base_sha: str
    policy_version: int = Field(ge=1)
    database_state: ResourceState
    database_name: str | None = None
    database_role: str | None = None
    secret_id: str | None = None
    completed_checkpoints: tuple[str, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def schema_is_supported(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported manifest schema")
        return value

    @field_validator("repository_path", "worktree_path")
    @classmethod
    def path_is_absolute_and_canonical(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or any(part in {".", ".."} for part in path.parts[1:]):
            raise ValueError("manifest path is invalid")
        return str(path)

    @field_validator("base_sha")
    @classmethod
    def sha_is_exact(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("manifest base SHA is invalid")
        return value

    @field_validator("completed_checkpoints")
    @classmethod
    def checkpoints_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 1024 or len(set(value)) != len(value):
            raise ValueError("manifest checkpoints are invalid")
        if any(_CHECKPOINT.fullmatch(item) is None for item in value):
            raise ValueError("manifest checkpoint is invalid")
        return value

    @model_validator(mode="after")
    def identity_is_exact(self) -> DeveloperWorktreeManifest:
        identity = WorktreeIdentity.for_developer(
            self.project_id,
            self.branch,
            self.database_state is not ResourceState.DISABLED,
        )
        if self.worktree_name != identity.worktree_name:
            raise ValueError("manifest identity is invalid")
        expected_path = Path(self.repository_path) / ".worktrees" / identity.worktree_name
        if Path(self.worktree_path) != expected_path:
            raise ValueError("manifest worktree path is invalid")
        if self.database_state is ResourceState.DISABLED:
            if any(
                value is not None
                for value in (self.database_name, self.database_role, self.secret_id)
            ):
                raise ValueError("disabled manifest database identity is invalid")
        else:
            if (
                self.database_name != identity.database_name
                or self.database_role != identity.database_role
                or not self.secret_id
            ):
                raise ValueError("manifest database identity is invalid")
        return self


class WorktreeManifestStore:
    """Atomically store exact standalone manifests below one local data root."""

    def __init__(self, data_root: str | os.PathLike[str]) -> None:
        path = Path(data_root)
        if not path.is_absolute():
            path = path.resolve()
        self._root = path / "worktrees"
        if os.name == "nt":
            from forge.tools.paths import _WindowsPathApi

            self._windows: Any | None = _WindowsPathApi()
        else:
            self._windows = None

    def path_for(self, project_id: UUID, branch: str) -> Path:
        if not isinstance(project_id, UUID):
            raise WorktreeManifestError()
        try:
            WorktreeIdentity.for_developer(project_id, branch, False)
        except Exception:  # noqa: BLE001 - filesystem diagnostics are redacted
            raise WorktreeManifestError() from None
        digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()
        return self._root / f"{project_id.hex}-{digest}.json"

    def exists(self, project_id: UUID, branch: str) -> bool:
        path = self.path_for(project_id, branch)
        self._prepare_root(create=False)
        if not os.path.lexists(path):
            return False
        self._reject_unsafe(path, directory=False)
        return True

    def create(self, manifest: DeveloperWorktreeManifest) -> None:
        path = self.path_for(manifest.project_id, manifest.branch)
        self._prepare_root(create=True)
        if os.path.lexists(path):
            raise WorktreeManifestError()
        self._publish(path, manifest, replace=False)

    def load(self, project_id: UUID, branch: str) -> DeveloperWorktreeManifest:
        path = self.path_for(project_id, branch)
        self._prepare_root(create=False)
        try:
            self._reject_unsafe(path, directory=False)
            data = self._read_bytes(path)
            if len(data) > _MAX_BYTES:
                raise WorktreeManifestError()
            manifest = DeveloperWorktreeManifest.model_validate_json(data)
            if manifest.project_id != project_id or manifest.branch != branch:
                raise WorktreeManifestError()
            return manifest
        except WorktreeManifestError:
            raise
        except Exception:  # noqa: BLE001 - filesystem diagnostics are redacted
            raise WorktreeManifestError() from None

    def save(self, manifest: DeveloperWorktreeManifest) -> None:
        current = self.load(manifest.project_id, manifest.branch)
        if self._stable_identity(current) != self._stable_identity(manifest):
            raise WorktreeManifestError()
        self._publish(
            self.path_for(manifest.project_id, manifest.branch),
            manifest,
            replace=True,
        )

    def delete(self, manifest: DeveloperWorktreeManifest) -> None:
        current = self.load(manifest.project_id, manifest.branch)
        if current != manifest:
            raise WorktreeManifestError()
        path = self.path_for(manifest.project_id, manifest.branch)
        try:
            if self._windows is None:
                path.unlink()
            else:
                api = self._windows
                parent = api.open_secret_directory(self._root)
                handle = None
                try:
                    handle = api.open_secret_file(
                        parent,
                        path.name,
                        access=0x80000000 | 0x00010000 | 0x00020000 | 0x00100000,
                        missing_ok=False,
                    )
                    assert handle is not None
                    api.dispose(handle)
                finally:
                    if handle is not None:
                        api.close(handle)
                    api.close(parent)
            self._flush_root()
        except Exception:  # noqa: BLE001 - filesystem diagnostics are redacted
            raise WorktreeManifestError() from None

    def _prepare_root(self, *, create: bool) -> None:
        try:
            if self._windows is None:
                if create:
                    self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not self._root.exists():
                    return
                self._reject_unsafe(self._root, directory=True)
                os.chmod(self._root, 0o700)
            else:
                self._root.parent.mkdir(parents=True, exist_ok=True)
                if create and not os.path.lexists(self._root):
                    self._windows.create_secure_directory(self._root)
                if not os.path.lexists(self._root):
                    return
                handle = self._windows.open_secret_directory(self._root)
                self._windows.close(handle)
        except WorktreeManifestError:
            raise
        except Exception:  # noqa: BLE001 - filesystem diagnostics are redacted
            raise WorktreeManifestError() from None

    @staticmethod
    def _reject_unsafe(path: Path, *, directory: bool) -> None:
        metadata = os.lstat(path)
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISLNK(metadata.st_mode) or attributes & _REPARSE:
            raise WorktreeManifestError()
        if directory and not stat.S_ISDIR(metadata.st_mode):
            raise WorktreeManifestError()
        if not directory and (not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1):
            raise WorktreeManifestError()

    def _publish(
        self,
        path: Path,
        manifest: DeveloperWorktreeManifest,
        *,
        replace: bool,
    ) -> None:
        payload = json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_BYTES:
            raise WorktreeManifestError()
        temporary = self._root / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            if self._windows is None:
                descriptor = os.open(
                    temporary,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(descriptor, payload)
                    os.fsync(descriptor)
                    os.fchmod(descriptor, 0o600)
                finally:
                    os.close(descriptor)
            else:
                handle = self._windows.create_secret_file(temporary, temporary.name)
                try:
                    self._windows.write_secret(handle, payload)
                finally:
                    self._windows.close(handle)
            self._reject_unsafe(temporary, directory=False)
            if not replace and os.path.lexists(path):
                raise WorktreeManifestError()
            if replace:
                self._reject_unsafe(path, directory=False)
            os.replace(temporary, path)
            self._reject_unsafe(path, directory=False)
            if self._windows is not None:
                self._verify_windows_file(path)
            self._flush_root()
        except WorktreeManifestError:
            raise
        except Exception:  # noqa: BLE001 - filesystem diagnostics are redacted
            raise WorktreeManifestError() from None
        finally:
            try:
                if os.path.lexists(temporary):
                    temporary.unlink()
            except OSError:
                pass

    def _flush_root(self) -> None:
        if self._windows is not None:
            handle = self._windows.open_secret_directory(self._root)
            try:
                self._windows.flush_secret_directory(handle)
            finally:
                self._windows.close(handle)
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self._root, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_bytes(self, path: Path) -> bytes:
        if self._windows is None:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
                    raise WorktreeManifestError()
                chunks: list[bytes] = []
                total = 0
                while total <= _MAX_BYTES:
                    chunk = os.read(descriptor, min(64 * 1024, _MAX_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        api = self._windows
        parent = api.open_secret_directory(self._root)
        handle = None
        try:
            handle = api.open_secret_file(
                parent,
                path.name,
                access=0x80000000 | 0x00000080 | 0x00020000 | 0x00100000,
                missing_ok=False,
            )
            assert handle is not None
            return cast(bytes, api.read_secret(handle, _MAX_BYTES + 1))
        finally:
            if handle is not None:
                api.close(handle)
            api.close(parent)

    def _verify_windows_file(self, path: Path) -> None:
        assert self._windows is not None
        parent = self._windows.open_secret_directory(self._root)
        handle = None
        try:
            handle = self._windows.open_secret_file(
                parent,
                path.name,
                access=0x80000000 | 0x00000080 | 0x00020000 | 0x00100000,
                missing_ok=False,
            )
            if handle is None:
                raise WorktreeManifestError()
        finally:
            if handle is not None:
                self._windows.close(handle)
            self._windows.close(parent)

    @staticmethod
    def _stable_identity(manifest: DeveloperWorktreeManifest) -> tuple[object, ...]:
        return (
            manifest.schema_version,
            manifest.project_id,
            manifest.repository_path,
            manifest.branch,
            manifest.worktree_name,
            manifest.worktree_path,
            manifest.base_sha,
            manifest.policy_version,
        )


__all__ = [
    "DeveloperWorktreeManifest",
    "WorktreeManifestError",
    "WorktreeManifestStore",
]
