"""Supervised, isolated Claude stream-json subscription gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Protocol
from uuid import uuid4

from forge.agents.capability_verification import (
    capability_report,
    capability_scope,
    validate_installation_identity,
)
from forge.agents.claude_protocol import ClaudeStreamCodec
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessError,
    ClientProcessLifecycle,
    ClientProcessSession,
    ClientProcessSupervisor,
    ClientProcessTimeout,
    ClientSettlementUncertain,
    terminal_launch_proof,
)
from forge.agents.codex_gateway import ToolBroker
from forge.agents.subscription_protocol import (
    ProtocolError,
    ProviderToolCall,
    decode_final,
    freeze_context,
    json_value,
    output_schema,
    tool_input_schema,
)
from forge.application.ports.capability_evidence import CapabilityEvidenceSourceError
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionGateway,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.domain.capability_evidence import (
    CapabilityEvidenceScope,
    ResolvedCapabilityEvidence,
    capability_identity,
)
from forge.domain.provider_quota import (
    QuotaExhaustion,
    classify_claude_error,
    quota_evidence,
    utc_now,
)
from forge.domain.subscription import AttemptTelemetry, AuthMode, BillingMode, TaskBudget
from forge.domain.tool import ToolName
from pydantic import TypeAdapter

CLAUDE_CLIENT_VERSION = "2.1.263"
_ALLOWANCE_WINDOWS = frozenset({"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"})
_MANAGED_ISOLATION_SETTINGS: tuple[tuple[str, bool], ...] = (
    ("allowManagedHooksOnly", True),
    ("disableClaudeAiConnectors", True),
    ("disableCommandPluginSources", True),
    ("syncClaudeAiPlugins", False),
    ("syncClaudeAiSkills", False),
)
_TOOL_ALIASES = {
    tool: "mcp__forge__" + re.sub(r"[^A-Za-z0-9_-]", "_", tool.value) for tool in ToolName
}


class ClaudeConfigurationError(ValueError):
    """The connected client does not retain Forge's exact isolation controls."""


if len(set(_TOOL_ALIASES.values())) != len(_TOOL_ALIASES):  # pragma: no cover - enum invariant
    raise RuntimeError("Claude tool aliases must be unique")
CLAUDE_ISOLATION_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "client_version": CLAUDE_CLIENT_VERSION,
            "command_controls": (
                "restricted",
                "safe_mode",
                "empty_native_tools",
                "permission_prompts_none",
                "strict_sdk_mcp",
                "empty_setting_sources",
                "disabled_slash_commands",
                "disabled_chrome",
                "managed_policy_root_bound_to_client_home",
                "managed_policy_and_system_mcp_files_absent_before_launch",
                "effective_settings_before_user_turn",
                "exact_mcp_handshake_before_session_init",
                "callbacks_after_configuration_admission",
            ),
            "managed_settings": _MANAGED_ISOLATION_SETTINGS,
            "tool_aliases": sorted((tool.value, alias) for tool, alias in _TOOL_ALIASES.items()),
            "initialize_controls": {
                "hooks": None,
                "sdkMcpServers": ["forge"],
                "skills": [],
                "title": "Forge bounded review",
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def claude_managed_policy_is_empty(client_home: str) -> bool:
    """Reject every on-disk policy source that could still supply managed hooks."""

    if type(client_home) is not str or not Path(client_home).is_absolute():
        return False
    try:
        root = Path(client_home).resolve(strict=True)
    except OSError:
        return False
    return root.is_dir() and all(not os.path.lexists(path) for path in _managed_policy_paths(root))


def _claude_system_managed_mcp_paths() -> tuple[Path, ...]:
    """Return pinned-client system policy locations; env overrides never relocate these."""

    if os.name == "nt":
        roots = (
            os.environ.get("ProgramW6432"),
            os.environ.get("ProgramFiles"),
            r"C:\Program Files",
        )
        return tuple(
            dict.fromkeys(
                Path(root) / "ClaudeCode" / "managed-mcp.json"
                for root in roots
                if isinstance(root, str) and Path(root).is_absolute()
            )
        )
    if platform.system() == "Darwin":
        return (Path("/Library/Application Support/ClaudeCode/managed-mcp.json"),)
    if platform.system() == "Linux":
        return (Path("/etc/claude-code/managed-mcp.json"),)
    return ()


def _managed_policy_paths(client_home: Path) -> tuple[Path, ...]:
    return (
        client_home / "managed-settings.json",
        client_home / "managed-settings.d",
        client_home / "managed-mcp.json",
        *_claude_system_managed_mcp_paths(),
    )


def claude_initialize_request() -> dict[str, object]:
    """Return the closed SDK initialization controls shared by runtime and proof."""

    return {
        "subtype": "initialize",
        "hooks": None,
        "sdkMcpServers": ["forge"],
        "skills": [],
        "title": "Forge bounded review",
    }


def claude_managed_isolation_settings() -> dict[str, bool]:
    """Return the exact managed policy accepted by the pinned client."""

    return dict(_MANAGED_ISOLATION_SETTINGS)


def claude_tool_alias(tool: ToolName) -> str:
    """Return the exact model-facing name assigned by Claude to a Forge MCP tool."""

    if not isinstance(tool, ToolName):
        raise TypeError("Claude tool alias requires a known Forge tool")
    return _TOOL_ALIASES[tool]


def claude_configuration_matches(
    init: Mapping[str, object],
    settings: Mapping[str, object],
    *,
    model: str,
    effort: str,
    tools: frozenset[ToolName],
    session_id: str,
) -> bool:
    """Require the effective client state to exactly match Forge's launch policy."""

    return claude_settings_match(settings, model=model, effort=effort) and claude_init_matches(
        init, model=model, tools=tools, session_id=session_id
    )


def claude_settings_match(settings: Mapping[str, object], *, model: str, effort: str) -> bool:
    """Require the exact effective managed-policy response before a user turn."""

    expected_applied = {"advisor": None, "effort": effort, "model": model, "ultracode": False}
    required = claude_managed_isolation_settings()
    applied, effective, sources = (
        settings.get("applied"),
        settings.get("effective"),
        settings.get("sources"),
    )
    return (
        isinstance(applied, Mapping)
        and dict(applied) == expected_applied
        and isinstance(effective, Mapping)
        and dict(effective) == required
        and isinstance(sources, list)
        and len(sources) == 1
        and isinstance(sources[0], Mapping)
        and sources[0].get("source") == "policySettings"
        and isinstance(sources[0].get("settings"), Mapping)
        and dict(sources[0]["settings"]) == required
    )


def claude_init_matches(
    init: Mapping[str, object], *, model: str, tools: frozenset[ToolName], session_id: str
) -> bool:
    """Require the exact post-turn client capability advertisement."""

    expected_tools = {claude_tool_alias(tool) for tool in tools} | {"StructuredOutput"}
    advertised = init.get("tools")
    return (
        init.get("session_id") == session_id
        and init.get("claude_code_version") == CLAUDE_CLIENT_VERSION
        and init.get("model") == model
        and init.get("permissionMode") == "dontAsk"
        and isinstance(advertised, list)
        and all(type(tool) is str for tool in advertised)
        and set(advertised) == expected_tools
        and len(advertised) == len(expected_tools)
        and init.get("slash_commands") == []
        and init.get("skills") == []
        and init.get("plugins") == []
        and init.get("mcp_servers") == [{"name": "forge", "status": "connected"}]
    )


def _control_response_is(frame: Mapping[str, object], request_id: str) -> bool:
    response = frame.get("response")
    return isinstance(response, Mapping) and response.get("request_id") == request_id


def _claude_mcp_message(frame: Mapping[str, object]) -> Mapping[str, object] | None:
    request = frame.get("request")
    message = request.get("message") if isinstance(request, Mapping) else None
    if (
        set(frame) != {"type", "request_id", "request"}
        or frame.get("type") != "control_request"
        or not isinstance(frame.get("request_id"), str)
        or not frame["request_id"]
        or not isinstance(request, Mapping)
        or set(request) != {"subtype", "server_name", "message"}
        or request.get("subtype") != "mcp_message"
        or request.get("server_name") != "forge"
        or not isinstance(message, Mapping)
    ):
        return None
    return message


def claude_setup_handshake_frame_is(frame: Mapping[str, object]) -> bool:
    """Recognize only exact Forge MCP setup traffic before capability admission."""

    if claude_preinit_handshake_frame_is(frame):
        return True
    message = _claude_mcp_message(frame)
    if message is None:
        return False
    ident, params = message.get("id"), message.get("params")
    return (
        set(message) == {"jsonrpc", "id", "method", "params"}
        and message.get("jsonrpc") == "2.0"
        and message.get("method") == "initialize"
        and isinstance(ident, (str, int))
        and not isinstance(ident, bool)
        and isinstance(params, Mapping)
        and not set(params) - {"protocolVersion", "capabilities", "clientInfo"}
        and isinstance(params.get("protocolVersion"), str)
    )


def claude_preinit_handshake_frame_is(frame: Mapping[str, object]) -> bool:
    """Recognize only non-authoritative Forge MCP handshake envelopes."""

    message = _claude_mcp_message(frame)
    if message is None:
        return False
    return message == {"jsonrpc": "2.0", "method": "notifications/initialized"} or (
        set(message) in ({"jsonrpc", "method", "id"}, {"jsonrpc", "method", "id", "params"})
        and message.get("jsonrpc") == "2.0"
        and message.get("method") == "tools/list"
        and message.get("params", {}) == {}
        and isinstance(message.get("id"), (str, int))
        and not isinstance(message.get("id"), bool)
    )


def claude_launch_arguments(
    installation: ClaudeInstallation,
    *,
    session_id: str,
    system_prompt: str,
    permitted_tools: frozenset[ToolName],
    schema: Mapping[str, object],
) -> tuple[str, ...]:
    """Compose the one production-isolated Claude invocation shape."""

    names = [claude_tool_alias(tool) for tool in ToolName if tool in permitted_tools]
    return (
        *installation.script,
        "--verbose",
        "--restricted",
        "--safe-mode",
        "--model",
        installation.model,
        "--effort",
        installation.effort,
        "--session-id",
        session_id,
        "--no-session-persistence",
        "--system-prompt",
        system_prompt,
        "--tools=",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--disable-slash-commands",
        "--no-chrome",
        "--managed-settings",
        json.dumps(claude_managed_isolation_settings(), separators=(",", ":")),
        "--mcp-config",
        json.dumps({"mcpServers": {"forge": {"type": "sdk"}}}, separators=(",", ":")),
        "--strict-mcp-config",
        "--setting-sources=",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--allowed-tools=" + ",".join(names),
    )


@dataclass(frozen=True, slots=True)
class ClaudeInstallation:
    executable: str
    cwd: str
    model: str
    effort: str
    client_home: str = field(repr=False)
    account: str
    executable_digest: str
    script: tuple[str, ...] = (
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    )
    duration_seconds: float = 30.0
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    quota_limit_types: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not Path(self.executable).is_absolute() or not Path(self.cwd).is_dir():
            raise ValueError("Claude installation requires a trusted executable and directory")
        if (
            type(self.client_home) is not str
            or not Path(self.client_home).is_absolute()
            or not Path(self.client_home).is_dir()
        ):
            raise ValueError("Claude requires an explicit existing absolute client home")
        if self.environment or not all(
            type(x) is str and x and "\0" not in x for x in (self.model, self.effort, *self.script)
        ):
            raise ValueError("invalid isolated Claude installation")
        if (
            type(self.duration_seconds) not in (int, float)
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("Claude duration must be finite and positive")
        if (
            type(self.quota_limit_types) is not frozenset
            or not self.quota_limit_types <= _ALLOWANCE_WINDOWS
        ):
            raise ValueError("Claude quota binding requires known allowance windows")
        validate_installation_identity(self.account, self.executable_digest)
        object.__setattr__(self, "script", tuple(self.script))
        object.__setattr__(self, "cwd", str(Path(self.cwd).resolve(strict=True)))
        object.__setattr__(self, "client_home", str(Path(self.client_home).resolve(strict=True)))


@dataclass(frozen=True, slots=True)
class ClaudeCapabilityReport:
    installed_version: str | None = None
    subscription_auth: bool = False
    model: str | None = None
    effort: str | None = None
    builtins_disabled: bool = False
    hooks_disabled: bool = False
    strict_mcp: bool = False
    allowance_only_enforced: bool = False
    quota_limit_types: frozenset[str] = frozenset()
    client_home: str | None = field(default=None, repr=False)
    account: str | None = None
    executable_digest: str | None = None
    evidence: ResolvedCapabilityEvidence | None = field(default=None, repr=False)

    def admits(self, installation: ClaudeInstallation, scope: CapabilityEvidenceScope) -> bool:
        try:
            identity = capability_identity(
                scope=scope,
                client_version=CLAUDE_CLIENT_VERSION,
                executable_digest=installation.executable_digest,
                client_home=installation.client_home,
                account=installation.account,
            )
        except TypeError, ValueError:
            return False
        return (
            self.installed_version == CLAUDE_CLIENT_VERSION
            and self.subscription_auth is True
            and self.model == installation.model
            and self.effort == installation.effort
            and self.builtins_disabled is True
            and self.hooks_disabled is True
            and self.strict_mcp is True
            and self.allowance_only_enforced is True
            and self.client_home == installation.client_home
            and self.account == installation.account
            and self.executable_digest == installation.executable_digest
            and type(self.quota_limit_types) is frozenset
            and self.quota_limit_types == installation.quota_limit_types
            and isinstance(self.evidence, ResolvedCapabilityEvidence)
            and self.evidence.matches(identity)
            and self.evidence.permits(scope)
        )


class ClaudeCapabilityVerifier(Protocol):
    def verify(
        self, installation: ClaudeInstallation, scope: CapabilityEvidenceScope
    ) -> ClaudeCapabilityReport | Awaitable[ClaudeCapabilityReport]: ...


@dataclass(slots=True)
class _QuotaState:
    """Attempt-local evidence; durable ownership and recovery stay in PostgreSQL."""

    limit_types: frozenset[str]
    quota: QuotaExhaustion | None = None
    _terminal_observed_at: datetime | None = field(default=None, init=False)

    def notification(self, frame: Mapping[str, object], now: datetime) -> None:
        if frame.get("type") != "rate_limit_event":
            return
        info = frame["rate_limit_info"]
        assert isinstance(info, Mapping)  # The codec checked the session and shape.
        if info.get("status") != "rejected" or info.get("rateLimitType") not in self.limit_types:
            return
        # A verified window rejection is an allowance signal, unlike a generic
        # HTTP 429. Credit/overage fields and unknown windows carry no authority.
        self._retain(
            quota_evidence(
                reason="claude_account_usage_exhausted", now=now, reset_at=info.get("resetsAt")
            )
        )

    def terminal(self, terminal: Mapping[str, object], now: datetime) -> SubscriptionFailure:
        status = terminal.get("api_error_status")
        if status is not None and type(status) is not int:
            raise ProtocolError("invalid Claude API error status")
        if self._terminal_observed_at is None:
            self._terminal_observed_at = now
        # SDKResultSuccess with is_error=true puts its API error in result;
        # SDKResultError instead carries errors. Neither is accepted as prose
        # evidence on a successful turn.
        failure, quota = classify_claude_error(
            status=status,
            errors={"errors": terminal.get("errors"), "result": terminal.get("result")},
            # A relative reset belongs to this terminal text's first arrival,
            # which may follow an earlier window rejection with no known reset.
            now=self._terminal_observed_at,
        )
        if quota is not None:
            self._retain(quota)
        if self.quota is not None:
            if failure not in {"quota", "throttled"}:
                raise ProtocolError("conflicting Claude quota completion")
            return SubscriptionFailure.QUOTA
        return SubscriptionFailure(failure)

    def _retain(self, quota: QuotaExhaustion) -> None:
        if self.quota is None:
            self.quota = quota
        elif quota.reset_at is not None and (
            self.quota.reset_at is None or quota.reset_at > self.quota.reset_at
        ):
            # Sparse/allowed notices and later success cannot clear exhaustion.
            # Duplicate notices retain the first observation and longest reset.
            self.quota = replace(self.quota, reset_at=quota.reset_at)


class _Usage:
    def __init__(self) -> None:
        self.input: int | None = None
        self.output: int | None = None
        self.calls = 0
        self.checks = 0
        self._messages: dict[str, tuple[int, int]] = {}

    def validate_budget(self, budget: TaskBudget) -> None:
        if (
            budget.max_input_tokens is not None
            and self.input is not None
            and self.input > budget.max_input_tokens
        ) or (
            budget.max_output_tokens is not None
            and self.output is not None
            and self.output > budget.max_output_tokens
        ):
            raise ProtocolError("token budget exhausted")

    def observe(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        source = value.get("input_tokens", value.get("inputTokens"))
        target = value.get("output_tokens", value.get("outputTokens"))
        if source is None and target is None:
            return
        if (
            type(source) is not int
            or type(target) is not int
            or source < 0
            or target < 0
            or (self.input is not None and source < self.input)
            or (self.output is not None and target < self.output)
        ):
            raise ProtocolError("invalid Claude usage")
        self.input, self.output = source, target

    def observe_assistant(self, frame: Mapping[str, object], session_id: str) -> None:
        if frame.get("type") != "assistant":
            return
        message = frame.get("message")
        if not isinstance(message, Mapping) or message.get("usage") is None:
            return
        if frame.get("session_id") != session_id:
            raise ProtocolError("foreign Claude usage session")
        ident, value = message.get("id"), message["usage"]
        if type(ident) is not str or not ident or not isinstance(value, Mapping):
            raise ProtocolError("unidentified Claude usage")
        source, target = value.get("input_tokens"), value.get("output_tokens")
        if type(source) is not int or type(target) is not int or source < 0 or target < 0:
            raise ProtocolError("invalid Claude assistant usage")
        prior = self._messages.get(ident)
        if prior is not None and (source < prior[0] or target < prior[1]):
            raise ProtocolError("regressing Claude assistant usage")
        if prior is None and len(self._messages) >= 1024:
            raise ProtocolError("too many Claude usage messages")
        self._messages[ident] = (source, target)
        self.input = sum(item[0] for item in self._messages.values())
        self.output = sum(item[1] for item in self._messages.values())

    def telemetry(self, started: float) -> AttemptTelemetry:
        return AttemptTelemetry(
            input_tokens=self.input,
            output_tokens=self.output,
            duration_ms=int((monotonic() - started) * 1000),
            tool_call_count=self.calls,
            named_check_count=self.checks,
            unknown_telemetry_reasons=("subscription cost and quota telemetry unavailable",)
            + (() if self.input is not None else ("token telemetry unavailable",)),
        )


class ClaudeGateway(SubscriptionGateway):
    def __init__(
        self,
        installation: ClaudeInstallation,
        verifier: ClaudeCapabilityVerifier,
        *,
        broker: ToolBroker | None = None,
        supervisor: ClientProcessSupervisor | None = None,
        lifecycle: ClientProcessLifecycle | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._installation, self._verifier, self._broker = installation, verifier, broker
        self._supervisor = supervisor or ClientProcessSupervisor()
        self._lifecycle = lifecycle
        self._now = now or utc_now

    async def execute(self, request: SubscriptionInvocationRequest) -> SubscriptionInvocationResult:
        if type(request) is not SubscriptionInvocationRequest:
            raise TypeError("subscription request is required")
        scope = capability_scope(request)
        started, usage, session, interrupted = monotonic(), _Usage(), None, False
        quota = _QuotaState(self._installation.quota_limit_types)

        def failed(
            kind: SubscriptionFailure, quota_exhaustion: QuotaExhaustion | None = None
        ) -> SubscriptionInvocationResult:
            if quota_exhaustion is None:
                quota_exhaustion = quota.quota
            if quota_exhaustion is not None and kind not in {
                SubscriptionFailure.QUOTA,
                SubscriptionFailure.UNCERTAIN,
                SubscriptionFailure.INTERRUPTED,
                SubscriptionFailure.DEADLINE,
            }:
                kind = SubscriptionFailure.UNCERTAIN
            if (
                quota_exhaustion is None
                and result is not None
                and kind
                in {
                    SubscriptionFailure.UNCERTAIN,
                    SubscriptionFailure.INTERRUPTED,
                    SubscriptionFailure.DEADLINE,
                }
            ):
                quota_exhaustion = result.quota_exhaustion
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=kind,
                failure_detail=f"Claude attempt {kind.value}",
                quota_exhaustion=quota_exhaustion,
                telemetry=usage.telemetry(started),
            )

        async def revoke() -> None:
            if self._broker is not None:
                await self._broker.revoke()

        result: SubscriptionInvocationResult | None = None
        try:
            route = request.task.route.effective
            if (
                route.provider != "anthropic"
                or route.client != "claude_code"
                or route.model != self._installation.model
                or route.effort is None
                or route.effort.value != self._installation.effort
                or route.auth_mode is not AuthMode.SUBSCRIPTION
                or route.billing_mode is not BillingMode.ALLOWANCE_ONLY
            ):
                result = failed(SubscriptionFailure.POLICY_DENIED)
            elif (
                not (
                    await capability_report(
                        self._verifier.verify(self._installation, scope), ClaudeCapabilityReport
                    )
                ).admits(self._installation, scope)
                or not claude_managed_policy_is_empty(self._installation.client_home)
                or (request.authorization.permitted_tools and self._broker is None)
            ):
                result = failed(SubscriptionFailure.UNAVAILABLE)
            else:
                duration = min(
                    self._installation.duration_seconds, request.budget.max_duration_seconds
                )
                if duration <= 0:
                    raise ClientProcessTimeout("attempt duration exhausted")
                spec = ClientLaunchSpec(
                    argv=(self._installation.executable, *self._command(request)),
                    cwd=self._installation.cwd,
                    environment={
                        "CLAUDE_CONFIG_DIR": self._installation.client_home,
                        "CLAUDE_CODE_MANAGED_SETTINGS_PATH": self._installation.client_home,
                    },
                    allowed_environment=frozenset(
                        {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_MANAGED_SETTINGS_PATH"}
                    ),
                    executable_digest=self._installation.executable_digest,
                    duration_seconds=duration,
                )
                async with asyncio.timeout(duration):
                    session = await self._supervisor.start(
                        spec, lifecycle=self._lifecycle, before_stop=revoke
                    )
                    result = await self._exchange(session, request, usage, quota)
        except asyncio.CancelledError:
            interrupted = True
            result = failed(SubscriptionFailure.INTERRUPTED)
        except ClientProcessTimeout, TimeoutError:
            result = failed(SubscriptionFailure.DEADLINE)
        except ClientSettlementUncertain:
            result = failed(SubscriptionFailure.UNCERTAIN)
        except CapabilityEvidenceSourceError, ClaudeConfigurationError:
            result = failed(SubscriptionFailure.UNAVAILABLE)
        except ClientProcessError, ProtocolError, ValueError, TypeError, KeyError:
            result = failed(SubscriptionFailure.PROTOCOL)
        except Exception:  # noqa: BLE001 - provider/verifier failures are sanitized
            result = failed(SubscriptionFailure.PROTOCOL)
        finally:
            try:
                if session is not None:
                    receipt = await session.close(
                        completed=result is not None and result.failure is None and not interrupted
                    )
                    proof = terminal_launch_proof(receipt)
                    if not proof.stop_confirmed:
                        result = failed(SubscriptionFailure.UNCERTAIN)
                    elif (
                        result is not None and result.failure is None and not proof.permits_decision
                    ):
                        result = failed(SubscriptionFailure.PROTOCOL)
                    if result is not None:
                        result = replace(result, launch_proof=proof)
                else:
                    await revoke()
            except Exception:  # noqa: BLE001 - failed revocation makes settlement uncertain
                result = failed(SubscriptionFailure.UNCERTAIN)
        result = replace(
            result or failed(SubscriptionFailure.PROTOCOL), telemetry=usage.telemetry(started)
        )
        if interrupted:
            raise SubscriptionInterrupted(result)
        if result.failure is None:
            try:
                request.budget.unknown_telemetry_policy.validate_telemetry(
                    result.telemetry, route.billing_mode
                )
            except ValueError:
                return failed(SubscriptionFailure.POLICY_DENIED)
        return result

    def _command(self, request: SubscriptionInvocationRequest) -> tuple[str, ...]:
        return claude_launch_arguments(
            self._installation,
            session_id=str(request.attempt.attempt_id),
            system_prompt=request.trusted_system_prompt,
            permitted_tools=request.authorization.permitted_tools,
            schema=output_schema(request),
        )

    async def _exchange(
        self,
        session: ClientProcessSession,
        request: SubscriptionInvocationRequest,
        usage: _Usage,
        quota: _QuotaState,
    ) -> SubscriptionInvocationResult:
        thread, turn = str(request.attempt.attempt_id), str(uuid4())
        allowed = frozenset(tool.value for tool in request.authorization.permitted_tools)
        admitted: dict[str, tuple[ProviderToolCall, Mapping[str, object]]] = {}
        initialized = False
        init: Mapping[str, object] | None = None

        async def call(value: ProviderToolCall) -> Mapping[str, object]:
            if (
                not initialized
                or quota.quota is not None
                or value.name not in allowed
                or self._broker is None
            ):
                raise ProtocolError("unadmitted tool")
            previous = admitted.get(value.call_key)
            if previous is not None:
                if previous[0] != value:
                    raise ProtocolError("conflicting MCP replay")
                return previous[1]
            tool = ToolName(value.name)
            schema = tool_input_schema(tool)
            required, permitted = set(schema["required"]), set(schema["properties"])
            if not required <= set(value.arguments) <= permitted or any(
                type(v) is not str for v in value.arguments.values()
            ):
                raise ProtocolError("invalid tool arguments")
            if usage.calls >= request.budget.max_tool_calls or (
                tool is ToolName.BUILD_RUN_NAMED_CHECK
                and usage.checks >= request.budget.max_named_checks
            ):
                raise ProtocolError("tool budget exhausted")
            usage.calls += 1
            usage.checks += int(tool is ToolName.BUILD_RUN_NAMED_CHECK)
            receipt = freeze_context(await self._broker(value))
            admitted[value.call_key] = (value, receipt)
            return receipt

        codec = ClaudeStreamCodec(thread, turn, call, allowed)
        await session.send(
            {
                "type": "control_request",
                "request_id": "forge_initialize",
                "request": claude_initialize_request(),
            }
        )
        for _ in range(64):
            first = await session.receive()
            if not isinstance(first, Mapping):
                raise ProtocolError("Claude ended during initialization")
            if first.get("type") == "control_request":
                if not claude_setup_handshake_frame_is(first):
                    raise ProtocolError("Claude emitted a callback before capability admission")
                response = await codec.receive(json.dumps(first, allow_nan=False))
                if response is None:
                    raise ProtocolError("invalid Claude initialization request")
                await session.send(response)
                continue
            if first.get("type") != "control_response":
                raise ProtocolError("unexpected Claude initialization frame")
            response = first.get("response")
            if (
                not isinstance(response, Mapping)
                or response.get("subtype") != "success"
                or response.get("request_id") != "forge_initialize"
                or not isinstance(response.get("response"), Mapping)
            ):
                raise ProtocolError("Claude initialization failed")
            break
        else:
            raise ProtocolError("too many Claude initialization frames")
        await session.send(
            {
                "type": "control_request",
                "request_id": "forge_settings",
                "request": {"subtype": "get_settings"},
            }
        )
        settings: Mapping[str, object] | None = None
        for _ in range(128):
            frame = await session.receive()
            if not isinstance(frame, Mapping):
                raise ProtocolError("Claude ended before reporting settings")
            if frame.get("type") == "system" and frame.get("subtype") == "init":
                if init is not None:
                    raise ProtocolError("duplicate Claude initialization metadata")
                if not codec.handshake_complete:
                    raise ProtocolError("Claude initialized before MCP handshake completion")
                init = frame
                if settings is not None:
                    break
                continue
            if frame.get("type") == "control_response" and _control_response_is(
                frame, "forge_settings"
            ):
                response = frame["response"]
                assert isinstance(response, Mapping)
                value = response.get("response")
                if response.get("subtype") != "success" or not isinstance(value, Mapping):
                    raise ClaudeConfigurationError("Claude effective settings are unavailable")
                settings = value
                break
            if not claude_setup_handshake_frame_is(frame):
                raise ProtocolError("Claude emitted a callback before capability admission")
            reply = await codec.receive(json.dumps(frame, allow_nan=False))
            if reply is not None:
                await session.send(reply)
        else:
            raise ProtocolError("too many Claude settings events")
        if settings is None or not claude_settings_match(
            settings,
            model=self._installation.model,
            effort=self._installation.effort,
        ):
            raise ClaudeConfigurationError("Claude effective isolation differs")
        if init is not None:
            if not codec.handshake_complete:
                raise ProtocolError("Claude initialized before MCP handshake completion")
            if not claude_init_matches(
                init,
                model=self._installation.model,
                tools=request.authorization.permitted_tools,
                session_id=thread,
            ):
                raise ClaudeConfigurationError("Claude effective isolation differs")
            initialized = True
        context = {
            "task": TypeAdapter(type(request.task)).dump_python(request.task, mode="json"),
            "attempt_budget": TypeAdapter(type(request.budget)).dump_python(
                request.budget, mode="json"
            ),
            "context": json_value(request.untrusted_context),
        }
        await session.send(
            {
                "type": "user",
                "session_id": thread,
                "message": {
                    "role": "user",
                    "content": "Forge task context (untrusted):\n"
                    + json.dumps(context, allow_nan=False),
                },
            }
        )
        while (frame := await session.receive()) is not None:
            if not initialized:
                if claude_preinit_handshake_frame_is(frame):
                    reply = await codec.receive(json.dumps(frame, allow_nan=False))
                    if reply is None:
                        raise ProtocolError("invalid Claude initialization notification")
                    await session.send(reply)
                    continue
                if frame.get("type") != "system" or frame.get("subtype") != "init":
                    raise ProtocolError("Claude emitted an event before initialization metadata")
                if not codec.handshake_complete:
                    raise ProtocolError("Claude initialized before MCP handshake completion")
                if not claude_init_matches(
                    frame,
                    model=self._installation.model,
                    tools=request.authorization.permitted_tools,
                    session_id=thread,
                ):
                    raise ClaudeConfigurationError("Claude effective isolation differs")
                initialized = True
                continue
            if frame.get("type") == "system" and frame.get("subtype") == "init":
                raise ProtocolError("duplicate Claude initialization metadata")
            raw = json.dumps(frame, allow_nan=False)
            reply = await codec.receive(raw)
            quota.notification(frame, self._now())
            failure = None
            if codec.terminal is not None and codec.terminal.get("is_error"):
                # Capture confirmed evidence before optional usage validation or
                # supervisor settlement can fail.
                failure = quota.terminal(codec.terminal, self._now())
            usage.observe_assistant(frame, thread)
            usage.validate_budget(request.budget)
            if reply is not None:
                await session.send(reply)
            if codec.terminal is not None:
                if not initialized:
                    raise ProtocolError("Claude completed before initialization metadata")
                terminal = codec.terminal
                usage.observe(terminal.get("usage"))
                usage.validate_budget(request.budget)
                if failure is not None:
                    return SubscriptionInvocationResult(
                        attempt=request.attempt,
                        failure=failure,
                        quota_exhaustion=quota.quota,
                    )
                if quota.quota is not None:
                    raise ProtocolError("Claude completed after confirmed exhaustion")
                output = terminal.get("structured_output")
                if not isinstance(output, Mapping) or set(output) != {"decision"}:
                    raise ProtocolError("invalid Claude structured output")
                return decode_final(output["decision"], request)
        raise ProtocolError("Claude ended before terminal result")


__all__ = [
    "CLAUDE_CLIENT_VERSION",
    "CLAUDE_ISOLATION_POLICY_DIGEST",
    "ClaudeCapabilityReport",
    "ClaudeCapabilityVerifier",
    "ClaudeGateway",
    "ClaudeInstallation",
    "claude_initialize_request",
    "claude_launch_arguments",
    "claude_managed_isolation_settings",
    "claude_managed_policy_is_empty",
    "claude_setup_handshake_frame_is",
    "claude_tool_alias",
]
