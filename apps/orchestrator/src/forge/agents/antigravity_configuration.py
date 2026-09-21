"""Hermetic, Forge-owned configuration for an Antigravity probe.

The official client may use its normal system keyring for an already-authorized
Google subscription, but this directory is deliberately *not* a copy of its
normal home.  In particular, no credential, plugin, hook, project, or MCP
state is permitted to enter this tree.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import cast
from uuid import UUID

ANTIGRAVITY_ISOLATION_AGENT = "forge-isolation-probe"

_AGENT = """---
name: forge-isolation-probe
description: Forge's zero-capability subscription isolation probe.
excludeDefaultComponents: true
inheritCustomizations: false
mainAgent: true
subagent: false
commandExecutionPolicy: off
mcpServers: []
skills: []
plugins: []
tools: []
---

This agent has no native tools. Do not invoke a model or a tool during initialization.
"""

# These files are a deliberately small closed set.  The keys document the
# controls Forge expects; runtime admission additionally requires a separately
# observed installed-client report, so unsupported keys cannot become proof.
_SETTINGS: dict[str, object] = {
    "useG1Credits": False,
    "plugins": {"enabled": False},
    "skills": {"enabled": False},
    "subagents": {"enabled": False},
    "customizations": {"inherit": False},
    "project": {"inherit": False},
    "hooks": {"enabled": False},
    "telemetry": {"enabled": False},
    "mcpServers": [],
}
_EMPTY = "{}\n"


class AntigravityIsolationError(ValueError):
    """The candidate home is not exclusively Forge-managed."""


def antigravity_settings() -> dict[str, object]:
    """Return a fresh exact policy payload (callers may not mutate globals)."""
    return cast(dict[str, object], json.loads(json.dumps(_SETTINGS, sort_keys=True)))


def antigravity_launch_environment(home: str | Path) -> dict[str, str]:
    """Build the child environment from scratch; never inherit credentials."""
    value = _absolute_directory(home)
    # HOME plus the explicit config root cover both conventional resolution
    # paths.  No PATH is inherited: argv[0] is an absolute verified executable.
    return {"HOME": value, "ANTIGRAVITY_HOME": value, "ANTIGRAVITY_CONFIG_DIR": value}


def antigravity_launch_argv(executable: str, model: str, effort: str) -> tuple[str, ...]:
    """The closed headless stream-json invocation; no fallback knobs exist."""
    if not all(
        type(value) is str and value and "\0" not in value for value in (executable, model, effort)
    ):
        raise AntigravityIsolationError("Antigravity launch fields are invalid")
    if not Path(executable).is_absolute():
        raise AntigravityIsolationError("Antigravity executable must be absolute")
    return (
        executable,
        "--agent",
        ANTIGRAVITY_ISOLATION_AGENT,
        "--model",
        model,
        "--effort",
        effort,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--sandbox",
        "off",
        "--project",
        "none",
        "--slash-commands",
        "off",
    )


class AntigravityIsolationHome:
    """One exclusive managed home under an operator-selected base directory."""

    def __init__(self, managed_base: str | Path, attempt_id: UUID) -> None:
        base = Path(managed_base)
        if not base.is_absolute() or not isinstance(attempt_id, UUID):
            raise AntigravityIsolationError("managed base and attempt are required")
        self.path = base / f".forge-antigravity-{attempt_id}"
        self._owned = False

    @property
    def expected_files(self) -> dict[Path, bytes]:
        return {
            self.path / "agents" / f"{ANTIGRAVITY_ISOLATION_AGENT}.md": _AGENT.encode(),
            self.path / "mcp.json": _EMPTY.encode(),
            self.path / "hooks.json": _EMPTY.encode(),
            self.path / "settings.json": (
                json.dumps(_SETTINGS, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
        }

    def prepare(self) -> dict[str, str]:
        base = self.path.parent
        if not _safe_directory(base):
            raise AntigravityIsolationError("managed base must already be a real directory")
        if os.path.lexists(self.path):
            raise AntigravityIsolationError("managed Antigravity home already exists")
        self.path.mkdir(mode=0o700)
        try:
            agents = self.path / "agents"
            agents.mkdir(mode=0o700)
            for target, payload in self.expected_files.items():
                _write_new_regular(target, payload)
            self._owned = True
            self.validate()
            return antigravity_launch_environment(self.path)
        except BaseException:
            self.cleanup()
            raise

    def validate(self) -> None:
        if (
            not self._owned
            or not _safe_directory(self.path)
            or not _safe_directory(self.path / "agents")
        ):
            raise AntigravityIsolationError("Antigravity home is not managed")
        expected = self.expected_files
        discovered: set[Path] = set()
        for root, directories, files in os.walk(self.path, followlinks=False):
            root_path = Path(root)
            for name in directories:
                candidate = root_path / name
                if candidate.is_symlink() or not _safe_directory(candidate):
                    raise AntigravityIsolationError(
                        "Antigravity home contains a link or unsafe directory"
                    )
            for name in files:
                candidate = root_path / name
                if candidate.is_symlink() or not _safe_regular(candidate):
                    raise AntigravityIsolationError(
                        "Antigravity home contains a link or unsafe file"
                    )
                discovered.add(candidate)
        if discovered != set(expected):
            raise AntigravityIsolationError("Antigravity home contents drifted")
        for target, payload in expected.items():
            try:
                if target.read_bytes() != payload:
                    raise AntigravityIsolationError("Antigravity home contents drifted")
            except OSError as exc:
                raise AntigravityIsolationError("Antigravity home is unreadable") from exc

    def cleanup(self) -> None:
        if not self._owned:
            return
        # Delete only the path we created, after revalidating its no-link tree.
        self.validate()
        shutil.rmtree(self.path)
        self._owned = False


def _absolute_directory(value: str | Path) -> str:
    path = Path(value)
    if not path.is_absolute() or not _safe_directory(path):
        raise AntigravityIsolationError("Antigravity home must be an existing real directory")
    return str(path.resolve(strict=True))


def _safe_directory(path: Path) -> bool:
    try:
        info = path.lstat()
        return (
            stat.S_ISDIR(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and not _reparse_point(info)
        )
    except OSError:
        return False


def _safe_regular(path: Path) -> bool:
    try:
        info = path.lstat()
        return (
            stat.S_ISREG(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and not _reparse_point(info)
        )
    except OSError:
        return False


def _reparse_point(info: os.stat_result) -> bool:
    """Treat Windows junctions/reparse points like symlinks."""
    return bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _write_new_regular(path: Path, payload: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
    except OSError as exc:
        raise AntigravityIsolationError("could not create managed Antigravity file") from exc


__all__ = [
    "ANTIGRAVITY_ISOLATION_AGENT",
    "AntigravityIsolationError",
    "AntigravityIsolationHome",
    "antigravity_launch_argv",
    "antigravity_launch_environment",
    "antigravity_settings",
]
