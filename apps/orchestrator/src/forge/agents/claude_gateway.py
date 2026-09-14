"""Supervised, isolated Claude stream-json subscription gateway."""

from __future__ import annotations

import asyncio
import json
import math
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

_VERSION = "2.1.263"
_ALLOWANCE_WINDOWS = frozenset({"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"})


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
                client_version=_VERSION,
                executable_digest=installation.executable_digest,
                client_home=installation.client_home,
                account=installation.account,
            )
        except TypeError, ValueError:
            return False
        return (
            self.installed_version == _VERSION
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
            elif not (
                await capability_report(
                    self._verifier.verify(self._installation, scope), ClaudeCapabilityReport
                )
            ).admits(self._installation, scope) or (
                request.authorization.permitted_tools and self._broker is None
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
                    environment={"CLAUDE_CONFIG_DIR": self._installation.client_home},
                    allowed_environment=frozenset({"CLAUDE_CONFIG_DIR"}),
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
        except CapabilityEvidenceSourceError:
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
        names = [
            f"mcp__forge__{tool.value}"
            for tool in ToolName
            if tool in request.authorization.permitted_tools
        ]
        return (
            *self._installation.script,
            "--verbose",
            "--model",
            self._installation.model,
            "--effort",
            self._installation.effort,
            "--session-id",
            str(request.attempt.attempt_id),
            "--no-session-persistence",
            "--system-prompt",
            request.trusted_system_prompt,
            "--tools=",
            "--permission-mode",
            "dontAsk",
            "--mcp-config",
            json.dumps({"mcpServers": {"forge": {"type": "sdk"}}}, separators=(",", ":")),
            "--strict-mcp-config",
            "--setting-sources=",
            "--json-schema",
            json.dumps(output_schema(request), separators=(",", ":")),
            "--allowed-tools=" + ",".join(names),
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
                "request": {"subtype": "initialize", "hooks": None, "skills": []},
            }
        )
        for _ in range(64):
            first = await session.receive()
            if not isinstance(first, Mapping):
                raise ProtocolError("Claude ended during initialization")
            if first.get("type") == "control_request":
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
    "ClaudeCapabilityReport",
    "ClaudeCapabilityVerifier",
    "ClaudeGateway",
    "ClaudeInstallation",
]
