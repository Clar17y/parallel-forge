"""Bounded, providerless official-client conformance for Claude review."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self, cast
from uuid import uuid4

from forge.agents.capability_verification import stable_executable_digest
from forge.agents.claude_gateway import (
    CLAUDE_CLIENT_VERSION,
    ClaudeInstallation,
    claude_configuration_matches,
    claude_initialize_request,
    claude_launch_arguments,
    claude_managed_policy_is_empty,
    claude_preinit_handshake_frame_is,
    claude_settings_match,
    claude_setup_handshake_frame_is,
    claude_tool_alias,
)
from forge.agents.claude_protocol import ClaudeStreamCodec
from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
    ClaudeVerificationScope,
    required_claude_verification_scopes,
)
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessSession,
    ClientProcessSupervisor,
    terminal_launch_proof,
)
from forge.agents.subscription_protocol import ProviderToolCall
from forge.domain.capability_evidence import capability_home_digest
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.tool import ToolName

_OFFLINE_API_KEY = "forge-offline-conformance"
_FORBIDDEN_TOOL_NAMES = (
    "Bash",
    "Read",
    "Skill",
    "WebFetch",
    "mcp__inherited_canary__touch",
)
_MAX_HTTP_BYTES = 1024 * 1024
_LOOPBACK_REQUEST_SECONDS = 2.0
_RESULT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"result": {"type": "string", "const": "offline complete"}},
    "required": ["result"],
}


class ClaudeConformanceError(RuntimeError):
    """The bounded offline client did not satisfy the closed conformance contract."""


ClaudeConformanceScope = ClaudeVerificationScope


def required_claude_live_scopes() -> tuple[ClaudeConformanceScope, ...]:
    """Return scopes that need separate explicitly authorized live proof."""

    return required_claude_verification_scopes()


@dataclass(frozen=True, slots=True)
class ClaudeConformanceResult:
    scope: ClaudeConformanceScope
    executable_digest: str
    client_home_digest: str
    account: str
    configuration_isolated: bool
    only_forge_tools_advertised: bool
    forbidden_tool_calls_denied: tuple[str, ...]
    callback_identity_bound: bool
    callback_result_forwarded: bool
    side_effects_absent: bool
    alternate_auth_isolated: bool
    terminal_proof: SubscriptionLaunchTerminalProof
    publishable: bool = field(default=False, init=False)
    live_provider_call: bool = field(default=False, init=False)

    def sanitized_payload(self) -> dict[str, object]:
        """Return the closed, path-free offline result safe for retention."""

        return {
            "schema_version": 1,
            "kind": "claude_official_offline_conformance",
            "installation": {
                "account": self.account,
                "client_home_digest": self.client_home_digest,
                "client_version": CLAUDE_CLIENT_VERSION,
                "executable_digest": self.executable_digest,
            },
            "scope": {
                "name": self.scope.name,
                "model": self.scope.model,
                "effort": self.scope.effort,
                "role": self.scope.role.value,
                "tool_surface": [tool.value for tool in self.scope.tool_surface],
            },
            "verifier": {"id": CLAUDE_VERIFIER_ID, "version": CLAUDE_VERIFIER_VERSION},
            "observations": {
                "alternate_auth_isolated": self.alternate_auth_isolated,
                "callback_identity_bound": self.callback_identity_bound,
                "callback_result_forwarded": self.callback_result_forwarded,
                "configuration_isolated": self.configuration_isolated,
                "forbidden_tool_calls_denied": list(self.forbidden_tool_calls_denied),
                "only_forge_tools_advertised": self.only_forge_tools_advertised,
                "side_effects_absent": self.side_effects_absent,
            },
            "terminal": self.terminal_proof.model_dump(mode="json"),
            "publication": {
                "eligible": self.publishable,
                "live_provider_call": self.live_provider_call,
                "account_authentication_proven": False,
                "billing_enforcement_proven": False,
                "reason": "offline transport proof cannot satisfy account or billing proofs",
            },
        }


@dataclass(slots=True)
class ClaudeOfficialConformanceHarness:
    """Exercise the exact official binary against a loopback Messages fixture."""

    side_effect_markers: Sequence[str | os.PathLike[str]] = field(default=(), repr=False)
    supervisor: ClientProcessSupervisor = field(default_factory=ClientProcessSupervisor, repr=False)

    def __post_init__(self) -> None:
        markers = tuple(Path(path).resolve() for path in self.side_effect_markers)
        if any(path.exists() for path in markers):
            raise ValueError("Claude conformance markers must not already exist")
        if not callable(getattr(self.supervisor, "start", None)):
            raise TypeError("Claude conformance requires a client supervisor")
        self.side_effect_markers = markers

    async def run(
        self, installation: ClaudeInstallation, scope: ClaudeConformanceScope
    ) -> ClaudeConformanceResult:
        if not isinstance(installation, ClaudeInstallation) or not isinstance(
            scope, ClaudeConformanceScope
        ):
            raise TypeError("Claude conformance requires an installation and scope")
        if (installation.model, installation.effort) != (scope.model, scope.effort):
            raise ValueError("Claude conformance scope differs from the installation")
        if not claude_managed_policy_is_empty(installation.client_home):
            raise ClaudeConformanceError("Claude managed policy is not empty")
        digest = await asyncio.to_thread(stable_executable_digest, installation.executable)
        if digest != installation.executable_digest:
            raise ClaudeConformanceError("Claude executable identity differs")

        shell_marker = Path(installation.cwd) / ".forge-claude-shell-canary"
        if shell_marker.exists():
            raise ValueError("Claude native conformance marker already exists")
        read_sentinel = Path(installation.cwd) / "README.md"
        if not read_sentinel.is_file():
            raise ValueError("Claude conformance requires a readable repository sentinel")
        markers = (*cast(tuple[Path, ...], self.side_effect_markers), shell_marker)
        session_id = str(uuid4())
        turn_id = str(uuid4())
        callback: list[ProviderToolCall] = []
        configuration_admitted = asyncio.Event()

        async def handle(call: ProviderToolCall) -> Mapping[str, object]:
            if not configuration_admitted.is_set():
                raise ClaudeConformanceError("Claude callback preceded capability admission")
            callback.append(call)
            return {"status": "succeeded", "path": "README.md"}

        codec = ClaudeStreamCodec(
            session_id,
            turn_id,
            handle,
            frozenset(tool.value for tool in scope.tool_surface),
        )
        session: ClientProcessSession | None = None
        process_result = None
        terminal: Mapping[str, object] | None = None
        init: Mapping[str, object] | None = None
        settings: Mapping[str, object] | None = None
        completed = False
        async with _LoopbackMessages(_response_sequence(shell_marker, read_sentinel)) as messages:
            spec = ClientLaunchSpec(
                argv=(
                    installation.executable,
                    *claude_launch_arguments(
                        installation,
                        session_id=session_id,
                        system_prompt="Use only the supplied Forge MCP tools.",
                        permitted_tools=frozenset(scope.tool_surface),
                        schema=_RESULT_SCHEMA,
                    ),
                ),
                cwd=installation.cwd,
                environment={
                    "ANTHROPIC_API_KEY": _OFFLINE_API_KEY,
                    "ANTHROPIC_BASE_URL": messages.base_url,
                    "CLAUDE_CODE_MANAGED_SETTINGS_PATH": installation.client_home,
                    "CLAUDE_CONFIG_DIR": installation.client_home,
                },
                allowed_environment=frozenset(
                    {
                        "ANTHROPIC_API_KEY",
                        "ANTHROPIC_BASE_URL",
                        "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
                        "CLAUDE_CONFIG_DIR",
                    }
                ),
                executable_digest=installation.executable_digest,
                duration_seconds=installation.duration_seconds,
            )
            try:
                session = await self.supervisor.start(spec)
                await session.send(
                    {
                        "type": "control_request",
                        "request_id": "forge_initialize",
                        "request": claude_initialize_request(),
                    }
                )
                await _await_initialization(session, codec)
                await session.send(
                    {
                        "type": "control_request",
                        "request_id": "forge_settings",
                        "request": {"subtype": "get_settings"},
                    }
                )
                settings, init = await _await_settings(session, codec)
                if not claude_settings_match(
                    settings, model=installation.model, effort=installation.effort
                ):
                    raise ClaudeConformanceError("Claude effective settings differ")
                if init is not None:
                    _validate_configuration(init, settings, installation, scope, session_id)
                    configuration_admitted.set()
                await session.send(
                    {
                        "type": "user",
                        "session_id": session_id,
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Run the bounded callback check."}
                            ],
                        },
                        "parent_tool_use_id": None,
                    }
                )
                terminal, init = await _await_terminal(
                    session,
                    codec,
                    init,
                    settings,
                    installation,
                    scope,
                    session_id,
                    configuration_admitted,
                )
                completed = terminal.get("structured_output") == {"result": "offline complete"}
            finally:
                if session is not None:
                    process_result = await session.close(completed=completed)

            messages.assert_complete()
            tools_match, forwarded, alternate_auth_isolated = _inspect_requests(
                messages.requests, scope
            )

        if process_result is None or terminal is None or init is None or settings is None:
            raise ClaudeConformanceError("Claude process proof is unavailable")
        expected_arguments = {"path": "README.md"}
        if len(callback) != 1 or (callback[0].name, dict(callback[0].arguments)) != (
            ToolName.REPOSITORY_READ_FILE.value,
            expected_arguments,
        ):
            raise ClaudeConformanceError("Claude callback identity differs")
        callback_identity_bound = codec.tool_use_id_for(callback[0].call_key) == "forge-call-1"
        if not callback_identity_bound:
            raise ClaudeConformanceError("Claude callback tool-use identity differs")
        side_effects_absent = not any(marker.exists() for marker in markers)
        proof = terminal_launch_proof(process_result)
        if not completed or not side_effects_absent or not proof.permits_decision:
            raise ClaudeConformanceError("Claude isolation or terminal proof is incomplete")
        return ClaudeConformanceResult(
            scope=scope,
            executable_digest=digest,
            client_home_digest=capability_home_digest(installation.client_home),
            account=installation.account,
            configuration_isolated=True,
            only_forge_tools_advertised=tools_match,
            forbidden_tool_calls_denied=_FORBIDDEN_TOOL_NAMES,
            callback_identity_bound=callback_identity_bound,
            callback_result_forwarded=forwarded,
            side_effects_absent=side_effects_absent,
            alternate_auth_isolated=alternate_auth_isolated,
            terminal_proof=proof,
        )


async def _await_initialization(session: ClientProcessSession, codec: ClaudeStreamCodec) -> None:
    for _ in range(128):
        frame = await session.receive()
        if not isinstance(frame, Mapping):
            raise ClaudeConformanceError("Claude ended during initialization")
        if _is_control_response(frame, "forge_initialize"):
            response = cast(Mapping[str, object], frame["response"])
            if response.get("subtype") != "success":
                raise ClaudeConformanceError("Claude rejected SDK initialization")
            return
        if not claude_setup_handshake_frame_is(frame):
            raise ClaudeConformanceError("Claude emitted an event before capability admission")
        reply = await codec.receive(json.dumps(frame, separators=(",", ":")))
        if reply is not None:
            await session.send(reply)
    raise ClaudeConformanceError("Claude emitted too many initialization events")


async def _await_settings(
    session: ClientProcessSession, codec: ClaudeStreamCodec
) -> tuple[Mapping[str, object], Mapping[str, object] | None]:
    init: Mapping[str, object] | None = None
    for _ in range(128):
        frame = await session.receive()
        if not isinstance(frame, Mapping):
            raise ClaudeConformanceError("Claude ended before reporting settings")
        if _is_control_response(frame, "forge_settings"):
            response = cast(Mapping[str, object], frame["response"])
            value = response.get("response")
            if response.get("subtype") != "success" or not isinstance(value, Mapping):
                raise ClaudeConformanceError("Claude effective settings are unavailable")
            return value, init
        if frame.get("type") == "system" and frame.get("subtype") == "init":
            if init is not None:
                raise ClaudeConformanceError("duplicate Claude initialization metadata")
            if not codec.handshake_complete:
                raise ClaudeConformanceError("Claude initialized before MCP handshake completion")
            init = frame
            continue
        if not claude_setup_handshake_frame_is(frame):
            raise ClaudeConformanceError("Claude emitted an event before capability admission")
        reply = await codec.receive(json.dumps(frame, separators=(",", ":")))
        if reply is not None:
            await session.send(reply)
    raise ClaudeConformanceError("Claude emitted too many settings events")


async def _await_terminal(
    session: ClientProcessSession,
    codec: ClaudeStreamCodec,
    init: Mapping[str, object] | None,
    settings: Mapping[str, object],
    installation: ClaudeInstallation,
    scope: ClaudeConformanceScope,
    session_id: str,
    configuration_admitted: asyncio.Event,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    for _ in range(512):
        frame = await session.receive()
        if not isinstance(frame, Mapping):
            raise ClaudeConformanceError("Claude ended before terminal output")
        if frame.get("type") == "system" and frame.get("subtype") == "init":
            if init is not None:
                raise ClaudeConformanceError("duplicate Claude initialization metadata")
            if not codec.handshake_complete:
                raise ClaudeConformanceError("Claude initialized before MCP handshake completion")
            init = frame
            _validate_configuration(init, settings, installation, scope, session_id)
            configuration_admitted.set()
            continue
        if init is None and not claude_preinit_handshake_frame_is(frame):
            raise ClaudeConformanceError("Claude emitted an event before initialization metadata")
        reply = await codec.receive(json.dumps(frame, separators=(",", ":")))
        if reply is not None:
            await session.send(reply)
        if codec.terminal is not None:
            if init is None:
                raise ClaudeConformanceError("Claude omitted initialization metadata")
            return codec.terminal, init
    raise ClaudeConformanceError("Claude emitted too many turn events")


def _is_control_response(frame: Mapping[str, object], request_id: str) -> bool:
    response = frame.get("response")
    return (
        frame.get("type") == "control_response"
        and isinstance(response, Mapping)
        and response.get("request_id") == request_id
    )


def _validate_configuration(
    init: Mapping[str, object],
    settings: Mapping[str, object],
    installation: ClaudeInstallation,
    scope: ClaudeConformanceScope,
    session_id: str,
) -> None:
    if not claude_configuration_matches(
        init,
        settings,
        model=installation.model,
        effort=installation.effort,
        tools=frozenset(scope.tool_surface),
        session_id=session_id,
    ):
        raise ClaudeConformanceError("Claude initialized with an unisolated capability")


def _response_sequence(shell_marker: Path, read_sentinel: Path) -> tuple[bytes, ...]:
    forbidden = list(_forbidden_calls(shell_marker, read_sentinel))
    allowed = [
        (
            "forge-call-1",
            claude_tool_alias(ToolName.REPOSITORY_READ_FILE),
            {"path": "README.md"},
        )
    ]
    return (
        _tool_message("message-forbidden", forbidden),
        _tool_message("message-forge", allowed),
        _tool_message(
            "message-final",
            [("structured-output-1", "StructuredOutput", {"result": "offline complete"})],
        ),
    )


def _tool_message(message_id: str, tools: Sequence[tuple[str, str, Mapping[str, object]]]) -> bytes:
    events: list[Mapping[str, object]] = [_message_start(message_id)]
    for index, (call_id, name, arguments) in enumerate(tools):
        events.extend(
            (
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(arguments, separators=(",", ":")),
                    },
                },
                {"type": "content_block_stop", "index": index},
            )
        )
    events.extend((_message_delta("tool_use"), {"type": "message_stop"}))
    return _sse(events)


def _message_start(message_id: str) -> Mapping[str, object]:
    return {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        },
    }


def _message_delta(reason: str) -> Mapping[str, object]:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": reason, "stop_sequence": None},
        "usage": {"output_tokens": 1},
    }


def _sse(events: Sequence[Mapping[str, object]]) -> bytes:
    return "".join(
        "event: "
        + cast(str, event["type"])
        + "\ndata: "
        + json.dumps(event, separators=(",", ":"), ensure_ascii=True)
        + "\n\n"
        for event in events
    ).encode("utf-8")


def _inspect_requests(
    requests: Sequence[_HttpRequest], scope: ClaudeConformanceScope
) -> tuple[bool, bool, bool]:
    if len(requests) != 3:
        raise ClaudeConformanceError("Claude Messages request count differs")
    expected = {claude_tool_alias(tool) for tool in scope.tool_surface} | {"StructuredOutput"}
    tools_match = True
    for request in requests:
        if not request.path.startswith("/v1/messages") or request.body.get("model") != scope.model:
            raise ClaudeConformanceError("Claude provider request identity differs")
        tools = request.body.get("tools")
        names = (
            {
                tool.get("name")
                for tool in tools
                if isinstance(tool, Mapping) and tool.get("type") in (None, "custom")
            }
            if isinstance(tools, list)
            else set()
        )
        tools_match = tools_match and names == expected and len(names) == len(tools or ())
    if not tools_match:
        raise ClaudeConformanceError("Claude exposed a non-Forge model tool")

    denied = _tool_results(requests[1].body)
    if not _forbidden_denials_are_exact(denied):
        raise ClaudeConformanceError("Claude admitted a forbidden model tool")
    forwarded = _tool_results(requests[2].body).get("forge-call-1")
    content = forwarded.get("content") if isinstance(forwarded, Mapping) else None
    text = (
        content[0].get("text")
        if isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], Mapping)
        and content[0].get("type") == "text"
        else None
    )
    callback_forwarded = isinstance(text, str) and json.loads(text) == {
        "path": "README.md",
        "status": "succeeded",
    }
    if not callback_forwarded:
        raise ClaudeConformanceError("Claude callback result forwarding differs")
    alternate_auth_isolated = all(
        request.headers.get("x-api-key") == _OFFLINE_API_KEY
        and not {"authorization", "cookie", "proxy-authorization"}.intersection(request.headers)
        for request in requests
    )
    if not alternate_auth_isolated:
        raise ClaudeConformanceError("Claude used an alternate authentication route")
    return tools_match, callback_forwarded, alternate_auth_isolated


def _forbidden_denials_are_exact(denied: Mapping[str, Mapping[str, Any]]) -> bool:
    expected_ids = {f"forbidden-{index}" for index in range(1, len(_FORBIDDEN_TOOL_NAMES) + 1)}
    if set(denied) != expected_ids:
        return False
    for index, name in enumerate(_FORBIDDEN_TOOL_NAMES, start=1):
        result = denied[f"forbidden-{index}"]
        unavailable = f"<tool_use_error>Error: No such tool available: {name}"
        if not name.startswith("mcp__"):
            unavailable += f". {name} is disabled for this session, in subagents as well as here."
        unavailable += "</tool_use_error>"
        if (
            result.get("type") != "tool_result"
            or result.get("tool_use_id") != f"forbidden-{index}"
            or result.get("is_error") is not True
            or result.get("content") != unavailable
        ):
            return False
    return True


def _forbidden_calls(
    shell_marker: Path, read_sentinel: Path
) -> tuple[tuple[str, str, Mapping[str, object]], ...]:
    command = (
        f'cmd.exe /d /c echo denied>"{shell_marker}"'
        if os.name == "nt"
        else f"/bin/sh -c \"printf denied > '{shell_marker}'\""
    )
    arguments: tuple[Mapping[str, object], ...] = (
        {"command": command},
        {"file_path": str(read_sentinel)},
        {"skill": "inherited-canary"},
        {"url": "http://127.0.0.1:9/forbidden", "prompt": "denied"},
        {"path": str(shell_marker)},
    )
    return tuple(
        (f"forbidden-{index}", name, value)
        for index, (name, value) in enumerate(
            zip(_FORBIDDEN_TOOL_NAMES, arguments, strict=True), start=1
        )
    )


def _tool_results(body: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return {}
    results: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    results[tool_use_id] = block
    return results


@dataclass(frozen=True, slots=True)
class _HttpRequest:
    path: str
    headers: Mapping[str, str]
    body: Mapping[str, Any]


class _LoopbackMessages:
    def __init__(self, responses: Sequence[bytes]) -> None:
        self._responses = tuple(responses)
        self._server: asyncio.Server | None = None
        self._error: ClaudeConformanceError | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.requests: list[_HttpRequest] = []
        self.probes: list[str] = []
        self.base_url = ""

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=65_536)
        socket = self._server.sockets[0]
        self.base_url = f"http://127.0.0.1:{socket.getsockname()[1]}"
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._server.close()
        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*tuple(self._handlers), return_exceptions=True)
        async with asyncio.timeout(_LOOPBACK_REQUEST_SECONDS):
            await self._server.wait_closed()

    def assert_complete(self) -> None:
        if self._error is not None:
            raise self._error
        if len(self.requests) != len(self._responses):
            raise ClaudeConformanceError("Claude did not complete the loopback sequence")
        if self.probes not in ([], ["/api/hello"]):
            raise ClaudeConformanceError("Claude emitted an unexpected loopback probe")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        self._writers.add(writer)
        try:
            async with asyncio.timeout(_LOOPBACK_REQUEST_SECONDS):
                request_line = await reader.readline()
                components = request_line.decode("ascii", "strict").strip().split()
                if len(components) != 3 or components[0] not in {"HEAD", "POST"}:
                    raise ClaudeConformanceError(
                        "Claude loopback request is invalid: " + repr(components[:2])
                    )
                headers: dict[str, str] = {}
                header_bytes = len(request_line)
                while line := await reader.readline():
                    header_bytes += len(line)
                    if header_bytes > 65_536:
                        raise ClaudeConformanceError("Claude loopback headers exceed the bound")
                    if line == b"\r\n":
                        break
                    name, separator, value = line.decode("ascii", "strict").partition(":")
                    if not separator or name.lower() in headers:
                        raise ClaudeConformanceError("Claude loopback headers are invalid")
                    headers[name.lower()] = value.strip()
                if components[0] == "HEAD":
                    if components[1] != "/api/hello" or "content-length" in headers:
                        raise ClaudeConformanceError("Claude loopback probe is invalid")
                    self.probes.append(components[1])
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    return
                length = int(headers.get("content-length", "-1"))
                if length < 0 or length > _MAX_HTTP_BYTES:
                    raise ClaudeConformanceError("Claude loopback body exceeds the bound")
                raw = await reader.readexactly(length)
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise ClaudeConformanceError("Claude loopback body is invalid")
                request = _HttpRequest(components[1], headers, body)
                index = len(self.requests)
                self.requests.append(request)
                if index >= len(self._responses):
                    raise ClaudeConformanceError("Claude emitted an extra loopback request")
                response = self._responses[index]
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    + f"Content-Length: {len(response)}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n"
                    + response
                )
                await writer.drain()
        except (ClaudeConformanceError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            self._error = (
                error
                if isinstance(error, ClaudeConformanceError)
                else ClaudeConformanceError("Claude loopback request is invalid")
            )
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            try:
                await writer.drain()
            except OSError:
                pass
        except asyncio.IncompleteReadError, OSError, TimeoutError:
            self._error = ClaudeConformanceError("Claude loopback transport failed")
        finally:
            writer.close()
            try:
                async with asyncio.timeout(_LOOPBACK_REQUEST_SECONDS):
                    await writer.wait_closed()
            except OSError, TimeoutError:
                pass
            self._writers.discard(writer)
            self._handlers.discard(task)


__all__ = [
    "ClaudeConformanceError",
    "ClaudeConformanceResult",
    "ClaudeConformanceScope",
    "ClaudeOfficialConformanceHarness",
    "required_claude_live_scopes",
]
