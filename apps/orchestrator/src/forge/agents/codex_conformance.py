"""Bounded, providerless official-client conformance for Codex routes."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self, cast

from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessSession,
    ClientProcessSupervisor,
    terminal_launch_proof,
)
from forge.agents.codex_gateway import (
    CODEX_MODEL_CATALOG_ARGUMENT,
    CodexInstallation,
    codex_configuration_arguments,
    codex_configuration_matches,
    codex_dynamic_tools,
    codex_isolation_configuration,
    codex_model_catalog_pin,
    codex_tool_alias,
)
from forge.agents.codex_verification import (
    CODEX_VERIFIER_ID,
    CODEX_VERIFIER_VERSION,
    CodexVerificationScope,
    codex_executable_digest,
    required_codex_verification_scopes,
)
from forge.agents.subscription_protocol import ProviderToolCall, tool_result_frame
from forge.domain.capability_evidence import capability_home_digest
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.tool import ToolName

_PROVIDER_ID = "forge_offline_conformance"
_GENERIC_FORBIDDEN_TOOLS = (
    "exec_command",
    "read_file",
    "apply_patch",
    "web_search",
    "request_user_input",
    "spawn_agent",
    "mcp__inherited_canary__touch",
)
_CUSTOM_FORBIDDEN_TOOLS = ("apply_patch", "functions.exec")
_FORBIDDEN_SURFACES = (
    *_GENERIC_FORBIDDEN_TOOLS,
    *(f"custom:{name}" for name in _CUSTOM_FORBIDDEN_TOOLS),
    "native:local_shell_call",
    "hosted:web_search_call",
    "hosted:image_generation_call",
    "mcpServer/tool/call",
)
_MAX_HTTP_BYTES = 1024 * 1024
_LOOPBACK_REQUEST_SECONDS = 2.0
_CREDENTIAL_HEADERS = frozenset(
    {
        "api-key",
        "authorization",
        "cookie",
        "openai-organization",
        "openai-project",
        "proxy-authorization",
        "x-api-key",
        "x-goog-api-key",
    }
)


class CodexConformanceError(RuntimeError):
    """The bounded offline client did not satisfy the closed conformance contract."""


CodexConformanceScope = CodexVerificationScope


def required_codex_live_scopes() -> tuple[CodexConformanceScope, ...]:
    """Return distinct scopes that require separate, explicitly authorized live proof."""

    return required_codex_verification_scopes()


@dataclass(frozen=True, slots=True)
class CodexConformanceResult:
    scope: CodexConformanceScope
    client_version: str
    executable_digest: str
    client_home_digest: str
    account: str
    environmentless: bool
    configuration_isolated: bool
    only_forge_tools_advertised: bool
    forbidden_tool_calls_denied: tuple[str, ...]
    callback_identity_bound: bool
    callback_result_forwarded: bool
    side_effects_absent: bool
    credentials_sent: bool
    terminal_proof: SubscriptionLaunchTerminalProof
    publishable: bool = field(default=False, init=False)
    live_provider_call: bool = field(default=False, init=False)

    def sanitized_payload(self) -> dict[str, object]:
        """Return the closed, path-free result that may be retained as an artifact."""

        return {
            "schema_version": 1,
            "kind": "codex_official_offline_conformance",
            "installation": {
                "account": self.account,
                "client_home_digest": self.client_home_digest,
                "client_version": self.client_version,
                "executable_digest": self.executable_digest,
            },
            "scope": {
                "name": self.scope.name,
                "model": self.scope.model,
                "effort": self.scope.effort,
                "role": self.scope.role.value,
                "tool_surface": [tool.value for tool in self.scope.tool_surface],
            },
            "verifier": {
                "id": CODEX_VERIFIER_ID,
                "version": CODEX_VERIFIER_VERSION,
            },
            "observations": {
                "callback_identity_bound": self.callback_identity_bound,
                "callback_result_forwarded": self.callback_result_forwarded,
                "configuration_isolated": self.configuration_isolated,
                "credentials_sent": self.credentials_sent,
                "environmentless": self.environmentless,
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
                "reason": "offline transport proof cannot satisfy account authentication, route identity, or subscription-route binding",
            },
        }


@dataclass(slots=True)
class CodexOfficialConformanceHarness:
    """Exercise the official binary against a loopback Responses fixture only."""

    side_effect_markers: Sequence[str | os.PathLike[str]] = field(default=(), repr=False)
    supervisor: ClientProcessSupervisor = field(default_factory=ClientProcessSupervisor, repr=False)

    def __post_init__(self) -> None:
        markers = tuple(Path(path).resolve() for path in self.side_effect_markers)
        if any(path.exists() for path in markers):
            raise ValueError("Codex conformance markers must not already exist")
        if not callable(getattr(self.supervisor, "start", None)):
            raise TypeError("Codex conformance requires a client supervisor")
        self.side_effect_markers = markers

    async def run(
        self, installation: CodexInstallation, scope: CodexConformanceScope
    ) -> CodexConformanceResult:
        if not isinstance(installation, CodexInstallation) or not isinstance(
            scope, CodexConformanceScope
        ):
            raise TypeError("Codex conformance requires an installation and scope")
        if (installation.model, installation.effort) != (scope.model, scope.effort):
            raise ValueError("Codex conformance scope differs from the installation")
        digest = await asyncio.to_thread(codex_executable_digest, installation.executable)
        if digest != installation.executable_digest:
            raise CodexConformanceError("Codex executable identity differs")

        shell_marker = Path(installation.cwd) / ".forge-codex-shell-canary"
        patch_marker = Path(installation.cwd) / ".forge-codex-patch-canary"
        code_marker = Path(installation.cwd) / ".forge-codex-code-canary"
        if any(marker.exists() for marker in (shell_marker, patch_marker, code_marker)):
            raise ValueError("Codex native conformance marker already exists")
        markers = (
            *cast(tuple[Path, ...], self.side_effect_markers),
            shell_marker,
            patch_marker,
            code_marker,
        )
        session: ClientProcessSession | None = None
        process_result = None
        protocol_complete = False
        async with _LoopbackResponses(
            _response_sequence(installation, shell_marker, patch_marker, code_marker)
        ) as responses:
            configuration = _offline_configuration(installation, responses.base_url)
            spec = ClientLaunchSpec(
                argv=(
                    installation.executable,
                    *installation.script,
                    *codex_configuration_arguments(configuration),
                ),
                cwd=installation.cwd,
                environment={"CODEX_HOME": installation.client_home},
                allowed_environment=frozenset({"CODEX_HOME"}),
                executable_digest=installation.executable_digest,
                pinned_files=(codex_model_catalog_pin(),),
                duration_seconds=installation.duration_seconds,
            )
            try:
                session = await self.supervisor.start(spec)
                configuration["model_catalog_json"] = session.pinned_path(
                    CODEX_MODEL_CATALOG_ARGUMENT
                )
                initialize = await _rpc(
                    session,
                    1,
                    "initialize",
                    {
                        "clientInfo": {"name": "forge-conformance", "version": "0.2"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                user_agent = initialize.get("userAgent")
                if not _user_agent_matches_client_version(user_agent, installation.client_version):
                    raise CodexConformanceError("Codex client version differs")
                await session.send({"method": "initialized", "params": {}})
                configured = await _rpc(
                    session,
                    2,
                    "config/read",
                    {"cwd": installation.cwd, "includeLayers": False},
                )
                effective_controls = codex_isolation_configuration(installation)
                effective_controls["model_provider"] = _PROVIDER_ID
                effective_controls["model_catalog_json"] = configuration["model_catalog_json"]
                if not codex_configuration_matches(
                    configured,
                    effective_controls,
                    allow_unreported_request_user_input=True,
                ):
                    raise CodexConformanceError("Codex effective configuration differs")
                thread = await _rpc(
                    session,
                    3,
                    "thread/start",
                    {
                        "model": installation.model,
                        "allowProviderModelFallback": False,
                        "environments": [],
                        "ephemeral": True,
                        "cwd": installation.cwd,
                        "config": configuration,
                        "baseInstructions": "Use only the supplied Forge dynamic tools.",
                        "developerInstructions": "This is a bounded offline isolation check.",
                        "dynamicTools": codex_dynamic_tools(scope.tool_surface),
                    },
                )
                thread_id = _object_id(thread.get("thread"))
                if thread.get("model") != installation.model:
                    raise CodexConformanceError("Codex thread model differs")
                await _rpc_denied(
                    session,
                    4,
                    "mcpServer/tool/call",
                    {
                        "server": "inherited_canary",
                        "threadId": thread_id,
                        "tool": "touch",
                        "arguments": {},
                    },
                )
                turn = await _rpc(
                    session,
                    5,
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": "Run the bounded callback check."}],
                        "model": installation.model,
                        "effort": installation.effort,
                        "environments": [],
                    },
                )
                turn_id = _object_id(turn.get("turn"))
                protocol_complete = await _complete_turn(session, thread_id, turn_id)
            finally:
                if session is not None:
                    process_result = await session.close(completed=protocol_complete)

            responses.assert_complete()
            tools_match, callback_forwarded, credentials_sent = _inspect_requests(
                responses.requests, scope
            )
            side_effects_absent = not any(marker.exists() for marker in markers)
            if not protocol_complete or not side_effects_absent or credentials_sent:
                raise CodexConformanceError("Codex native tool isolation differs")
        if process_result is None:
            raise CodexConformanceError("Codex process proof is unavailable")
        proof = terminal_launch_proof(process_result)
        if not proof.permits_decision:
            raise CodexConformanceError("Codex terminal process proof is incomplete")
        return CodexConformanceResult(
            scope=scope,
            client_version=installation.client_version,
            executable_digest=digest,
            client_home_digest=capability_home_digest(installation.client_home),
            account=installation.account,
            environmentless=True,
            configuration_isolated=True,
            only_forge_tools_advertised=tools_match,
            forbidden_tool_calls_denied=_FORBIDDEN_SURFACES,
            callback_identity_bound=True,
            callback_result_forwarded=callback_forwarded,
            side_effects_absent=side_effects_absent,
            credentials_sent=credentials_sent,
            terminal_proof=proof,
        )


def _user_agent_matches_client_version(user_agent: object, client_version: str) -> bool:
    if not isinstance(user_agent, str):
        return False
    return (
        re.search(
            rf"(?<![0-9.]){re.escape(client_version)}(?![0-9.])",
            user_agent,
        )
        is not None
    )


def _offline_configuration(installation: CodexInstallation, base_url: str) -> dict[str, object]:
    configuration = codex_isolation_configuration(installation)
    configuration.update(
        {
            "model_provider": _PROVIDER_ID,
            f"model_providers.{_PROVIDER_ID}.name": "Forge Offline OpenAI",
            f"model_providers.{_PROVIDER_ID}.base_url": base_url,
            f"model_providers.{_PROVIDER_ID}.wire_api": "responses",
            f"model_providers.{_PROVIDER_ID}.request_max_retries": 0,
            f"model_providers.{_PROVIDER_ID}.stream_max_retries": 0,
            f"model_providers.{_PROVIDER_ID}.supports_websockets": False,
            f"model_providers.{_PROVIDER_ID}.requires_openai_auth": False,
            f"model_providers.{_PROVIDER_ID}.supports_standalone_web_search": True,
        }
    )
    return configuration


async def _rpc(
    session: ClientProcessSession,
    ident: int,
    method: str,
    params: Mapping[str, object],
) -> Mapping[str, Any]:
    await session.send({"id": ident, "method": method, "params": dict(params)})
    for _ in range(64):
        frame = await session.receive()
        if not isinstance(frame, Mapping):
            raise CodexConformanceError("Codex ended before an expected response")
        if "id" not in frame:
            continue
        if (
            frame.get("id") != ident
            or "error" in frame
            or not isinstance(frame.get("result"), Mapping)
        ):
            raise CodexConformanceError("Codex emitted an unexpected response")
        return cast(Mapping[str, Any], frame["result"])
    raise CodexConformanceError("Codex emitted too many initialization events")


async def _rpc_denied(
    session: ClientProcessSession,
    ident: int,
    method: str,
    params: Mapping[str, object],
) -> None:
    await session.send({"id": ident, "method": method, "params": dict(params)})
    for _ in range(64):
        frame = await session.receive()
        if not isinstance(frame, Mapping):
            raise CodexConformanceError("Codex ended before an expected denial")
        if "id" not in frame:
            continue
        if (
            frame.get("id") != ident
            or "result" in frame
            or not isinstance(frame.get("error"), Mapping)
        ):
            raise CodexConformanceError("Codex admitted a forbidden control path")
        return
    raise CodexConformanceError("Codex emitted too many denial events")


async def _complete_turn(session: ClientProcessSession, thread_id: str, turn_id: str) -> bool:
    callback = False
    for _ in range(256):
        frame = await session.receive()
        if not isinstance(frame, Mapping):
            raise CodexConformanceError("Codex ended before turn completion")
        method, params = frame.get("method"), frame.get("params")
        if not isinstance(params, Mapping):
            raise CodexConformanceError("Codex emitted an invalid turn event")
        if method == "account/rateLimits/updated":
            if (
                "id" in frame
                or set(params) != {"rateLimits"}
                or not isinstance(params.get("rateLimits"), Mapping)
            ):
                raise CodexConformanceError("Codex emitted invalid account telemetry")
            continue
        if params.get("threadId") != thread_id:
            raise CodexConformanceError("Codex callback thread identity differs")
        item = params.get("item")
        if (
            method in {"item/started", "item/completed"}
            and isinstance(item, Mapping)
            and item.get("type")
            in {"commandExecution", "fileChange", "mcpToolCall", "collabToolCall"}
        ):
            raise CodexConformanceError("Codex executed an unadmitted native tool")
        if "id" in frame:
            if method != "item/tool/call" or callback:
                raise CodexConformanceError("Codex exposed an unadmitted tool callback")
            if (
                params.get("turnId") != turn_id
                or params.get("callId") != "forge-call-1"
                or params.get("namespace") != "forge"
                or params.get("tool") != codex_tool_alias(ToolName.REPOSITORY_READ_FILE)
                or params.get("arguments") != {"path": "README.md"}
            ):
                raise CodexConformanceError("Codex dynamic callback identity differs")
            call = ProviderToolCall(
                call_key="forge-call-1",
                thread_id=thread_id,
                turn_id=turn_id,
                name=ToolName.REPOSITORY_READ_FILE.value,
                arguments={"path": "README.md"},
            )
            await session.send(
                tool_result_frame(
                    call,
                    {"status": "succeeded", "path": "README.md"},
                    cast(str | int, frame["id"]),
                )
            )
            callback = True
            continue
        if method == "turn/completed":
            turn = params.get("turn")
            if (
                not isinstance(turn, Mapping)
                or _object_id(turn) != turn_id
                or turn.get("status") != "completed"
                or turn.get("error") is not None
                or not callback
            ):
                raise CodexConformanceError("Codex turn completion differs")
            return True
        if params.get("turnId") not in (None, turn_id):
            raise CodexConformanceError("Codex turn event identity differs")
    raise CodexConformanceError("Codex emitted too many turn events")


def _object_id(value: object) -> str:
    ident = value.get("id") if isinstance(value, Mapping) else None
    if type(ident) is not str or not ident or len(ident.encode("utf-8")) > 255:
        raise CodexConformanceError("Codex object identity is invalid")
    return ident


def _response_sequence(
    installation: CodexInstallation,
    shell_marker: Path,
    patch_marker: Path | None = None,
    code_marker: Path | None = None,
) -> tuple[bytes, ...]:
    patch_marker = patch_marker or shell_marker.with_name(".forge-codex-patch-canary")
    code_marker = code_marker or shell_marker.with_name(".forge-codex-code-canary")

    def marker_command(marker: Path) -> tuple[str, ...]:
        return (
            ("cmd.exe", "/d", "/c", f'echo denied>"{marker}"')
            if os.name == "nt"
            else ("/bin/sh", "-c", f"printf denied > '{marker}'")
        )

    shell_command = marker_command(shell_marker)
    command_text = " ".join(shell_command)
    code_command_text = " ".join(marker_command(code_marker))
    attempts: dict[str, Mapping[str, object]] = {
        "exec_command": {
            "cmd": command_text,
            "workdir": installation.cwd,
            "yield_time_ms": 500,
        },
        "read_file": {"path": "README.md"},
        "apply_patch": {
            "patch": f"*** Begin Patch\n*** Add File: {patch_marker.name}\n+denied\n*** End Patch"
        },
        "web_search": {"query": "must remain unavailable"},
        "request_user_input": {
            "questions": [
                {
                    "id": "forbidden",
                    "header": "Denied",
                    "question": "This native request must not execute.",
                    "options": [
                        {"label": "Stop (Recommended)", "description": "Remain isolated."},
                        {"label": "Continue", "description": "Must remain unavailable."},
                    ],
                }
            ]
        },
        "spawn_agent": {"message": "must not spawn", "task_name": "forbidden"},
        "mcp__inherited_canary__touch": {"path": str(shell_marker)},
    }
    forbidden_events = [_event_created("response-forbidden")]
    forbidden_events.extend(
        _event_function(f"forbidden-{index}", name, arguments)
        for index, (name, arguments) in enumerate(attempts.items(), start=1)
    )
    forbidden_events.extend(
        (
            _event_custom(
                "custom-patch",
                "apply_patch",
                f"*** Begin Patch\n*** Add File: {patch_marker.name}\n+denied\n*** End Patch",
            ),
            _event_custom(
                "custom-code",
                "functions.exec",
                "await tools.exec_command({cmd:" + json.dumps(code_command_text) + "})",
            ),
            _event_item(
                {
                    "type": "local_shell_call",
                    "id": "native-shell-item",
                    "call_id": "native-shell",
                    "status": "completed",
                    "action": {
                        "type": "exec",
                        "command": list(shell_command),
                        "working_directory": installation.cwd,
                    },
                }
            ),
            _event_item(
                {
                    "type": "web_search_call",
                    "id": "hosted-web",
                    "status": "completed",
                    "action": {"type": "search", "query": "must remain unavailable"},
                }
            ),
            _event_item(
                {
                    "type": "image_generation_call",
                    "id": "hosted-image",
                    "status": "completed",
                    "result": "",
                    "revised_prompt": None,
                }
            ),
        )
    )
    forbidden_events.append(_event_completed("response-forbidden"))
    dynamic = _sse(
        (
            _event_created("response-forge"),
            _event_function(
                "forge-call-1",
                codex_tool_alias(ToolName.REPOSITORY_READ_FILE),
                {"path": "README.md"},
                namespace="forge",
            ),
            _event_completed("response-forge"),
        )
    )
    final = _sse(
        (
            _event_created("response-final"),
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": "message-final",
                    "content": [{"type": "output_text", "text": "offline complete"}],
                },
            },
            _event_completed("response-final"),
        )
    )
    return _sse(tuple(forbidden_events)), dynamic, final


def _event_created(ident: str) -> dict[str, object]:
    return {"type": "response.created", "response": {"id": ident}}


def _event_completed(ident: str) -> dict[str, object]:
    return {
        "type": "response.completed",
        "response": {
            "id": ident,
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": None,
                "output_tokens": 0,
                "output_tokens_details": None,
                "total_tokens": 0,
            },
        },
    }


def _event_function(
    call_id: str,
    name: str,
    arguments: Mapping[str, object],
    *,
    namespace: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments, separators=(",", ":"), ensure_ascii=True),
    }
    if namespace is not None:
        item["namespace"] = namespace
    return _event_item(item)


def _event_custom(call_id: str, name: str, input_value: str) -> dict[str, object]:
    return _event_item(
        {
            "type": "custom_tool_call",
            "call_id": call_id,
            "name": name,
            "input": input_value,
        }
    )


def _event_item(item: Mapping[str, object]) -> dict[str, object]:
    return {"type": "response.output_item.done", "item": dict(item)}


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
    requests: Sequence[_HttpRequest], scope: CodexConformanceScope
) -> tuple[bool, bool, bool]:
    if len(requests) != 3:
        raise CodexConformanceError("Codex Responses request count differs")
    expected = {codex_tool_alias(tool) for tool in scope.tool_surface}
    tools_match = True
    for request in requests:
        if request.path != "/v1/responses" or request.body.get("model") != scope.model:
            raise CodexConformanceError("Codex provider request identity differs")
        reasoning = request.body.get("reasoning")
        if not isinstance(reasoning, Mapping) or reasoning.get("effort") != scope.effort:
            raise CodexConformanceError("Codex provider effort differs")
        direct_tools = request.body.get("tools")
        inputs = request.body.get("input")
        additional_tools = (
            [
                item.get("tools")
                for item in inputs
                if isinstance(item, Mapping) and item.get("type") == "additional_tools"
            ]
            if isinstance(inputs, list)
            else []
        )
        if direct_tools is None and len(additional_tools) == 1:
            tools = additional_tools[0]
        elif isinstance(direct_tools, list) and not additional_tools:
            tools = direct_tools
        else:
            tools = None
        namespace = tools[0] if isinstance(tools, list) and len(tools) == 1 else None
        nested = namespace.get("tools") if isinstance(namespace, Mapping) else None
        names = (
            {
                tool.get("name")
                for tool in nested
                if isinstance(tool, Mapping) and tool.get("type") == "function"
            }
            if isinstance(nested, list)
            else set()
        )
        tools_match = tools_match and (
            isinstance(namespace, Mapping)
            and namespace.get("type") == "namespace"
            and namespace.get("name") == "forge"
            and names == expected
            and isinstance(nested, list)
            and len(nested) == len(expected)
        )
    if not tools_match:
        raise CodexConformanceError("Codex exposed a non-Forge model tool")
    generic_denied = {
        name
        for index, name in enumerate(_GENERIC_FORBIDDEN_TOOLS, start=1)
        if _tool_output(requests[1].body, f"forbidden-{index}") == f"unsupported call: {name}"
    }
    custom_denied = {
        name
        for kind, name in (("patch", "apply_patch"), ("code", "functions.exec"))
        if _tool_output(requests[1].body, f"custom-{kind}")
        == f"unsupported custom tool call: {name}"
    }
    native_items = {
        value.get("type")
        for value in requests[1].body.get("input", [])
        if isinstance(value, Mapping)
    }
    native_denied = {
        "local_shell_call",
        "web_search_call",
        "image_generation_call",
    } <= native_items
    callback = _tool_output(requests[2].body, "forge-call-1")
    try:
        decoded_callback = json.loads(callback) if isinstance(callback, str) else callback
    except json.JSONDecodeError:
        decoded_callback = None
    forwarded = decoded_callback == {"status": "succeeded", "path": "README.md"}
    if (
        generic_denied != set(_GENERIC_FORBIDDEN_TOOLS)
        or custom_denied != set(_CUSTOM_FORBIDDEN_TOOLS)
        or not native_denied
        or not forwarded
    ):
        raise CodexConformanceError(
            "Codex tool result forwarding differs: "
            f"generic={len(generic_denied)}/{len(_GENERIC_FORBIDDEN_TOOLS)}, "
            f"custom={len(custom_denied)}/{len(_CUSTOM_FORBIDDEN_TOOLS)}, "
            f"native={native_denied}, callback={forwarded}"
        )
    credentials_sent = any(
        _CREDENTIAL_HEADERS.intersection(request.headers) for request in requests
    )
    return tools_match, forwarded, credentials_sent


def _tool_output(body: Mapping[str, Any], call_id: str) -> object | None:
    values = body.get("input")
    if not isinstance(values, list):
        return None
    for value in values:
        if (
            isinstance(value, Mapping)
            and value.get("type") in {"function_call_output", "custom_tool_call_output"}
            and value.get("call_id") == call_id
        ):
            return value.get("output")
    return None


@dataclass(frozen=True, slots=True)
class _HttpRequest:
    path: str
    headers: Mapping[str, str]
    body: Mapping[str, Any]


class _LoopbackResponses:
    def __init__(self, responses: Sequence[bytes]) -> None:
        self._responses = tuple(responses)
        self._server: asyncio.Server | None = None
        self._error: CodexConformanceError | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.requests: list[_HttpRequest] = []
        self.base_url = ""

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=65_536)
        socket = self._server.sockets[0]
        self.base_url = f"http://127.0.0.1:{socket.getsockname()[1]}/v1"
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
            raise CodexConformanceError("Codex did not complete the loopback sequence")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        self._writers.add(writer)
        try:
            async with asyncio.timeout(_LOOPBACK_REQUEST_SECONDS):
                request_line = await reader.readline()
                components = request_line.decode("ascii", "strict").strip().split()
                if len(components) != 3 or components[0] != "POST":
                    raise CodexConformanceError("Codex loopback request is invalid")
                headers: dict[str, str] = {}
                header_bytes = len(request_line)
                while line := await reader.readline():
                    header_bytes += len(line)
                    if header_bytes > 65_536:
                        raise CodexConformanceError("Codex loopback headers exceed the bound")
                    if line == b"\r\n":
                        break
                    name, separator, value = line.decode("ascii", "strict").partition(":")
                    if not separator or name.lower() in headers:
                        raise CodexConformanceError("Codex loopback headers are invalid")
                    headers[name.lower()] = value.strip()
                length = int(headers.get("content-length", "-1"))
                if length < 0 or length > _MAX_HTTP_BYTES:
                    raise CodexConformanceError("Codex loopback body exceeds the bound")
                raw = await reader.readexactly(length)
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise CodexConformanceError("Codex loopback body is invalid")
                request = _HttpRequest(components[1], headers, body)
                index = len(self.requests)
                self.requests.append(request)
                if index >= len(self._responses):
                    raise CodexConformanceError("Codex emitted an extra loopback request")
                response = self._responses[index]
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    + f"Content-Length: {len(response)}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n"
                    + response
                )
                await writer.drain()
        except (CodexConformanceError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            self._error = (
                error
                if isinstance(error, CodexConformanceError)
                else CodexConformanceError("Codex loopback request is invalid")
            )
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except asyncio.IncompleteReadError, OSError, TimeoutError:
            self._error = CodexConformanceError("Codex loopback transport failed")
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
    "CodexConformanceError",
    "CodexConformanceResult",
    "CodexConformanceScope",
    "CodexOfficialConformanceHarness",
    "required_codex_live_scopes",
]
