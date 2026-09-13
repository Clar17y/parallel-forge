"""One bounded official ACP v1 session with a separate Forge MCP transport."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping
from time import monotonic
from uuid import uuid4

from forge.agents.client_process import ClientProcessSession
from forge.agents.codex_gateway import ToolBroker
from forge.agents.gemini_gateway_mcp import MAX_FRAME_BYTES, GeminiMcpBridge
from forge.agents.gemini_protocol import GeminiDualChannelProtocol
from forge.agents.subscription_protocol import (
    ProtocolError,
    ProviderToolCall,
    decode_final,
    freeze_context,
    json_value,
    output_schema,
    parse_json,
    tool_input_schema,
)
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.domain.subscription import AttemptTelemetry
from forge.domain.tool import ToolName


class GeminiResponseFailure(Exception):
    def __init__(self, failure: SubscriptionFailure) -> None:
        self.failure = failure
        super().__init__("Gemini request failed")


class GeminiSession:
    def __init__(
        self, request: SubscriptionInvocationRequest, broker: ToolBroker | None, cwd: str
    ) -> None:
        self.request, self.broker, self.cwd = request, broker, cwd
        self.started = monotonic()
        self.input: int | None = None
        self.output: int | None = None
        self.calls = self.checks = self.metadata_calls = 0
        self.session_id: str | None = None
        self.codec: GeminiDualChannelProtocol | None = None
        self._failure: asyncio.Task[None] | None = None
        self._mcp_initialized = False
        self._closed = False
        self.allowed = (
            frozenset(tool.value for tool in request.authorization.permitted_tools)
            if request.budget.max_tool_calls
            else frozenset()
        )
        self.bridge = GeminiMcpBridge(secrets.token_urlsafe(32), self._mcp)

    def telemetry(self) -> AttemptTelemetry:
        return AttemptTelemetry(
            input_tokens=self.input,
            output_tokens=self.output,
            duration_ms=int((monotonic() - self.started) * 1000),
            tool_call_count=self.calls,
            named_check_count=self.checks,
            unknown_telemetry_reasons=("subscription cost and quota telemetry unavailable",)
            + (() if self.input is not None else ("token telemetry unavailable",)),
        )

    async def start(self) -> None:
        await self.bridge.start()
        self._failure = asyncio.create_task(self.bridge.wait_failed())

    async def revoke(self) -> None:
        self._closed = True
        if self.codec:
            self.codec.close()
        if self.broker:
            await self.broker.revoke()

    async def close(self) -> None:
        self._closed = True
        if self.codec:
            self.codec.close()
        try:
            await self.bridge.close()
        finally:
            if self._failure:
                self._failure.cancel()
                await asyncio.gather(self._failure, return_exceptions=True)
            if self.codec:
                await self.codec.drain()

    async def _receive(self, session: ClientProcessSession) -> Mapping[str, object]:
        assert self._failure is not None
        read = asyncio.create_task(session.receive())
        try:
            done, _ = await asyncio.wait((read, self._failure), return_when=asyncio.FIRST_COMPLETED)
            if self._failure in done:
                self._failure.result()
            value = await read
            if not isinstance(value, Mapping) or value.get("jsonrpc") != "2.0":
                raise ProtocolError("invalid Gemini ACP frame")
            return value
        finally:
            if not read.done():
                read.cancel()
            await asyncio.gather(read, return_exceptions=True)

    @staticmethod
    def _result(frame: Mapping[str, object], ident: int) -> Mapping[str, object]:
        if type(frame.get("id")) is not int or frame["id"] != ident or "method" in frame:
            raise ProtocolError("foreign Gemini response")
        error = frame.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            if type(code) is not int:
                raise ProtocolError("invalid Gemini error")
            # The pinned ACP client replaces every upstream 429 with generic
            # throttling text. It cannot establish confirmed usage exhaustion.
            reason = (
                SubscriptionFailure.THROTTLED
                if code == 429
                else SubscriptionFailure.AUTHENTICATION
                if code in (-32000, 401, 403)
                else SubscriptionFailure.UNSUPPORTED
                if code in (-32601, 404)
                else SubscriptionFailure.OUTAGE
                if 500 <= code <= 599
                else SubscriptionFailure.PROTOCOL
            )
            raise GeminiResponseFailure(reason)
        result = frame.get("result")
        if not isinstance(result, Mapping) or "error" in frame:
            raise ProtocolError("invalid Gemini result")
        return result

    async def run(self, session: ClientProcessSession) -> SubscriptionInvocationResult:
        await session.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                        "auth": {"terminal": False},
                    },
                    "clientInfo": {"name": "forge", "version": "0.2"},
                },
            }
        )
        initialized = self._result(await self._receive(session), 1)
        agent = initialized.get("agentInfo")
        if (
            type(initialized.get("protocolVersion")) is not int
            or initialized["protocolVersion"] != 1
            or not isinstance(agent, Mapping)
            or agent.get("name") != "gemini-cli"
            or agent.get("version") != "0.59.0"
        ):
            raise GeminiResponseFailure(SubscriptionFailure.UNSUPPORTED)
        await session.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {
                    "cwd": self.cwd,
                    "mcpServers": [self.bridge.descriptor()],
                },
            }
        )
        created = self._result(await self._receive(session), 2)
        identity, models = created.get("sessionId"), created.get("models")
        if (
            not isinstance(identity, str)
            or not 1 <= len(identity) <= 255
            or any(ord(c) < 32 for c in identity)
        ):
            raise ProtocolError("invalid Gemini session")
        if (
            not isinstance(models, Mapping)
            or models.get("currentModelId") != self.request.task.route.effective.model
        ):
            raise GeminiResponseFailure(SubscriptionFailure.UNSUPPORTED)
        self.session_id = identity
        self.codec = GeminiDualChannelProtocol(
            session_id=identity,
            turn_id=str(uuid4()),
            broker=self._call,
            call_limit=max(1, min(1024, 4 * self.request.budget.max_tool_calls + 16)),
        )
        body = json.dumps(
            {
                "untrusted_context": json_value(self.request.untrusted_context),
                "output_schema": output_schema(self.request),
            },
            separators=(",", ":"),
            allow_nan=False,
        )
        await session.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": identity,
                    "prompt": [{"type": "text", "text": body}],
                },
            }
        )
        parts: list[str] = []
        size = 0
        while True:
            frame = await self._receive(session)
            if frame.get("method") == "session/request_permission":
                await session.send(
                    {
                        "jsonrpc": "2.0",
                        "id": frame.get("id"),
                        "result": {
                            "outcome": {"outcome": "cancelled"},
                        },
                    }
                )
                raise GeminiResponseFailure(SubscriptionFailure.POLICY_DENIED)
            if frame.get("method") == "session/update":
                params = frame.get("params")
                if (
                    not isinstance(params, Mapping)
                    or params.get("sessionId") != identity
                    or not isinstance(params.get("update"), Mapping)
                ):
                    raise ProtocolError("foreign Gemini update")
                update = params["update"]
                kind = update.get("sessionUpdate")
                if kind in ("tool_call", "tool_call_update"):
                    if kind == "tool_call":
                        parts.clear()
                        size = 0
                    self.codec.receive_acp(json.dumps(json_value(frame), allow_nan=False))
                elif kind == "agent_message_chunk":
                    content = update.get("content")
                    if (
                        not isinstance(content, Mapping)
                        or content.get("type") != "text"
                        or not isinstance(content.get("text"), str)
                    ):
                        raise ProtocolError("invalid Gemini message content")
                    size += len(content["text"].encode())
                    if size > MAX_FRAME_BYTES:
                        raise ProtocolError("Gemini final text exceeds bound")
                    parts.append(content["text"])
                elif kind not in {
                    "agent_thought_chunk",
                    "available_commands_update",
                    "plan",
                    "session_info_update",
                }:
                    raise ProtocolError("unsupported Gemini update")
                continue
            final = self._result(frame, 3)
            self._usage(final)
            reason = final.get("stopReason")
            if reason in ("max_tokens", "max_turn_requests"):
                raise GeminiResponseFailure(SubscriptionFailure.BUDGET)
            if reason == "cancelled":
                raise GeminiResponseFailure(SubscriptionFailure.INTERRUPTED)
            if reason != "end_turn" or self.codec.pending:
                raise ProtocolError("Gemini turn is not safely complete")
            return decode_final(parse_json("".join(parts)), self.request)

    def _usage(self, final: Mapping[str, object]) -> None:
        meta = final.get("_meta")
        quota = meta.get("quota") if isinstance(meta, Mapping) else None
        counts = quota.get("token_count") if isinstance(quota, Mapping) else None
        if not isinstance(counts, Mapping):
            return
        source, target = counts.get("input_tokens"), counts.get("output_tokens")
        if type(source) is not int or type(target) is not int or source < 0 or target < 0:
            raise ProtocolError("invalid Gemini token telemetry")
        assert isinstance(quota, Mapping)
        models = quota.get("model_usage")
        if not isinstance(models, (list, tuple)) or len(models) > 64:
            raise ProtocolError("invalid Gemini model telemetry")
        measured_input = measured_output = 0
        foreign_model = False
        for item in models:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("model"), str)
                or not item["model"]
            ):
                raise ProtocolError("invalid Gemini model telemetry")
            foreign_model |= item["model"] != self.request.task.route.effective.model
            tokens = item.get("token_count")
            if not isinstance(tokens, Mapping):
                raise ProtocolError("invalid Gemini model telemetry")
            left, right = tokens.get("input_tokens"), tokens.get("output_tokens")
            if type(left) is not int or type(right) is not int or left < 0 or right < 0:
                raise ProtocolError("invalid Gemini model telemetry")
            measured_input += left
            measured_output += right
        if (source, target) != (measured_input, measured_output):
            raise ProtocolError("inconsistent Gemini token telemetry")
        # ACP emits zero totals with no model measurement after some failures.
        if models:
            self.input, self.output = source, target
        if foreign_model:
            raise GeminiResponseFailure(SubscriptionFailure.UNSUPPORTED)
        budget = self.request.budget
        if (budget.max_input_tokens is not None and source > budget.max_input_tokens) or (
            budget.max_output_tokens is not None and target > budget.max_output_tokens
        ):
            raise GeminiResponseFailure(SubscriptionFailure.BUDGET)

    async def _call(self, call: ProviderToolCall) -> Mapping[str, object]:
        if self.broker is None or call.name not in self.allowed:
            raise ProtocolError("unadmitted Gemini tool")
        check = call.name == ToolName.BUILD_RUN_NAMED_CHECK.value
        if self.calls >= self.request.budget.max_tool_calls or (
            check and self.checks >= self.request.budget.max_named_checks
        ):
            raise ProtocolError("Gemini tool budget exhausted")
        self.calls += 1
        self.checks += int(check)
        return freeze_context(await self.broker(call))

    async def _mcp(self, frame: Mapping[str, object]) -> Mapping[str, object] | None:
        method = frame.get("method")
        if self._closed or frame.get("jsonrpc") != "2.0":
            raise ProtocolError("invalid Gemini MCP frame")
        if method == "tools/call":
            params = frame.get("params")
            if (
                not self._mcp_initialized
                or self.codec is None
                or not isinstance(params, Mapping)
                or params.get("name") not in self.allowed | {"forge_execute_receipt"}
            ):
                raise ProtocolError("unadmitted Gemini MCP tool")
            return await self.codec.receive_mcp(json.dumps(json_value(frame), allow_nan=False))
        self.metadata_calls += 1
        if self.metadata_calls > 64:
            raise ProtocolError("too many Gemini MCP metadata requests")
        if method == "notifications/initialized" and "id" not in frame and self._mcp_initialized:
            return None
        ident = frame.get("id")
        if (
            type(ident) not in (str, int)
            or not 1 <= len(str(ident).encode()) <= 255
            or any(ord(char) < 32 for char in str(ident))
        ):
            raise ProtocolError("invalid Gemini MCP identity")
        result: dict[str, object]
        if method == "initialize" and not self._mcp_initialized:
            params = frame.get("params")
            if not isinstance(params, Mapping) or params.get("protocolVersion") not in {
                "2024-11-05",
                "2025-03-26",
                "2025-06-18",
                "2025-11-25",
            }:
                raise ProtocolError("unsupported Gemini MCP initialization")
            self._mcp_initialized = True
            result = {
                "protocolVersion": params["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "forge", "version": "0.2"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list" and self._mcp_initialized:
            entries = [
                {
                    "name": name,
                    "description": "Prepare a Forge controlled operation; execute the returned token using forge_execute_receipt.",
                    "inputSchema": tool_input_schema(ToolName(name)),
                }
                for name in sorted(self.allowed)
            ]
            if entries:
                entries.append(
                    {
                        "name": "forge_execute_receipt",
                        "description": "Execute a prepared Forge token after its ACP echo; not_ready grants no authority.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"token": {"type": "string"}},
                            "required": ["token"],
                            "additionalProperties": False,
                        },
                    }
                )
            result = {"tools": entries}
        else:
            raise ProtocolError("unsupported Gemini MCP method")
        return {"jsonrpc": "2.0", "id": ident, "result": result}
