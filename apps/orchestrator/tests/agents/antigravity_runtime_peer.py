"""Providerless agy peer: documented stream plus an actual MCP child process."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def send(value):
    print(json.dumps(value), flush=True)


def rpc(child, ident, method, params=None):
    child.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}) + "\n"
    )
    child.stdin.flush()
    return json.loads(child.stdout.readline())


scenario = sys.argv[1]
home = Path(os.environ["HOME"])
settings = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
assert settings["useG1Credits"] is False
servers = json.loads((home / ".gemini/config/mcp_config.json").read_text())["mcpServers"]
name, config = next(iter(servers.items()))
assert settings["permissions"]["allow"] == [f"mcp({name}/*)"]
assert not any(
    key in os.environ
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
)
assert "--dangerously-skip-permissions" not in sys.argv
if scenario == "login_state":
    assert (home / ".gemini/antigravity-cli/jetski_state.pbtxt").read_text(
        encoding="utf-8"
    ) == "providerless login fixture"
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
model = sys.argv[sys.argv.index("--model") + 1]


def start_mcp():
    child = subprocess.Popen(
        [config["command"], *config["args"]],
        env={**os.environ, **config["env"]},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    if scenario == "optional_startup":
        assert rpc(child, 0, "client/optional_capability")["error"]["code"] == -32601
    assert "result" in rpc(
        child,
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "fake-agy", "version": "1"},
        },
    )
    child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    child.stdin.flush()
    assert "repository.read_file" in [
        tool["name"] for tool in rpc(child, 2, "tools/list")["result"]["tools"]
    ]
    return child


child = start_mcp() if scenario == "optional_startup" else None
send(
    {
        "event": "init",
        "conversation_id": "conversation",
        "init": {"model": model, "json_schema": schema},
    }
)
user = json.loads(sys.stdin.readline())
assert user["event"] == "user"
if child is None:
    child = start_mcp()
tool_name = "forbidden" if scenario == "bad_tool" else "repository.read_file"
receipt = rpc(child, 3, "tools/call", {"name": tool_name, "arguments": {"path": "README.md"}})
assert receipt["result"]["isError"] is False
if scenario == "cancel":
    time.sleep(60)
handoff = {
    "kind": "handoff",
    "status": "blocked",
    "summary": "done",
    "candidate_commit": None,
    "candidate_tree_digest": "",
    "changed_paths": [],
    "check_results": [],
    "evidence_receipt_ids": [],
    "residual_concerns": [],
    "scope_request_paths": [],
}
usage = {
    "input_tokens": 13,
    "output_tokens": 5,
    "thinking_tokens": 1,
    "cache_read_tokens": 2,
    "total_tokens": 18,
}
if scenario == "bad_usage":
    usage["input_tokens"] = True
result = {
    "conversation_id": "conversation",
    "status": "SUCCESS",
    "num_turns": 1,
    "duration_seconds": 1,
    "structured_output": {"decision": handoff},
    "usage": usage,
}
if scenario == "missing_usage":
    result.pop("usage")
if scenario == "429":
    result.update(status="ERROR", error="HTTP 429")
if scenario == "401":
    result.update(status="ERROR", error="authentication required (HTTP 401)")
if scenario == "provider_cancel":
    result.update(status="CANCELED")
send({"event": "result", "result": result})
sys.stdin.read()
