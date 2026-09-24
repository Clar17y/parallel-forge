"""A private MCP connection to the existing Forge tool broker."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping
from typing import Any, cast

from forge.agents.client_process import ClientProcessSession
from forge.agents.codex_gateway import ToolBroker
from forge.agents.gemini_gateway_mcp import GeminiMcpBridge
from forge.agents.subscription_protocol import (
    ProtocolError,
    ProviderToolCall,
    freeze_context,
    json_value,
    tool_input_schema,
)
from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.domain.tool import ToolName


class LocalCliMcp:
    def __init__(self, request: SubscriptionInvocationRequest, broker: ToolBroker | None):
        self.request, self.broker = request, broker
        self.name = "forge_" + request.attempt.attempt_id.hex
        self.bridge = GeminiMcpBridge(secrets.token_urlsafe(32), self._handle)
        self.calls = self.checks = self.metadata_calls = 0
        self.allowed = request.authorization.permitted_tools
        self._initialized = self._closed = False
        self._receipts: dict[str, tuple[ProviderToolCall, Mapping[str, object]]] = {}
        self._failure: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.bridge.start()
        self._failure = asyncio.create_task(self.bridge.wait_failed())

    def configuration(self) -> dict[str, object]:
        descriptor = self.bridge.descriptor()
        environment = cast(list[dict[str, str]], descriptor["env"])
        return {
            "mcpServers": {
                self.name: {
                    "command": descriptor["command"],
                    "args": descriptor["args"],
                    "env": {item["name"]: item["value"] for item in environment},
                }
            }
        }

    async def receive(self, session: ClientProcessSession) -> dict[str, Any] | None:
        assert self._failure is not None
        read = asyncio.create_task(session.receive())
        try:
            done, _ = await asyncio.wait((read, self._failure), return_when=asyncio.FIRST_COMPLETED)
            if self._failure in done:
                self._failure.result()
            return await read
        finally:
            if not read.done():
                read.cancel()
            await asyncio.gather(read, return_exceptions=True)

    async def revoke(self) -> None:
        self._closed = True
        if self.broker is not None:
            await self.broker.revoke()

    async def close(self) -> None:
        self._closed = True
        try:
            await self.bridge.close()
        finally:
            if self._failure is not None:
                self._failure.cancel()
                await asyncio.gather(self._failure, return_exceptions=True)

    async def _handle(self, frame: Mapping[str, object]) -> Mapping[str, object] | None:
        if self._closed or frame.get("jsonrpc") != "2.0":
            raise ProtocolError("MCP connection is closed or invalid")
        method, ident = frame.get("method"), frame.get("id")
        if method == "notifications/initialized" and ident is None and self._initialized:
            return None
        if type(ident) not in (str, int) or not 1 <= len(str(ident)) <= 128:
            raise ProtocolError("invalid MCP request identity")
        if not isinstance(method, str) or not method:
            raise ProtocolError("invalid MCP method")
        params = frame.get("params", {})
        if not isinstance(params, Mapping):
            raise ProtocolError("invalid MCP parameters")
        if method == "tools/call":
            result = await self._call(ident, params)
        else:
            self.metadata_calls += 1
            if self.metadata_calls > 64:
                raise ProtocolError("MCP metadata limit exceeded")
            if method == "initialize" and not self._initialized:
                version = params.get("protocolVersion")
                if version not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
                    raise ProtocolError("unsupported MCP protocol")
                self._initialized = True
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": "0.2"},
                }
            elif method == "tools/list" and self._initialized:
                result = {
                    "tools": [
                        {
                            "name": tool.value,
                            "description": "Run a controlled Forge operation.",
                            "inputSchema": tool_input_schema(tool),
                        }
                        for tool in sorted(self.allowed)
                        if self.request.budget.max_tool_calls
                    ]
                }
            elif method == "ping" and self._initialized:
                result = {}
            elif method in {"initialize", "notifications/initialized", "tools/list", "ping"}:
                raise ProtocolError("unsupported MCP request")
            else:
                # Optional client extensions must receive a JSON-RPC error,
                # not tear down the authenticated transport before initialization.
                return {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "error": {"code": -32601, "message": "Method not found"},
                }
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    async def _call(self, ident: object, params: Mapping[str, object]) -> dict[str, object]:
        if not self._initialized or self.broker is None:
            raise ProtocolError("MCP tools unavailable")
        try:
            name = params.get("name")
            if not isinstance(name, str):
                raise TypeError
            tool = ToolName(name)
        except ValueError, TypeError:
            raise ProtocolError("unknown Forge tool") from None
        if tool not in self.allowed:
            raise ProtocolError("tool exceeds task permission")
        args = params.get("arguments")
        schema = tool_input_schema(tool)
        if (
            not isinstance(args, Mapping)
            or not set(schema["required"]) <= set(args) <= set(schema["properties"])
            or any(type(value) is not str for value in args.values())
        ):
            if self.calls >= self.request.budget.max_tool_calls:
                raise ProtocolError("tool budget exhausted")
            self.calls += 1
            # No broker dispatch or command debit. Let the worker correct a
            # permitted tool's arguments within the same bounded invocation.
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Invalid tool arguments. Required input schema: "
                        + json.dumps(schema),
                    }
                ],
                "isError": True,
            }
        key = ("s:" if isinstance(ident, str) else "i:") + str(ident)
        call = ProviderToolCall(
            call_key=key,
            thread_id=str(self.request.attempt.attempt_id),
            turn_id=str(self.request.attempt.attempt_id),
            name=tool.value,
            arguments=freeze_context(args),
        )
        previous = self._receipts.get(key)
        if previous is not None:
            if previous[0] != call:
                raise ProtocolError("MCP request changed during replay")
            receipt = previous[1]
        else:
            check = tool is ToolName.BUILD_RUN_NAMED_CHECK
            if (
                self.calls >= self.request.budget.max_tool_calls
                or check
                and self.checks >= self.request.budget.max_named_checks
            ):
                raise ProtocolError("tool budget exhausted")
            self.calls += 1
            self.checks += int(check)
            receipt = freeze_context(await self.broker(call))
            self._receipts[key] = (call, receipt)
        return {
            "content": [{"type": "text", "text": json.dumps(json_value(receipt), allow_nan=False)}],
            "isError": False,
        }
