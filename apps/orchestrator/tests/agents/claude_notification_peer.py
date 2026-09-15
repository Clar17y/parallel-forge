"""Offline Claude stream-json peer; no provider imports or network access."""

import json
import sys
import time


def receive():
    line = sys.stdin.readline()
    assert line, "unexpected input EOF"
    return json.loads(line)


def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


scenario = sys.argv[1]
reset = int(sys.argv[2])
initialize = receive()
assert initialize["request"] == {
    "subtype": "initialize",
    "hooks": None,
    "sdkMcpServers": ["forge"],
    "skills": [],
    "title": "Forge bounded review",
}
send(
    {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": "forge_initialize", "response": {}},
    }
)
settings = receive()
assert settings["request"] == {"subtype": "get_settings"}
send(
    {
        "type": "control_request",
        "request_id": "mcp-init",
        "request": {
            "subtype": "mcp_message",
            "server_name": "forge",
            "message": {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
        },
    }
)
assert receive()["response"]["request_id"] == "mcp-init"
send(
    {
        "type": "control_request",
        "request_id": "mcp-ready",
        "request": {
            "subtype": "mcp_message",
            "server_name": "forge",
            "message": {"jsonrpc": "2.0", "method": "notifications/initialized"},
        },
    }
)
assert receive()["response"]["request_id"] == "mcp-ready"
send(
    {
        "type": "control_request",
        "request_id": "mcp-list",
        "request": {
            "subtype": "mcp_message",
            "server_name": "forge",
            "message": {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        },
    }
)
assert receive()["response"]["request_id"] == "mcp-list"
managed = {
    "allowManagedHooksOnly": True,
    "disableClaudeAiConnectors": True,
    "disableCommandPluginSources": True,
    "syncClaudeAiPlugins": False,
    "syncClaudeAiSkills": False,
}
send(
    {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "forge_settings",
            "response": {
                "applied": {
                    "advisor": None,
                    "effort": "medium",
                    "model": "claude-test",
                    "ultracode": False,
                },
                "effective": managed,
                "sources": [{"source": "policySettings", "settings": managed}],
            },
        },
    }
)
allowed = next(value for value in sys.argv if value.startswith("--allowed-tools="))
tools = ["StructuredOutput"]
if allowed != "--allowed-tools=":
    tools.insert(0, "mcp__forge__repository_read_file")
send(
    {
        "type": "system",
        "subtype": "init",
        "session_id": sys.argv[sys.argv.index("--session-id") + 1],
        "claude_code_version": "2.1.263",
        "model": "claude-test",
        "permissionMode": "dontAsk",
        "tools": tools,
        "slash_commands": [],
        "skills": [],
        "plugins": [],
        "mcp_servers": [{"name": "forge", "status": "connected"}],
    }
)
user = receive()
assert user["type"] == "user"
session = user["session_id"]
assert session == sys.argv[sys.argv.index("--session-id") + 1]


def control(ident, message):
    send(
        {
            "type": "control_request",
            "request_id": ident,
            "request": {"subtype": "mcp_message", "server_name": "forge", "message": message},
        }
    )


def tool():
    control(
        "read",
        {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {
                "name": "repository.read_file",
                "arguments": {"path": "README.md"},
                "_meta": {"claudecode/toolUseId": "forge-call-1", "progressToken": 1},
            },
        },
    )


if scenario == "partial_tool":
    tool()
    assert receive()["response"]["request_id"] == "read"

if scenario != "result_only":
    status = (
        "allowed_warning"
        if scenario in {"warning", "throttled", "authentication", "outage", "unsupported"}
        else "rejected"
    )
    if scenario == "allowed":
        status = "allowed"
    kind = (
        "seven_day_sonnet"
        if scenario == "unrelated"
        else "overage"
        if scenario == "overage"
        else "five_hour"
    )
    info = {
        "status": status,
        "rateLimitType": kind,
        "resetsAt": reset,
        "utilization": 1.0,
        "overageStatus": "rejected",
        "overageDisabledReason": "out_of_credits",
        "canUserPurchaseCredits": True,
        "unknownFutureField": "fixture-secret-not-to-persist",
    }
    frame = {
        "type": "rate_limit_event",
        "session_id": session,
        "uuid": "00000000-0000-0000-0000-000000000001",
        "rate_limit_info": info,
    }
    if scenario == "foreign":
        frame["session_id"] = "foreign"
    if scenario == "missing_identity":
        del frame["uuid"]
    if scenario == "bad_status":
        info["status"] = True
    send(frame)
    if scenario == "sparse_after_quota":
        send({**frame, "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}})
    if scenario == "eof":
        raise SystemExit(0)
    if scenario == "hang":
        time.sleep(30)
    if scenario == "tool_after_quota":
        tool()
        receive()

if scenario in {"allowed", "warning", "overage", "contradiction"}:
    decision = {
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
    send(
        {
            "type": "result",
            "session_id": session,
            "subtype": "success",
            "is_error": False,
            "structured_output": {"decision": decision},
        }
    )
else:
    status, message = {
        "throttled": (429, "API Error: Rate limit reached"),
        "authentication": (401, "Invalid API key"),
        "outage": (503, "Service unavailable"),
        "unsupported": (None, "Model not found"),
        "generic_429": (429, "API Error: Rate limit reached"),
        "quota_then_authentication": (401, "Invalid API key"),
    }.get(scenario, (429, "You've hit your session limit"))
    terminal = {
        "type": "result",
        "session_id": session,
        "subtype": "success",
        "is_error": True,
        "result": message,
        "api_error_status": status,
    }
    if scenario == "malformed_usage":
        terminal["usage"] = {"input_tokens": -1, "output_tokens": 0}
    send(terminal)
