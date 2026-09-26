import asyncio
import hashlib
import json

import pytest
from forge.agents.claude_protocol import ClaudeStreamCodec
from forge.agents.subscription_protocol import ProtocolError


async def _handler(call):
    return {"tool": call.name, "status": "succeeded"}


def _codec(handler=_handler):
    codec = ClaudeStreamCodec("thread", "turn", handler, frozenset({"repository.read_file"}))
    asyncio.run(codec.receive(_initialize()))
    asyncio.run(
        codec.receive(
            json.dumps(
                {
                    "type": "control_request",
                    "request_id": "ready",
                    "request": {
                        "subtype": "mcp_message",
                        "server_name": "forge",
                        "message": {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    },
                }
            )
        )
    )
    asyncio.run(
        codec.receive(
            json.dumps(
                {
                    "type": "control_request",
                    "request_id": "list",
                    "request": {
                        "subtype": "mcp_message",
                        "server_name": "forge",
                        "message": {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        },
                    },
                }
            )
        )
    )
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


def _frame(call_id="native-1", request_id="req-1", arguments=None, metadata=None):
    params = {
        "name": "repository.read_file",
        "arguments": arguments or {"path": "README.md"},
    }
    if metadata is not None:
        params["_meta"] = metadata
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
                    "params": params,
                },
            },
        }
    )


def test_native_mcp_identity_is_used_for_forge_callback_and_response():
    codec = _codec()
    metadata = {"claudecode/toolUseId": "provider-call-1", "progressToken": 2}
    response = asyncio.run(codec.receive(_frame(metadata=metadata)))
    assert response["response"]["request_id"] == "req-1"
    assert response["response"]["response"]["mcp_response"]["id"] == "native-1"


def test_official_client_tool_metadata_is_bound_but_not_forwarded() -> None:
    calls = []

    async def handler(call):
        calls.append(call)
        return {"status": "succeeded"}

    codec = _codec(handler)
    metadata = {"claudecode/toolUseId": "provider-call-1", "progressToken": 2}
    response = asyncio.run(codec.receive(_frame(metadata=metadata)))

    assert codec.tool_use_id_for(calls[0].call_key) == "provider-call-1"
    assert calls[0].call_key == hashlib.sha256(b'"native-1"\0provider-call-1').hexdigest()
    assert response["response"]["response"]["mcp_response"]["id"] == "native-1"
    with pytest.raises(ProtocolError):
        asyncio.run(
            codec.receive(
                _frame(
                    request_id="req-2",
                    metadata={"claudecode/toolUseId": "provider-call-2", "progressToken": 2},
                )
            )
        )


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
    response = asyncio.run(
        codec.receive(
            _frame(metadata={"claudecode/toolUseId": "provider-call-1", "progressToken": 2})
        )
    )
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
    ready = json.dumps(
        {
            "type": "control_request",
            "request_id": "ready",
            "request": {
                "subtype": "mcp_message",
                "server_name": "forge",
                "message": {"jsonrpc": "2.0", "method": "notifications/initialized"},
            },
        }
    )
    asyncio.run(codec.receive(ready))
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


def test_only_one_post_handshake_initialized_notification_is_accepted():
    codec = ClaudeStreamCodec("thread", "turn", _handler, frozenset({"repository.read_file"}))
    asyncio.run(codec.receive(_initialize()))
    notification = json.dumps(
        {
            "type": "control_request",
            "request_id": "ready",
            "request": {
                "subtype": "mcp_message",
                "server_name": "forge",
                "message": {"jsonrpc": "2.0", "method": "notifications/initialized"},
            },
        }
    )
    assert asyncio.run(codec.receive(notification))["response"]["request_id"] == "ready"
    with pytest.raises(ProtocolError):
        asyncio.run(codec.receive(notification))
    with pytest.raises(ProtocolError):
        asyncio.run(
            ClaudeStreamCodec(
                "thread", "turn", _handler, frozenset({"repository.read_file"})
            ).receive(notification)
        )


def test_duplicate_control_or_conflicting_native_call_is_rejected():
    codec = _codec()
    metadata = {"claudecode/toolUseId": "provider-call-1", "progressToken": 2}
    asyncio.run(codec.receive(_frame(metadata=metadata)))
    with pytest.raises(ProtocolError):
        asyncio.run(codec.receive(_frame(metadata=metadata)))
    with pytest.raises(ProtocolError):
        asyncio.run(
            codec.receive(
                _frame(
                    call_id="native-1",
                    request_id="req-2",
                    arguments={"path": "other"},
                    metadata=metadata,
                )
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
        asyncio.run(
            codec.receive(
                _frame(metadata={"claudecode/toolUseId": "provider-call-1", "progressToken": 2})
            )
        )
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
