"""Shared repository-policy path validation primitives."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
        "nul",
        "prn",
    }
)
RESERVED_REPOSITORY_COMPONENTS = frozenset(
    {
        ".git",
        ".worktrees",
        ".forge",
        ".forge-worktrees",
        "node_modules",
        ".venv",
        "venv",
        "env",
    }
)


def policy_path_key(value: str) -> str:
    return value.casefold() if os.name == "nt" else value


def normalize_policy_path(value: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError("policy paths must be nonblank canonical repository paths")
    if "\x00" in value or "\\" in value:
        raise ValueError("policy paths must use canonical forward slashes")
    if value.startswith("/") or _DRIVE_PREFIX.match(value):
        raise ValueError("policy paths must be relative")
    parts = tuple(value.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("policy paths must not contain traversal or empty components")
    if value != "/".join(parts):
        raise ValueError("policy paths must use canonical spelling")
    if any(_unsafe_windows_component(part) for part in parts):
        raise ValueError("policy paths contain an unsafe Windows component")
    reserved = {
        component.casefold() if os.name == "nt" else component
        for component in RESERVED_REPOSITORY_COMPONENTS
    }
    if any((part.casefold() if os.name == "nt" else part) in reserved for part in parts):
        raise ValueError("policy paths contain a reserved repository component")
    return value


def _unsafe_windows_component(value: str) -> bool:
    """Reject names that acquire special meaning on Windows before I/O."""

    if ":" in value or value[-1:] in {".", " "}:
        return True
    stem = value.rstrip(" .").split(".", maxsplit=1)[0].casefold()
    return stem in _WINDOWS_DEVICE_NAMES


def normalize_policy_paths(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = normalize_policy_path(value)
        key = policy_path_key(path)
        if key in seen:
            raise ValueError("policy paths must not contain duplicates")
        seen.add(key)
        normalized.append(path)
    return tuple(normalized)


def union_policy_paths(*groups: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: dict[str, str] = {}
    for group in groups:
        for value in group:
            path = normalize_policy_path(value)
            key = policy_path_key(path)
            previous = seen.get(key)
            if previous is not None:
                if previous != path:
                    raise ValueError("policy paths contain a platform alias")
                continue
            seen[key] = path
            result.append(path)
    return tuple(result)


__all__ = [
    "RESERVED_REPOSITORY_COMPONENTS",
    "normalize_policy_path",
    "normalize_policy_paths",
    "policy_path_key",
    "union_policy_paths",
]
