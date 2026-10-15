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
from forge.application.ports.tool_schemas import arguments_match_schema
from forge.domain.policy import MAX_COMMAND_TIMEOUT_SECONDS
from forge.domain.tool import ToolName

# A named check can outlive a client's MCP tool-call timeout (Muse gives up after
# about ten minutes). The call returns after this soft wait while the check keeps
# running in a task owned here; an identical later call joins the same run.
_NAMED_CHECK_WAIT_SECONDS = 90.0
# A check still running when the connection closes is left to finish (its own
# command timeout bounds it), so its effect settles with a receipt instead of
# waiting for restart recovery. Only a check that outlasts this is cancelled.
_NAMED_CHECK_FINISH_SECONDS = float(MAX_COMMAND_TIMEOUT_SECONDS + 60)
# Bound for a cancelled check to stop its container and settle.
_NAMED_CHECK_SETTLE_SECONDS = 120.0


class LocalCliMcp:
    def __init__(
        self,
        request: SubscriptionInvocationRequest,
        broker: ToolBroker | None,
        *,
        named_check_wait_seconds: float = _NAMED_CHECK_WAIT_SECONDS,
        finish_seconds: float = _NAMED_CHECK_FINISH_SECONDS,
        settle_seconds: float = _NAMED_CHECK_SETTLE_SECONDS,
    ):
        self.request, self.broker = request, broker
        self._named_check_wait = named_check_wait_seconds
        self._finish_seconds = finish_seconds
        self._settle_seconds = settle_seconds
        self._inflight: dict[str, asyncio.Task[Mapping[str, object]]] = {}
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

    @property
    def close_seconds(self) -> float:
        """Upper bound a caller should allow ``close`` before treating it as uncertain."""
        return self._finish_seconds + self._settle_seconds + 5

    async def close(self) -> None:
        self._closed = True
        try:
            await self.bridge.close()
        finally:
            try:
                await self._settle_inflight()
            finally:
                if self._failure is not None:
                    self._failure.cancel()
                    await asyncio.gather(self._failure, return_exceptions=True)

    async def _settle_inflight(self) -> None:
        """Let in-flight checks finish, cancel any that outlast the bound, or raise."""
        tasks = tuple(self._inflight.values())
        if not tasks:
            return
        try:
            _, pending = await asyncio.wait(tasks, timeout=self._finish_seconds)
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=self._settle_seconds)
        except asyncio.CancelledError:
            # The attempt itself is being stopped: stop its checks too.
            for task in tasks:
                task.cancel()
            raise
        if pending:
            raise TimeoutError("named check did not settle after the connection closed")
        self._inflight.clear()

    async def _handle(self, frame: Mapping[str, object]) -> Mapping[str, object] | None:
        if self._closed or frame.get("jsonrpc") != "2.0":
            raise ProtocolError("MCP connection is closed or invalid")
        method, ident = frame.get("method"), frame.get("id")
        if method == "notifications/initialized" and ident is None and self._initialized:
            return None
        if method == "notifications/roots/list_changed" and "id" not in frame:
            if not isinstance(frame.get("params", {}), Mapping):
                raise ProtocolError("invalid MCP parameters")
            self.metadata_calls += 1
            if self.metadata_calls > 64:
                raise ProtocolError("MCP metadata limit exceeded")
            # Some clients emit this advisory notice before initialize. Forge's
            # frozen task/worktree authority never comes from client roots.
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
        if not isinstance(args, Mapping) or not arguments_match_schema(tool, args):
            if self.calls >= self.request.budget.max_tool_calls:
                return _budget_exhausted()
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
        if tool is ToolName.BUILD_RUN_NAMED_CHECK and key not in self._receipts:
            return await self._named_check(key, call)
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
                return _budget_exhausted()
            self.calls += 1
            self.checks += int(check)
            try:
                receipt = freeze_context(await self.broker(call))
            except PermissionError:
                # The broker refused or could not complete this one callback
                # (and reconciled any admitted effect). Report it to the client
                # as a failed tool call instead of tearing down the attempt; the
                # call still counts against the tool budget and gets no receipt.
                return _refused()
            self._receipts[key] = (call, receipt)
        return {
            "content": [{"type": "text", "text": json.dumps(json_value(receipt), allow_nan=False)}],
            "isError": False,
        }

    async def _named_check(self, key: str, call: ProviderToolCall) -> dict[str, object]:
        run_key = json.dumps(json_value(call.arguments), sort_keys=True, separators=(",", ":"))
        task = self._inflight.get(run_key)
        if task is None:
            if (
                self.calls >= self.request.budget.max_tool_calls
                or self.checks >= self.request.budget.max_named_checks
            ):
                return _budget_exhausted()
            self.calls += 1
            self.checks += 1
            task = asyncio.create_task(self._dispatch(call))
            # The failure may land after the soft reply with no later join; mark
            # it retrieved now. A join still re-raises it from ``task.result()``.
            task.add_done_callback(_retrieve_exception)
            self._inflight[run_key] = task
        done, _ = await asyncio.wait({task}, timeout=self._named_check_wait)
        if not done:
            name = call.arguments.get("command_name")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Named check {name!r} is still running and keeps running "
                        "after this call returns. Call build.run_named_check again with the "
                        "same command_name to wait for its result; that does not start a "
                        "second run.",
                    }
                ],
                "isError": True,
            }
        self._inflight.pop(run_key, None)
        try:
            receipt = task.result()
        except PermissionError:
            return _refused()
        self._receipts[key] = (call, receipt)
        return {
            "content": [{"type": "text", "text": json.dumps(json_value(receipt), allow_nan=False)}],
            "isError": False,
        }

    async def _dispatch(self, call: ProviderToolCall) -> Mapping[str, object]:
        assert self.broker is not None
        return freeze_context(await self.broker(call))


def _retrieve_exception(task: asyncio.Task[Mapping[str, object]]) -> None:
    if not task.cancelled():
        task.exception()


def _budget_exhausted() -> dict[str, object]:
    # Dispatch nothing more, but let the client finish its answer: raising here
    # tore down the transport and discarded the attempt's work.
    return {
        "content": [
            {
                "type": "text",
                "text": "Tool budget exhausted for this attempt. Do not call more tools; "
                "return your final structured answer now using what you already have.",
            }
        ],
        "isError": True,
    }


def _refused() -> dict[str, object]:
    return {
        "content": [
            {
                "type": "text",
                "text": "Forge refused this tool call. Use another permitted "
                "tool or narrower arguments, or continue without it.",
            }
        ],
        "isError": True,
    }
