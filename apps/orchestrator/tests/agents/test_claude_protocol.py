import asyncio
import json

import pytest
from forge.agents.claude_protocol import ClaudeStreamCodec
from forge.agents.subscription_protocol import ProtocolError


async def _handler(call):
    return {"tool": call.name, "status": "succeeded"}


def _codec(handler=_handler):
    codec = ClaudeStreamCodec("thread", "turn", handler, frozenset({"repository.read_file"}))
    asyncio.run(codec.receive(_initialize()))
    return codec


def _initialize():
    return json.dumps(
        {
            "type": "control_request",
            "request_id": "init",
            "request": {
                "subtype": "mcp_message",
                "server_name": "forge",
                "message": {
                    "jsonrpc": "2.0",
                    "id": "init",
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26"},
                },
            },
        }
    )


def _frame(call_id="native-1", request_id="req-1", arguments=None):
    return json.dumps(
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "mcp_message",
                "server_name": "forge",
                "message": {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {
                        "name": "repository.read_file",
                        "arguments": arguments or {"path": "README.md"},
                    },
                },
            },
        }
    )


def test_native_mcp_identity_is_used_for_forge_callback_and_response():
    codec = _codec()
    response = asyncio.run(codec.receive(_frame()))
    assert response["response"]["request_id"] == "req-1"
    assert response["response"]["response"]["mcp_response"]["id"] == "native-1"


def test_subscription_quota_warning_is_informational_and_keeps_tool_exchange_open():
    codec = _codec()
    notification = {
        "type": "rate_limit_event",
        "session_id": "thread",
        "uuid": "00000000-0000-0000-0000-000000000001",
        "rate_limit_info": {
            "status": "allowed_warning",
            "rateLimitType": "five_hour",
            "resetsAt": 1789300800,
            "utilization": 0.8,
        },
    }
    assert asyncio.run(codec.receive(json.dumps(notification))) is None
    assert codec.terminal is None
    response = asyncio.run(codec.receive(_frame()))
    assert response["response"]["response"]["mcp_response"]["id"] == "native-1"


def test_mcp_handshake_and_tool_listing_only_expose_registered_forge_tools():
    codec = ClaudeStreamCodec("thread", "turn", _handler, frozenset({"repository.read_file"}))
    initialize = json.dumps(
        {
            "type": "control_request",
            "request_id": "init",
            "request": {
                "subtype": "mcp_message",
                "server_name": "forge",
                "message": {
                    "jsonrpc": "2.0",
                    "id": "one",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "claude", "version": "2"},
                    },
                },
            },
        }
    )
    listing = json.dumps(
        {
            "type": "control_request",
            "request_id": "list",
            "request": {
                "subtype": "mcp_message",
                "server_name": "forge",
                "message": {"jsonrpc": "2.0", "id": "two", "method": "tools/list", "params": {}},
            },
        }
    )
    assert (
        asyncio.run(codec.receive(initialize))["response"]["response"]["mcp_response"]["result"][
            "serverInfo"
        ]["name"]
        == "forge"
    )
    tools = asyncio.run(codec.receive(listing))["response"]["response"]["mcp_response"]["result"][
        "tools"
    ]
    assert tools == [
        {
            "name": "repository.read_file",
            "description": "Forge controlled repository.read_file",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]


def test_duplicate_control_or_conflicting_native_call_is_rejected():
    codec = _codec()
    asyncio.run(codec.receive(_frame()))
    with pytest.raises(ProtocolError):
        asyncio.run(codec.receive(_frame()))
    with pytest.raises(ProtocolError):
        asyncio.run(
            codec.receive(
                _frame(call_id="native-1", request_id="req-2", arguments={"path": "other"})
            )
        )


def test_foreign_and_terminal_frames_are_strict():
    codec = _codec()
    with pytest.raises(ProtocolError):
        asyncio.run(
            codec.receive(
                json.dumps(
                    {
                        "type": "control_request",
                        "request_id": "r",
                        "request": {
                            "subtype": "mcp_message",
                            "server_name": "other",
                            "message": {},
                        },
                    }
                )
            )
        )
    asyncio.run(
        codec.receive(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "thread",
                    "subtype": "success",
                    "structured_output": {"decision": {}},
                    "is_error": False,
                }
            )
        )
    )
    with pytest.raises(ProtocolError):
        asyncio.run(codec.receive(json.dumps({"type": "result", "structured_output": {}})))


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "foreign"},
        {"session_id": None},
        {"is_error": 0},
        {"subtype": "error_during_execution"},
        {"structured_output": None},
        {"structured_output": []},
    ],
)
def test_terminal_requires_matching_session_and_typed_success(change):
    codec = _codec()
    frame = {
        "type": "result",
        "session_id": "thread",
        "subtype": "success",
        "structured_output": {"decision": {}},
        "is_error": False,
    }
    frame.update(change)
    with pytest.raises(ProtocolError):
        asyncio.run(codec.receive(json.dumps(frame)))
    assert codec.terminal is None


def test_tool_callback_after_terminal_is_not_dispatched():
    calls = []

    async def handler(call):
        calls.append(call)
        return {"status": "succeeded"}

    codec = _codec(handler)
    asyncio.run(
        codec.receive(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "thread",
                    "subtype": "success",
                    "structured_output": {"decision": {}},
                    "is_error": False,
                }
            )
        )
    )
    with pytest.raises(ProtocolError):
        asyncio.run(codec.receive(_frame()))
    assert calls == []


@pytest.mark.parametrize("subtype", ["success", "error_during_execution"])
def test_matching_error_terminal_is_retained_as_failure_evidence(subtype):
    codec = _codec()
    asyncio.run(
        codec.receive(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "thread",
                    "subtype": subtype,
                    "is_error": True,
                    "errors": ["provider failure"],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                }
            )
        )
    )
    assert codec.terminal["is_error"] is True
    assert codec.terminal["usage"]["input_tokens"] == 3
