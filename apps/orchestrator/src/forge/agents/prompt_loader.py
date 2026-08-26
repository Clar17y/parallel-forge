"""Exact, versioned loading for Forge-owned role instructions."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from forge.domain.actor import AgentRole
from forge.domain.agent import AgentRequest

_MAX_INSTRUCTION_BYTES: Final = 10_000
_VERSION_HEADER = re.compile(
    r"\A<!-- forge-instruction-version: ([A-Za-z0-9][A-Za-z0-9._-]{0,95}) -->\Z"
)
_ROLE_PATHS = MappingProxyType(
    {
        AgentRole.PLANNER: Path("planner/instructions.md"),
        AgentRole.DEVELOPER: Path("developer/instructions.md"),
        AgentRole.REVIEWER: Path("reviewer/instructions.md"),
    }
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class PromptLoadError(RuntimeError):
    """A role instruction could not be loaded safely."""

    def __init__(self) -> None:
        super().__init__("role instruction could not be loaded safely")


class PromptChanged(RuntimeError):
    """A frozen execution request no longer matches its role instruction."""

    def __init__(self) -> None:
        super().__init__("frozen role instruction changed")


@dataclass(frozen=True, slots=True, kw_only=True)
class LoadedPrompt:
    """Detached exact instruction evidence suitable for an AgentRequest."""

    role: AgentRole
    version: str
    instruction: str = field(repr=False)
    digest: str

    def __post_init__(self) -> None:
        if type(self.role) is not AgentRole:
            raise TypeError("loaded prompt role must be an AgentRole")
        if _VERSION_HEADER.fullmatch(f"<!-- forge-instruction-version: {self.version} -->") is None:
            raise ValueError("loaded prompt version is invalid")
        encoded = self.instruction.encode("utf-8")
        if not encoded or len(encoded) > _MAX_INSTRUCTION_BYTES:
            raise ValueError("loaded prompt instruction size is invalid")
        if _version_from_instruction(self.instruction) != self.version:
            raise ValueError("loaded prompt version does not match instruction")
        if hashlib.sha256(encoded).hexdigest() != self.digest:
            raise ValueError("loaded prompt digest does not match instruction")


class PromptLoader:
    """Load only the three fixed Forge role-instruction files."""

    def __init__(self, instruction_root: Path) -> None:
        if not isinstance(instruction_root, Path):
            raise TypeError("instruction root must be a Path")
        try:
            supplied_stat = instruction_root.lstat()
            if not stat.S_ISDIR(supplied_stat.st_mode) or _is_reparse(supplied_stat):
                raise PromptLoadError
            root = instruction_root.resolve(strict=True)
            root_stat = root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
                raise PromptLoadError
        except OSError, RuntimeError, PromptLoadError:
            raise PromptLoadError from None
        self._root = root
        self._root_identity = _object_identity(root_stat)

    def load(self, role: AgentRole) -> LoadedPrompt:
        """Load and hash the exact bytes for one closed agent role."""

        if type(role) is not AgentRole:
            raise TypeError("prompt role must be an AgentRole")
        try:
            data = self._read_stable(_ROLE_PATHS[role])
            if data.startswith(b"\xef\xbb\xbf") or len(data) > _MAX_INSTRUCTION_BYTES:
                raise PromptLoadError
            instruction = data.decode("utf-8", errors="strict")
            version = _version_from_instruction(instruction)
            return LoadedPrompt(
                role=role,
                version=version,
                instruction=instruction,
                digest=hashlib.sha256(data).hexdigest(),
            )
        except OSError, UnicodeError, KeyError, PromptLoadError, ValueError:
            raise PromptLoadError from None

    def verify_unchanged(self, request: AgentRequest) -> LoadedPrompt:
        """Re-read a frozen request's fixed role file and reject any drift."""

        if not isinstance(request, AgentRequest):
            raise TypeError("prompt verification requires an AgentRequest")
        try:
            loaded = self.load(request.role)
        except PromptLoadError:
            raise PromptChanged from None
        if (
            loaded.version != request.instruction_version
            or loaded.instruction != request.system_instruction
            or loaded.digest != request.instruction_digest
        ):
            raise PromptChanged
        return loaded

    def _read_stable(self, relative_path: Path) -> bytes:
        root_stat = self._root.lstat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or _is_reparse(root_stat)
            or _object_identity(root_stat) != self._root_identity
        ):
            raise PromptLoadError

        role_directory = self._root / relative_path.parent
        role_stat = role_directory.lstat()
        if not stat.S_ISDIR(role_stat.st_mode) or _is_reparse(role_stat):
            raise PromptLoadError

        path = self._root / relative_path
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
            raise PromptLoadError

        with path.open("rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _file_snapshot(opened) != _file_snapshot(before):
                raise PromptLoadError
            data = handle.read(_MAX_INSTRUCTION_BYTES + 1)
            after_handle = os.fstat(handle.fileno())

        after_path = path.lstat()
        if (
            _file_snapshot(after_handle) != _file_snapshot(before)
            or _file_snapshot(after_path) != _file_snapshot(before)
            or _is_reparse(after_path)
            or len(data) > _MAX_INSTRUCTION_BYTES
        ):
            raise PromptLoadError
        return data


def _file_snapshot(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _object_identity(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _is_reparse(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & _REPARSE_POINT)


def _version_from_instruction(instruction: str) -> str:
    lines = instruction.splitlines()
    if not lines:
        raise ValueError("loaded prompt instruction is invalid")
    header_match = _VERSION_HEADER.fullmatch(lines[0])
    if header_match is None:
        raise ValueError("loaded prompt instruction header is invalid")
    if sum(1 for line in lines if _VERSION_HEADER.fullmatch(line) is not None) != 1:
        raise ValueError("loaded prompt instruction header is duplicated")
    if not "\n".join(lines[1:]).strip():
        raise ValueError("loaded prompt instruction body is blank")
    return header_match.group(1)


__all__ = ["LoadedPrompt", "PromptChanged", "PromptLoadError", "PromptLoader"]
