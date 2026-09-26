"""Bounded ACP result-echo correlation for one trusted Gemini prompt session."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import cast

from forge.agents.claude_protocol import MAX_FRAME_BYTES
from forge.agents.subscription_protocol import (
    ProtocolError,
    ProviderToolCall,
    freeze_context,
    json_value,
    parse_json,
)
from forge.application.ports.tool_schemas import arguments_match_schema
from forge.domain.tool import ToolName

Broker = Callable[[ProviderToolCall], Awaitable[Mapping[str, object]]]
_MARKER, _RECEIPT, _EXECUTE, _MAX_ID = (
    "forge_operation_proposed",
    "forge_operation_receipt",
    "forge_execute_receipt",
    255,
)


@dataclass(frozen=True, slots=True)
class GeminiPreparedTool:
    token: str
    marker: str


@dataclass(slots=True)
class _Operation:
    native_id: str
    name: str
    arguments: Mapping[str, object]
    task: asyncio.Task[Mapping[str, object]] | None = None
    receipt: Mapping[str, object] | None = None
    uncertain: bool = False
    issued_auxiliary: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Proposal:
    name: str
    arguments: Mapping[str, object]
    operation: _Operation | None = None
    issued_auxiliary: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Transport:
    fingerprint: str
    task: asyncio.Task[Mapping[str, object]]


class GeminiDualChannelProtocol:
    """Codec for one gateway-established ACP session and one Forge turn.

    The gateway establishes and serializes the dedicated session. ACP has no
    turn identity, so only the trusted constructor ``turn_id`` is used here.
    """

    def __init__(
        self, *, session_id: str, turn_id: str, broker: Broker, call_limit: int = 128
    ) -> None:
        if (
            not self._valid_text(session_id)
            or not self._valid_text(turn_id)
            or not callable(broker)
        ):
            raise ValueError("invalid Gemini protocol configuration")
        if type(call_limit) is not int or not 1 <= call_limit <= 1024:
            raise ValueError("invalid Gemini protocol configuration")
        self._session, self._turn, self._broker, self._limit = (
            session_id,
            turn_id,
            broker,
            call_limit,
        )
        self._proposals: dict[str, _Proposal] = {}
        self._operations: dict[str, _Operation] = {}
        self._started: dict[str, str] = {}
        self._auxiliary_native: dict[str, str] = {}
        self._transport: dict[str, _Transport] = {}
        self._tasks: set[asyncio.Task[Mapping[str, object]]] = set()
        self._closed = False
        self._invocations = 0

    @property
    def invocation_count(self) -> int:
        """Monotone count of accepted non-transport-replayed MCP invocations."""
        return self._invocations

    @staticmethod
    def _valid_text(value: object) -> bool:
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            return False
        try:
            return 0 < len(value.encode()) <= _MAX_ID
        except UnicodeError:
            return False

    @staticmethod
    def _transport_id(value: object) -> str | None:
        if isinstance(value, str) and GeminiDualChannelProtocol._valid_text(value):
            return f"s:{value}"
        if type(value) is float:
            if not math.isfinite(value) or not value.is_integer():
                return None
            # The pinned MCP schema requires integers; 1 and 1.0 have one identity.
            value = int(value)
        if type(value) is int:
            number = str(value)
            if len(number) <= _MAX_ID:
                return f"n:{number}"
        return None

    @staticmethod
    def _fingerprint(value: object) -> str:
        try:
            return json.dumps(
                json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except TypeError, ValueError, RecursionError:
            raise ProtocolError("invalid Gemini structured value") from None

    @staticmethod
    def _text(value: Mapping[str, object]) -> str:
        try:
            text = json.dumps(
                json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except TypeError, ValueError, RecursionError:
            raise ProtocolError("invalid Gemini receipt") from None
        if len(text.encode()) > MAX_FRAME_BYTES:
            raise ProtocolError("Gemini receipt exceeds frame bound")
        return text

    def _take(self) -> None:
        if self._closed or self._invocations >= self._limit:
            raise ProtocolError("Gemini tool invocation denied")
        self._invocations += 1

    def prepare(self, name: str, arguments: Mapping[str, object]) -> GeminiPreparedTool:
        self._take()
        try:
            tool = ToolName(name)
            if not isinstance(arguments, Mapping) or not arguments_match_schema(tool, arguments):
                raise ValueError
            frozen = freeze_context(arguments)
        except TypeError, ValueError:
            raise ProtocolError("Gemini tool proposal denied") from None
        if len(self._proposals) >= self._limit:
            raise ProtocolError("too many Gemini proposals")
        token = secrets.token_urlsafe(32)
        self._proposals[token] = _Proposal(name, frozen)
        return GeminiPreparedTool(token, self._text({"kind": _MARKER, "nonce": token}))

    def receive_acp(self, line: str) -> None:
        frame = self._frame(line)
        if frame.get("method") != "session/update" or set(frame) != {"jsonrpc", "method", "params"}:
            raise ProtocolError("unsupported Gemini ACP frame")
        params = frame.get("params")
        if (
            not isinstance(params, Mapping)
            or set(params) != {"sessionId", "update"}
            or params.get("sessionId") != self._session
        ):
            raise ProtocolError("invalid Gemini ACP identity")
        update = params["update"]
        if not isinstance(update, Mapping) or not self._valid_text(update.get("toolCallId")):
            raise ProtocolError("unsupported Gemini ACP update")
        native, fingerprint = update["toolCallId"], self._fingerprint(update)
        if update.get("sessionUpdate") == "tool_call":
            if not self._valid_start(update):
                raise ProtocolError("invalid Gemini ACP start")
            prior = self._started.get(native)
            if prior is not None:
                if prior != fingerprint:
                    raise ProtocolError("conflicting Gemini ACP start")
                return
            if len(self._started) >= self._limit:
                raise ProtocolError("too many Gemini ACP starts")
            self._started[native] = fingerprint
            return
        if (
            update.get("sessionUpdate") != "tool_call_update"
            or native not in self._started
            or not self._valid_completion(update)
        ):
            raise ProtocolError("invalid Gemini ACP update")
        text = self._completion_text(update)
        body = parse_json(text)
        if body.get("kind") == _MARKER:
            self._bind_proposal(native, body)
            return
        if body.get("kind") == _RECEIPT:
            self._consume_auxiliary(native, text)
            return
        raise ProtocolError("invalid Gemini ACP completion")

    @staticmethod
    def _valid_start(update: Mapping[str, object]) -> bool:
        allowed = {
            "sessionUpdate",
            "toolCallId",
            "status",
            "title",
            "content",
            "locations",
            "kind",
            "rawInput",
            "rawOutput",
            "_meta",
        }
        return (
            not (set(update) - allowed)
            and update.get("status") == "in_progress"
            and isinstance(update.get("title"), str)
            and update.get("kind") == "other"
            and isinstance(update.get("content"), (list, tuple))
            and isinstance(update.get("locations"), (list, tuple))
        )

    @staticmethod
    def _valid_completion(update: Mapping[str, object]) -> bool:
        allowed = {
            "sessionUpdate",
            "toolCallId",
            "status",
            "title",
            "content",
            "locations",
            "kind",
            "rawInput",
            "rawOutput",
            "_meta",
        }
        return (
            not (set(update) - allowed)
            and update.get("status") == "completed"
            and update.get("kind") == "other"
        )

    @staticmethod
    def _completion_text(update: Mapping[str, object]) -> str:
        content = update.get("content")
        if (
            not isinstance(content, (list, tuple))
            or len(content) != 1
            or not isinstance(content[0], Mapping)
        ):
            raise ProtocolError("invalid Gemini ACP completion")
        item = content[0]
        if (
            set(item) != {"type", "content"}
            or item.get("type") != "content"
            or not isinstance(item.get("content"), Mapping)
        ):
            raise ProtocolError("invalid Gemini ACP completion")
        nested = item["content"]
        if (
            set(nested) != {"type", "text"}
            or nested.get("type") != "text"
            or not isinstance(nested.get("text"), str)
        ):
            raise ProtocolError("invalid Gemini ACP completion")
        return cast(str, nested["text"])

    def _bind_proposal(self, native: str, body: Mapping[str, object]) -> None:
        nonce = body.get("nonce")
        if set(body) != {"kind", "nonce"} or not isinstance(nonce, str):
            raise ProtocolError("invalid Gemini ACP completion")
        proposal = self._proposals.get(nonce)
        if (
            proposal is None
            or native in self._auxiliary_native
            or (proposal.operation is not None and proposal.operation.native_id != native)
        ):
            raise ProtocolError("Gemini tool echo denied")
        operation = self._operations.get(native)
        if operation is None:
            if len(self._operations) >= self._limit:
                raise ProtocolError("too many Gemini operations")
            operation = self._operations[native] = _Operation(
                native, proposal.name, proposal.arguments
            )
        elif (operation.name, operation.arguments) != (proposal.name, proposal.arguments):
            raise ProtocolError("Gemini tool echo denied")
        proposal.operation = operation
        operation.issued_auxiliary.update(proposal.issued_auxiliary)

    def _consume_auxiliary(self, native: str, text: str) -> None:
        body = parse_json(text)
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or set(body) != {"kind", "nonce", "receipt"}:
            raise ProtocolError("Gemini tool echo denied")
        proposal = self._proposals.get(nonce)
        if proposal is None or native in self._operations:
            raise ProtocolError("Gemini tool echo denied")
        issued = (
            proposal.operation.issued_auxiliary
            if proposal.operation is not None
            else proposal.issued_auxiliary
        )
        if text not in issued:
            raise ProtocolError("Gemini tool echo denied")
        prior = self._auxiliary_native.get(native)
        if prior is not None and prior != text:
            raise ProtocolError("Gemini tool echo denied")
        if prior is None:
            if len(self._auxiliary_native) >= self._limit:
                raise ProtocolError("too many Gemini auxiliary updates")
            self._auxiliary_native[native] = text

    def _frame(self, line: str) -> Mapping[str, object]:
        if self._closed or type(line) is not str:
            raise ProtocolError("invalid Gemini frame")
        try:
            if len(line.encode()) > MAX_FRAME_BYTES:
                raise ProtocolError("invalid Gemini frame")
        except UnicodeError:
            raise ProtocolError("invalid Gemini frame") from None
        frame = parse_json(line)
        if frame.get("jsonrpc") != "2.0":
            raise ProtocolError("invalid Gemini frame")
        return frame

    async def receive_mcp(self, line: str) -> Mapping[str, object]:
        if self._closed:
            raise ProtocolError("Gemini tool invocation denied")
        frame = self._frame(line)
        if (
            set(frame) != {"jsonrpc", "id", "method", "params"}
            or frame.get("method") != "tools/call"
            or not isinstance(frame.get("params"), Mapping)
        ):
            raise ProtocolError("invalid Gemini MCP request")
        params = frame.get("params")
        ident = self._transport_id(frame.get("id"))
        if not isinstance(params, Mapping):
            raise ProtocolError("invalid Gemini MCP request")
        if ident is None or not self._valid_mcp_params(params):
            raise ProtocolError("invalid Gemini MCP request")
        fingerprint = self._fingerprint(params)
        prior = self._transport.get(ident)
        if prior is not None:
            if prior.fingerprint != fingerprint:
                raise ProtocolError("conflicting Gemini MCP replay")
            return await asyncio.shield(prior.task)
        if len(self._transport) >= self._limit:
            raise ProtocolError("too many Gemini MCP requests")
        task = asyncio.create_task(self._handle_mcp(frame["id"], params))
        self._track(task)
        self._transport[ident] = _Transport(fingerprint, task)
        return await asyncio.shield(task)

    def _valid_mcp_params(self, params: Mapping[str, object]) -> bool:
        if (
            set(params) - {"name", "arguments", "_meta"}
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments"), Mapping)
        ):
            return False
        meta = params.get("_meta")
        return meta is None or (
            isinstance(meta, Mapping)
            and set(meta) == {"progressToken"}
            # Progress tokens share MCP's string/integer shape, not tool authority.
            and self._transport_id(meta.get("progressToken")) is not None
        )

    def _track(self, task: asyncio.Task[Mapping[str, object]]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._consume_task_exception)

    def _consume_task_exception(self, task: asyncio.Task[Mapping[str, object]]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _handle_mcp(
        self, request_id: object, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        name = cast(str, params["name"])
        arguments = cast(Mapping[str, object], params["arguments"])
        if name == _EXECUTE:
            token = arguments.get("token")
            if set(arguments) != {"token"} or not isinstance(token, str):
                raise ProtocolError("unregistered Gemini MCP tool")
            self._take()
            receipt = await self._execute(token)
            return self._issue_receipt(request_id, token, receipt)
        prepared = self.prepare(name, arguments)
        return self._response(request_id, prepared.marker, error=False)

    @staticmethod
    def _response(request_id: object, text: str, *, error: bool) -> Mapping[str, object]:
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": error},
        }
        # The enclosing MCP JSON escapes its text again; bound that entire frame.
        if len(json.dumps(response, allow_nan=False).encode()) > MAX_FRAME_BYTES:
            raise ProtocolError("Gemini response exceeds frame bound")
        return response

    def _issue_receipt(
        self, request_id: object, token: str, receipt: Mapping[str, object]
    ) -> Mapping[str, object]:
        proposal = self._proposals.get(token)
        result = {"kind": _RECEIPT, "nonce": token, "receipt": dict(receipt)}
        try:
            text = self._text(result)
            response = self._response(request_id, text, error=receipt.get("status") == "not_ready")
            if proposal is not None:
                issued = (
                    proposal.operation.issued_auxiliary
                    if proposal.operation is not None
                    else proposal.issued_auxiliary
                )
                if text not in issued and len(issued) >= self._limit:
                    raise ProtocolError("too many Gemini receipts")
                issued.add(text)
            return response
        except ProtocolError:
            if proposal is not None and proposal.operation is not None:
                proposal.operation.uncertain = True
                proposal.operation.receipt = None
            raise

    async def _execute(self, token: str) -> Mapping[str, object]:
        proposal = self._proposals.get(token)
        if proposal is None or proposal.operation is None or self._closed:
            return {"status": "not_ready"}
        operation = proposal.operation
        if operation.uncertain:
            return {"status": "not_ready"}
        if operation.receipt is not None:
            return operation.receipt
        if operation.task is None:
            key = hashlib.sha256(
                (self._session + "\0" + self._turn + "\0" + operation.native_id).encode()
            ).hexdigest()
            call = ProviderToolCall(
                call_key=key,
                thread_id=self._session,
                turn_id=self._turn,
                name=operation.name,
                arguments=operation.arguments,
            )
            operation.task = asyncio.create_task(self._broker_call(operation, call))
            self._track(operation.task)
        return await asyncio.shield(operation.task)

    async def _broker_call(
        self, operation: _Operation, call: ProviderToolCall
    ) -> Mapping[str, object]:
        try:
            result = await self._broker(call)
        except BaseException:
            operation.uncertain = True
            raise
        if self._closed or not isinstance(result, Mapping):
            operation.uncertain = True
            return {"status": "not_ready"}
        try:
            receipt = dict(freeze_context(result))
            self._text(receipt)
        except ProtocolError:
            operation.uncertain = True
            raise
        operation.receipt = receipt
        return operation.receipt

    def close(self) -> None:
        self._closed = True
        self._proposals.clear()
        self._operations.clear()
        self._started.clear()
        self._auxiliary_native.clear()
        self._transport.clear()

    @property
    def pending(self) -> bool:
        return any(not task.done() for task in self._tasks)

    async def drain(self) -> None:
        """Finish owned callback cancellation/reconciliation before resources close."""
        if not self._closed:
            raise RuntimeError("Gemini protocol must be closed before draining")
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["GeminiDualChannelProtocol", "GeminiPreparedTool"]
