"""Strict Claude Agent SDK stream-json MCP control codec."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from forge.agents.subscription_protocol import (
    ProtocolError,
    ProviderToolCall,
    json_value,
    parse_json,
    tool_input_schema,
)
from forge.domain.tool import ToolName

MAX_FRAME_BYTES = 1_048_576
ToolHandler = Callable[[ProviderToolCall], Awaitable[Mapping[str, object]]]


@dataclass(slots=True)
class ClaudeStreamCodec:
    """Serve one isolated Forge MCP endpoint over SDK control envelopes."""

    thread_id: str
    turn_id: str
    handler: ToolHandler
    tools: frozenset[str]
    _requests: set[str] = field(default_factory=set, init=False)
    _calls: dict[str, tuple[str, str]] = field(default_factory=dict, init=False)
    _initialized: bool = field(default=False, init=False)
    terminal: Mapping[str, object] | None = field(default=None, init=False)

    async def receive(self, line: str) -> Mapping[str, object] | None:
        if self.terminal is not None:
            raise ProtocolError("Claude attempt is already terminal")
        if type(line) is not str or len(line.encode()) > MAX_FRAME_BYTES:
            raise ProtocolError("invalid Claude frame")
        frame = parse_json(line)
        if frame.get("type") == "control_request":
            return await self._control_request(frame)
        if frame.get("type") == "result":
            self._terminal(frame)
            return None
        if frame.get("type") == "rate_limit_event":
            self._rate_limit(frame)
            return None
        if frame.get("type") in {"system", "assistant", "user", "stream_event"}:
            return None
        raise ProtocolError("foreign Claude frame")

    def _rate_limit(self, frame: Mapping[str, object]) -> None:
        info, ident = frame.get("rate_limit_info"), frame.get("uuid")
        if (
            set(frame) != {"type", "session_id", "uuid", "rate_limit_info"}
            or frame.get("session_id") != self.thread_id
            or type(ident) is not str
            or not 1 <= len(ident) <= 128
            or not isinstance(info, Mapping)
            or info.get("status") not in ("allowed", "allowed_warning", "rejected")
            or (info.get("rateLimitType") is not None and type(info["rateLimitType"]) is not str)
        ):
            raise ProtocolError("invalid Claude quota notification")
        # Optional/unknown SDK telemetry is not authority to dispatch tools,
        # change billing, or clear a durable block. The gateway selects only
        # verified quota windows and sanitizes evidence before settlement.

    def _terminal(self, frame: Mapping[str, object]) -> None:
        permitted = {
            "type",
            "subtype",
            "structured_output",
            "session_id",
            "duration_ms",
            "duration_api_ms",
            "num_turns",
            "usage",
            "model_usage",
            "modelUsage",
            "total_cost_usd",
            "is_error",
            "stop_reason",
            "errors",
            "uuid",
            "result",
            "permission_denials",
            "api_error_status",
            "terminal_reason",
            "origin",
            "deferred_tool_use",
        }
        if (
            set(frame) - permitted
            or frame.get("session_id") != self.thread_id
            or type(frame.get("is_error")) is not bool
            or not isinstance(frame.get("subtype"), str)
        ):
            raise ProtocolError("invalid Claude terminal identity or outcome")
        origin = frame.get("origin")
        if origin is not None and (
            not isinstance(origin, Mapping) or origin.get("kind") != "human"
        ):
            raise ProtocolError("Claude result belongs to an injected turn")
        if not frame["is_error"] and (
            frame.get("subtype") != "success"
            or frame.get("terminal_reason") not in (None, "completed")
            or frame.get("deferred_tool_use") is not None
            or not isinstance(frame.get("structured_output"), Mapping)
        ):
            raise ProtocolError("invalid Claude terminal identity or outcome")
        self.terminal = frame

    async def _control_request(self, frame: Mapping[str, object]) -> Mapping[str, object]:
        if set(frame) != {"type", "request_id", "request"}:
            raise ProtocolError("invalid Claude control request")
        request_id, request = frame["request_id"], frame["request"]
        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id in self._requests
            or not isinstance(request, Mapping)
        ):
            raise ProtocolError("invalid Claude request identity")
        if len(self._requests) >= 1024:
            raise ProtocolError("too many Claude control requests")
        self._requests.add(request_id)
        if (
            request.get("subtype") != "mcp_message"
            or set(request) - {"subtype", "server_name", "message"}
            or request.get("server_name") != "forge"
            or not isinstance(request.get("message"), Mapping)
        ):
            raise ProtocolError("unsupported Claude control request")
        reply = await self._mcp(request["message"])
        return {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {"mcp_response": reply},
            },
        }

    async def _mcp(self, message: Mapping[str, object]) -> dict[str, object]:
        if message.get("jsonrpc") != "2.0" or set(message) - {"jsonrpc", "id", "method", "params"}:
            raise ProtocolError("invalid MCP message")
        method, ident, params = message.get("method"), message.get("id"), message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, Mapping):
            raise ProtocolError("invalid MCP request")
        if method.startswith("notifications/"):
            if "id" in message:
                raise ProtocolError("notification has an id")
            return {"jsonrpc": "2.0"}
        if not isinstance(ident, (str, int)) or isinstance(ident, bool):
            raise ProtocolError("MCP request requires an id")
        if method == "initialize":
            if set(params) - {"protocolVersion", "capabilities", "clientInfo"} or not isinstance(
                params.get("protocolVersion"), str
            ):
                raise ProtocolError("invalid MCP initialize")
            self._initialized = True
            return self._result(
                ident,
                {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "forge", "version": "0.2"},
                },
            )
        if not self._initialized:
            raise ProtocolError("MCP call before initialize")
        if method == "tools/list":
            if params:
                raise ProtocolError("invalid MCP tool listing")
            return self._result(ident, {"tools": self._tool_descriptions()})
        if method != "tools/call" or set(params) != {"name", "arguments"}:
            raise ProtocolError("unregistered MCP method")
        name, arguments = params["name"], params["arguments"]
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            raise ProtocolError("unregistered MCP tool")
        if name not in self.tools:
            raise ProtocolError("unregistered MCP tool")
        fingerprint, key = (
            (
                name,
                json.dumps(
                    json_value(arguments), sort_keys=True, separators=(",", ":"), allow_nan=False
                ),
            ),
            json.dumps(ident, ensure_ascii=True, separators=(",", ":")),
        )
        if (prior := self._calls.get(key)) is not None and prior != fingerprint:
            raise ProtocolError("conflicting MCP call replay")
        if len(self._calls) >= 1024:
            raise ProtocolError("too many MCP calls")
        self._calls[key] = fingerprint
        receipt = await self.handler(
            ProviderToolCall(
                call_key=key,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                name=name,
                arguments=arguments,
            )
        )
        if not isinstance(receipt, Mapping):
            raise ProtocolError("invalid Forge tool receipt")
        return self._result(
            ident,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            json_value(receipt),
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    }
                ],
                "isError": receipt.get("status") != "succeeded",
            },
        )

    def _tool_descriptions(self) -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "description": f"Forge controlled {name}",
                "inputSchema": tool_input_schema(ToolName(name)),
            }
            for name in sorted(self.tools)
        ]

    @staticmethod
    def _result(ident: str | int, result: Mapping[str, Any]) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": ident, "result": dict(result)}


__all__ = ["MAX_FRAME_BYTES", "ClaudeStreamCodec"]
