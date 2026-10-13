"""Private per-run Muse permission hook for exactly selected Forge MCP tools.

This file is also executed by an absolute path with a filtered environment.
Keep the command path dependency-free: no Forge imports are needed at hook time.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_MAX_INPUT = 1024 * 1024
_MAX_POLICY = 4 * 1024
_SERVER = re.compile(r"forge(?:_[0-9a-f]{32})?\Z")
_TOOL = re.compile(r"[a-z][a-z0-9_.]*\Z")
_HOOK_SOURCE = Path(__file__).read_bytes()


def permission_hook_settings(
    policy_path: Path, process_cwd: Path, server: str, tools: tuple[str, ...],
) -> dict[str, object]:
    """Write one private allowlist and return an exact, per-run Muse hook."""
    if not policy_path.is_absolute() or not process_cwd.is_absolute():
        raise ValueError("Muse hook paths must be absolute")
    if _SERVER.fullmatch(server) is None or not tools or len(tools) > 64:
        raise ValueError("invalid Muse hook server or toolset")
    if len(set(tools)) != len(tools) or any(_TOOL.fullmatch(tool) is None for tool in tools):
        raise ValueError("invalid Muse hook toolset")
    names = tuple("mcp__" + server + "__" + tool for tool in tools)
    if any(len(name) > 128 for name in names):
        raise ValueError("Muse hook tool name is too long")
    helper_path = policy_path.with_name("muse_approval_hook.py")
    argv = (sys.executable, str(helper_path), str(policy_path))
    if os.name == "nt" and any(re.search(r"[%!^&|<>()\r\n]", part) for part in argv):
        # Muse receives a command string. Reject cmd expansion/metacharacters
        # rather than depending on Windows shell interpretation of those paths.
        raise ValueError("unsafe Muse hook command path")
    policy = {"event": "PermissionRequest", "cwd": str(process_cwd), "tools": names}
    policy_text = json.dumps(policy, separators=(",", ":"))
    if len(policy_text.encode("utf-8")) > _MAX_POLICY:
        raise ValueError("Muse hook policy is too large")
    with policy_path.open("x", encoding="utf-8") as stream:
        stream.write(policy_text)
    # Runtime agents may edit repository source. Execute a private per-attempt
    # copy so later repository writes cannot change an already-running grant.
    with helper_path.open("xb") as stream:
        stream.write(_HOOK_SOURCE)
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    matcher = "^(?:" + "|".join(re.escape(name) for name in names) + ")$"
    return {
        "PermissionRequest": [{
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command}],
        }],
    }


def _canonical_path(path: str) -> str:
    if not os.path.isabs(path) or "\x00" in path:
        return ""
    return os.path.normcase(os.path.normpath(path))


def _decision(policy_path: Path, raw: bytes) -> str:
    if len(raw) > _MAX_INPUT or not policy_path.is_absolute():
        return "deny"
    try:
        if policy_path.is_symlink() or policy_path.stat().st_size > _MAX_POLICY:
            return "deny"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        request = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "deny"
    if not isinstance(policy, dict) or not isinstance(request, dict):
        return "deny"
    event, tool, cwd = request.get("hook_event_name"), request.get("tool_name"), request.get("cwd")
    names = policy.get("tools")
    expected_cwd = policy.get("cwd")
    if (
        event != "PermissionRequest" or policy.get("event") != "PermissionRequest"
        or not isinstance(tool, str) or not isinstance(cwd, str)
        or not isinstance(expected_cwd, str)
        or not isinstance(names, list) or not names or len(names) > 64
        or any(not isinstance(name, str) or len(name) > 128 for name in names)
        or _canonical_path(cwd) != _canonical_path(expected_cwd)
        or not _canonical_path(cwd)
    ):
        return "deny"
    return "allow" if tool in names else "deny"


def main() -> int:
    behavior = "deny"
    if len(sys.argv) == 2:
        behavior = _decision(Path(sys.argv[1]), sys.stdin.buffer.read(_MAX_INPUT + 1))
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": behavior},
        },
    }
    sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
