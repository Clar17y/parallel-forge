"""Muse permission hook grants only the private run's selected Forge callbacks."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from forge.agents.muse_approval import permission_hook_settings


def _invoke(script: Path, policy: Path, payload: object) -> str:
    result = subprocess.run(
        [sys.executable, str(script), str(policy)], input=json.dumps(payload), text=True,
        capture_output=True, check=False, timeout=5, cwd=policy.parent,
        env={"SystemRoot": __import__("os").environ.get("SystemRoot", ""),
             "SystemDrive": __import__("os").environ.get("SystemDrive", ""),
             "ProgramData": __import__("os").environ.get("ProgramData", "")},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["decision"]["behavior"]


def test_permission_hook_exact_tool_and_cwd(tmp_path: Path) -> None:
    name = "forge_" + "a" * 32
    private_cwd = tmp_path / "private"
    private_cwd.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    policy = private_cwd / "approval-policy.json"
    settings = permission_hook_settings(policy, private_cwd, name, ("read_file", "write_file", "named_check"))
    hook = settings["PermissionRequest"][0]
    assert hook["matcher"].startswith("^") and hook["matcher"].endswith("$")
    script = private_cwd / "muse_approval_hook.py"
    assert script.is_file()
    for tool in ("read_file", "write_file", "named_check"):
        assert _invoke(script, policy, {
            "hook_event_name": "PermissionRequest", "tool_name": f"mcp__{name}__{tool}",
            "cwd": str(private_cwd),
        }) == "allow"
    for tool in ("write_file", "named_check"):
        assert _invoke(script, policy, {
            "hook_event_name": "PermissionRequest", "tool_name": f"mcp__{name}__{tool}",
            "cwd": str(private_cwd), "tool_input": {
                "content": "x" * 120000, "tool_name": "mcp__other__write_file",
            },
        }) == "allow"
    for payload in (
        {"hook_event_name": "PermissionRequest", "tool_name": f"mcp__{name}__write_files", "cwd": str(private_cwd)},
        {"hook_event_name": "PermissionRequest", "tool_name": "mcp__other__write_file", "cwd": str(private_cwd)},
        {"hook_event_name": "PreToolUse", "tool_name": f"mcp__{name}__write_file", "cwd": str(private_cwd)},
        {"hook_event_name": "PermissionRequest", "tool_name": f"mcp__{name}__write_file", "cwd": str(repository)},
        {"hook_event_name": "PermissionRequest", "tool_name": "mcp__other__write_file", "cwd": str(private_cwd),
         "tool_input": {"tool_name": f"mcp__{name}__write_file"}},
        {"hook_event_name": "PermissionRequest", "tool_name": f"mcp__{name}__write_file"},
        [],
    ):
        assert _invoke(script, policy, payload) == "deny"


def test_permission_hook_rejects_oversize_and_invalid_policy(tmp_path: Path) -> None:
    name = "forge_" + "a" * 32
    policy = tmp_path / "approval-policy.json"
    permission_hook_settings(policy, tmp_path, name, ("read_file",))
    script = tmp_path / "muse_approval_hook.py"
    payload = {"hook_event_name": "PermissionRequest", "tool_name": f"mcp__{name}__read_file", "cwd": str(tmp_path)}
    assert _invoke(script, policy, {**payload, "padding": "x" * (1024 * 1024 + 1)}) == "deny"
    malformed = subprocess.run(
        [sys.executable, str(script), str(policy)], input="{", text=True,
        capture_output=True, check=False, timeout=5, cwd=tmp_path,
    )
    assert malformed.returncode == 0
    assert json.loads(malformed.stdout)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    policy.write_text("{}", encoding="utf-8")
    assert _invoke(script, policy, payload) == "deny"
    with pytest.raises(ValueError):
        permission_hook_settings(tmp_path / "new.json", tmp_path, "forge", ("read_file|.*",))


@pytest.mark.skipif(os.name != "nt", reason="Windows hook shell quoting")
def test_permission_hook_command_runs_from_private_path_with_spaces(tmp_path: Path) -> None:
    private = tmp_path / "private home"
    private.mkdir()
    workspace = tmp_path / "managed workspace"
    workspace.mkdir()
    policy = private / "approval-policy.json"
    settings = permission_hook_settings(policy, private, "forge", ("read_file",))
    command = settings["PermissionRequest"][0]["hooks"][0]["command"]
    request = {
        "hook_event_name": "PermissionRequest", "tool_name": "mcp__forge__read_file",
        "cwd": str(private),
    }
    result = subprocess.run(
        command, shell=True, input=json.dumps(request), text=True,
        capture_output=True, check=False, timeout=5, cwd=private,
        env={key: os.environ[key] for key in ("SystemRoot", "SystemDrive", "ProgramData") if key in os.environ},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
