"""Offline ACP peer with the pinned client's separate, real MCP child transport."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def emit(frame):
    print(json.dumps(frame, separators=(",", ":")), flush=True)


def read():
    return json.loads(sys.stdin.readline())


def rpc(ident, result):
    emit({"jsonrpc": "2.0", "id": ident, "result": result})


scenario = sys.argv[1]
model = sys.argv[sys.argv.index("--model") + 1]
assert "--tools" not in sys.argv and "--billing-overage-strategy" not in sys.argv
assert "--extensions=none" in sys.argv and "--allowed-mcp-server-names=forge" in sys.argv
# Assert materialization through the real child environment in every composed
# scenario. This fake client does not establish official-client conformance.
settings_path = Path(os.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
assert settings_path.parent == Path.cwd()
settings = json.loads(settings_path.read_text(encoding="utf-8"))
assert settings["tools"]["core"] == [] and settings["billing"]["overageStrategy"] == "never"
assert settings["hooksConfig"]["enabled"] is False and settings["skills"]["enabled"] is False
assert settings["security"]["auth"]["selectedType"] == "oauth-personal"
assert Path(os.environ["GEMINI_CLI_HOME"]).is_dir()
assert json.loads(Path(os.environ["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"]).read_text()) == {}
assert (Path.cwd() / ".env").read_bytes() == b""
assert (Path.cwd() / ".gemini/.env").read_bytes() == b""
initialize = read()
assert initialize["method"] == "initialize"
assert initialize["params"]["protocolVersion"] == 1
assert not initialize["params"]["clientCapabilities"]["terminal"]
if scenario.startswith("rpc_"):
    emit(
        {
            "jsonrpc": "2.0",
            "id": initialize["id"],
            "error": {
                "code": int(scenario.removeprefix("rpc_")),
                "message": "fake-sensitive-text quota exhausted; reset tomorrow",
            },
        }
    )
    time.sleep(30)
    raise SystemExit(0)
if scenario == "early_eof":
    raise SystemExit(0)
rpc(
    initialize["id"],
    {
        "protocolVersion": 1,
        "agentInfo": {
            "name": "gemini-cli",
            "version": "0.59.1" if scenario == "wrong_version" else "0.59.0",
        },
    },
)
new = read()
assert new["method"] == "session/new"
assert Path(new["params"]["cwd"]) == Path.cwd()
descriptor = new["params"]["mcpServers"][0]
assert set(descriptor) == {"name", "command", "args", "env"}
environment = dict(os.environ)
environment.update({item["name"]: item["value"] for item in descriptor["env"]})
proxy = subprocess.Popen(
    [descriptor["command"], *descriptor["args"]],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=environment,
)


def mcp(frame, *, reply=True):
    proxy.stdin.write(json.dumps(frame) + "\n")
    proxy.stdin.flush()
    return json.loads(proxy.stdout.readline()) if reply else None


mcp(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "fake-gemini", "version": "0.59.0"},
        },
    }
)
mcp({"jsonrpc": "2.0", "method": "notifications/initialized"}, reply=False)
listing = mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
session_id = "fake-gemini-session"
rpc(
    new["id"],
    {
        "sessionId": session_id,
        "models": {
            "currentModelId": "unapproved-model" if scenario == "wrong_model" else model,
            "availableModels": [],
        },
    },
)
prompt = read()
assert prompt["method"] == "session/prompt" and prompt["params"]["sessionId"] == session_id
system_prompt = Path(os.environ["GEMINI_SYSTEM_MD"]).read_text(encoding="utf-8")
assert system_prompt
if not scenario.startswith("production"):
    assert "Return structured JSON only." in system_prompt
context = json.loads(prompt["params"]["prompt"][0]["text"])
assert "Return structured JSON only." not in json.dumps(context)
assert system_prompt not in json.dumps(context)


def update(value):
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": value},
        }
    )


def start(ident):
    update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": ident,
            "status": "in_progress",
            "title": "Forge controlled operation",
            "kind": "other",
            "content": [],
            "locations": [],
        }
    )


def completed(ident, response):
    update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": ident,
            "status": "completed",
            "kind": "other",
            "content": [{"type": "content", "content": response["result"]["content"][0]}],
        }
    )


if scenario in ("tool", "two_tools"):
    assert {tool["name"] for tool in listing["result"]["tools"]} == {
        "repository.read_file",
        "forge_execute_receipt",
    }
    count = 2 if scenario == "two_tools" else 1
elif scenario in ("production_active_stop", "production_active_check_stop"):
    count = 3 if scenario == "production_active_check_stop" else 2
else:
    count = 0
previous_receipt = None
for operation in range(count):
    name = "repository.read_file"
    arguments = {"path": "src/example.py"}
    if scenario in ("production_active_stop", "production_active_check_stop"):
        arguments = {"path": "src/counter.py"}
        if operation == 1:
            name = "repository.write_file"
            assert previous_receipt is not None
            arguments["content"] = previous_receipt["metadata"]["content"].replace(
                "value + 2", "value + 1"
            )
        elif operation == 2:
            # Let the test observe the physical provider/MCP identities, then
            # wait on the actual check's barrier before requesting its stop.
            update(
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {
                        "type": "text",
                        "text": json.dumps({"fixture_active_stop": True, "mcp_pid": proxy.pid}),
                    },
                }
            )
            name = "build.run_named_check"
            arguments = {"command_name": "slow-unit"}
    start(f"native-proposal-{operation}")
    proposal = mcp(
        {
            "jsonrpc": "2.0",
            "id": f"prepare-{operation}",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    completed(f"native-proposal-{operation}", proposal)
    nonce = json.loads(proposal["result"]["content"][0]["text"])["nonce"]
    # ACP and MCP use independent pipes; an early execute must wait for Forge
    # to consume the proposal's ACP echo and can only receive not_ready.
    for retry in range(8):
        native = f"native-execute-{operation}-{retry}"
        start(native)
        receipt = mcp(
            {
                "jsonrpc": "2.0",
                "id": f"execute-{operation}-{retry}",
                "method": "tools/call",
                "params": {"name": "forge_execute_receipt", "arguments": {"token": nonce}},
            }
        )
        completed(native, receipt)
        if json.loads(receipt["result"]["content"][0]["text"])["receipt"]["status"] != "not_ready":
            break
        time.sleep(0.01)
    previous_receipt = json.loads(receipt["result"]["content"][0]["text"])["receipt"]
    assert previous_receipt["status"] == "succeeded"
if scenario == "production_active_stop":
    # An observed protocol boundary follows both durable controlled receipts.
    # The parent test records this real descendant's process identity before
    # requesting an operator stop. No final provider result is emitted.
    update(
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {
                "type": "text",
                "text": json.dumps({"fixture_active_stop": True, "mcp_pid": proxy.pid}),
            },
        }
    )
    sys.stdin.readline()
    raise SystemExit(0)
if scenario == "forbidden_tool":
    mcp(
        {
            "jsonrpc": "2.0",
            "id": 50,
            "method": "tools/call",
            "params": {
                "name": "shell.run",
                "arguments": {"command": "untrusted"},
            },
        }
    )
if scenario == "native_permission":
    emit(
        {
            "jsonrpc": "2.0",
            "id": 70,
            "method": "session/request_permission",
            "params": {"sessionId": session_id},
        }
    )
    assert read()["result"]["outcome"]["outcome"] == "cancelled"
    time.sleep(30)
if scenario == "wrong_session":
    session_id = "foreign-session"
if scenario == "no_tools":
    assert listing["result"]["tools"] == []
decision = json.dumps(
    {"kind": "handoff", "status": "blocked", "summary": "Fake result with preserved work"}
)
for chunk in (decision[:20], decision[20:]):
    update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": chunk}})
terminal = {
    "stopReason": "end_turn",
    "_meta": {
        "quota": {
            "token_count": {"input_tokens": 10, "output_tokens": 4},
            "model_usage": [
                {"model": model, "token_count": {"input_tokens": 10, "output_tokens": 4}}
            ],
        }
    },
}
if scenario == "unknown_usage":
    terminal.pop("_meta")
if scenario == "empty_usage":
    terminal["_meta"]["quota"] = {
        "token_count": {"input_tokens": 0, "output_tokens": 0},
        "model_usage": [],
    }
rpc(prompt["id"], terminal)
time.sleep(30)  # Forge owns stopping this client and its MCP descendant.
