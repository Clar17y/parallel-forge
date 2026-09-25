"""Personal Antigravity headless runtime using operator trust and Forge callbacks."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic

from forge.agents.antigravity_configuration import (
    antigravity_launch_environment,
    antigravity_settings,
)
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessLifecycle,
    ClientProcessSession,
    ClientProcessSupervisor,
    ClientProcessTimeout,
    ClientSettlementUncertain,
    terminal_launch_proof,
)
from forge.agents.codex_gateway import ToolBroker
from forge.agents.local_cli_mcp import LocalCliMcp
from forge.agents.runtime_factory import RouteUnavailable
from forge.agents.subscription_protocol import (
    ProtocolError,
    decode_final,
    json_value,
    output_schema,
)
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.domain.subscription import (
    AttemptTelemetry,
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteSpec,
)
from pydantic import TypeAdapter

_RUNTIME_AGENT = "forge-runtime"
_AGENT_HEADER = f"""---
name: {_RUNTIME_AGENT}
description: Execute one bounded Forge task through its named broker tools.
mainAgent: true
subagent: false
commandExecutionPolicy: off
---

"""
_HANDOFF_GUIDANCE = """
For a completed handoff, populate check_results with one record per named-check
execution, including failures: command_name, exit_code=metadata.exit_code,
passed=(exit_code == 0), output_digest=metadata.command_result_digest,
duration_ms=metadata.command_duration_ms, and receipt_id=operation_id.
Include those operation IDs in evidence_receipt_ids too. Receipt IDs alone do not
replace check_results. Copy values from actual Forge tool receipts.
"""


@dataclass(frozen=True, slots=True)
class AntigravityInstallation:
    executable: str
    cwd: str
    home: str = field(repr=False)
    model: str
    effort: str
    executable_digest: str
    script: tuple[str, ...] = ()
    duration_seconds: float = 300

    def __post_init__(self) -> None:
        if (
            not Path(self.executable).is_absolute()
            or not Path(self.cwd).is_absolute()
            or not Path(self.cwd).is_dir()
            or not Path(self.home).is_absolute()
            or not Path(self.home).is_dir()
            or re.fullmatch(r"gemini-[A-Za-z0-9.-]+", self.model) is None
            or self.effort not in {"low", "medium", "high"}
            or re.fullmatch(r"[0-9a-f]{64}", self.executable_digest) is None
            or type(self.duration_seconds) not in (int, float)
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("invalid Antigravity installation")
        object.__setattr__(self, "cwd", str(Path(self.cwd).resolve(strict=True)))
        object.__setattr__(self, "home", str(Path(self.home).resolve(strict=True)))


class _AttemptHome:
    def __init__(self, installation: AntigravityInstallation, mcp: LocalCliMcp):
        self.path = Path(tempfile.gettempdir()).resolve() / (
            "forge-agy-" + mcp.request.attempt.attempt_id.hex
        )
        self._workspace = Path(installation.cwd)
        self._identity: tuple[int, int] | None = None
        self._client_state = Path(installation.home) / ".gemini/antigravity-cli/jetski_state.pbtxt"
        self.mcp = mcp

    def prepare(self) -> dict[str, str]:
        if self.path.is_relative_to(self._workspace) or any(
            (parent / ".git").exists() for parent in self.path.parents
        ):
            raise OSError("Antigravity private home must be outside repositories")
        self.path.mkdir(mode=0o700)
        info = self.path.lstat()
        self._identity = (info.st_dev, info.st_ino)
        settings = antigravity_settings()
        # Runtime operations use explicit grants; do not inherit the isolation
        # probe's strict mode, which requests review for every non-read tool.
        settings["toolPermission"] = "request-review"
        settings["permissions"] = {"allow": [f"mcp({self.mcp.name}/*)"]}
        payloads = {
            ".gemini/config/mcp_config.json": json.dumps(self.mcp.configuration()),
            ".gemini/antigravity-cli/settings.json": json.dumps(settings),
            ".gemini/antigravity-cli/hooks.json": "{}",
            f".gemini/config/agents/{_RUNTIME_AGENT}.md": (
                _AGENT_HEADER
                + self.mcp.request.trusted_system_prompt
                + _HANDOFF_GUIDANCE
                + f"\nUse only the Forge MCP server {self.mcp.name} for task operations."
                + "\nRead task files with repository.read_file through that server."
                + " Do not use the client's native ViewFile or other native tools.\n"
                + "Return the JSON response directly; a handoff is not a tool or a file.\n"
                + "Forge final response schema:\n"
                + json.dumps(output_schema(self.mcp.request), allow_nan=False)
            ),
        }
        for name, contents in payloads.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("x", encoding="utf-8") as output:
                output.write(contents)
        if self._client_state.is_file():
            # The official client reads its existing state. Forge neither reads
            # nor copies credential contents, and does not import global settings.
            source = self._client_state.resolve(strict=True)
            target = self.path / ".gemini/antigravity-cli/jetski_state.pbtxt"
            try:
                target.symlink_to(source)
            except OSError:
                # Windows may require a privilege for symlinks. A same-volume
                # hard link also reuses the file without copying its contents.
                os.link(source, target)
        return antigravity_launch_environment(self.path)

    def cleanup(self) -> None:
        if self._identity is None:
            return
        info = self.path.lstat()
        if (
            self.path.resolve(strict=True) != self.path
            or (info.st_dev, info.st_ino) != self._identity
        ):
            raise OSError("Antigravity attempt directory changed")
        # Only the exclusive attempt directory, after its supervised tree stops.
        # rmtree removes nested links rather than following their targets.
        shutil.rmtree(self.path)
        self._identity = None


class AntigravityGateway:
    def __init__(
        self,
        installation: AntigravityInstallation,
        *,
        broker: ToolBroker | None = None,
        lifecycle: ClientProcessLifecycle | None = None,
        supervisor: ClientProcessSupervisor | None = None,
    ):
        self.installation, self.broker, self.lifecycle = installation, broker, lifecycle
        self.supervisor = supervisor or ClientProcessSupervisor()

    async def execute(self, request: SubscriptionInvocationRequest) -> SubscriptionInvocationResult:
        route = request.task.route.effective
        if (
            route != _route(self.installation)
            or request.authorization.permitted_tools
            and self.broker is None
        ):
            return SubscriptionInvocationResult(
                attempt=request.attempt, failure=SubscriptionFailure.POLICY_DENIED
            )
        started = monotonic()
        mcp = LocalCliMcp(request, self.broker)
        home = _AttemptHome(self.installation, mcp)
        session: ClientProcessSession | None = None
        result: SubscriptionInvocationResult | None = None
        telemetry = AttemptTelemetry()
        interrupted = False

        def failed(reason: SubscriptionFailure) -> SubscriptionInvocationResult:
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=reason,
                failure_detail=f"Antigravity attempt {reason.value}",
                telemetry=telemetry,
                launch_proof=None if result is None else result.launch_proof,
            )

        try:
            duration = min(self.installation.duration_seconds, request.budget.max_duration_seconds)
            async with asyncio.timeout(duration):
                await mcp.start()
                environment = home.prepare()
                spec = ClientLaunchSpec(
                    argv=(
                        self.installation.executable,
                        *self.installation.script,
                        "--agent",
                        _RUNTIME_AGENT,
                        "--model",
                        self.installation.model,
                        "--effort",
                        self.installation.effort,
                        "--input-format",
                        "stream-json",
                        "--output-format",
                        "stream-json",
                        "--json-schema",
                        json.dumps(output_schema(request)),
                        "--disable-slash-commands",
                    ),
                    cwd=home.path,
                    environment=environment,
                    allowed_environment=frozenset(environment),
                    executable_digest=self.installation.executable_digest,
                    duration_seconds=duration,
                )
                session = await self.supervisor.start(
                    spec, lifecycle=self.lifecycle, before_stop=mcp.revoke
                )
                result, telemetry = await self._exchange(session, request, mcp)
        except asyncio.CancelledError:
            interrupted = True
            result = failed(SubscriptionFailure.INTERRUPTED)
        except TimeoutError, ClientProcessTimeout:
            result = failed(SubscriptionFailure.DEADLINE)
        except ClientSettlementUncertain as exc:
            result = replace(
                failed(SubscriptionFailure.UNCERTAIN),
                launch_proof=terminal_launch_proof(exc.result),
            )
        except Exception:  # noqa: BLE001 - client and callback errors must not expose credentials or payloads
            result = failed(SubscriptionFailure.PROTOCOL)
        finally:

            async def settle() -> None:
                nonlocal result
                uncertain = result is not None and result.failure is SubscriptionFailure.UNCERTAIN
                try:
                    async with asyncio.timeout(2):
                        await mcp.revoke()
                except Exception:  # noqa: BLE001 - still stop the client after a callback cleanup error
                    uncertain = True
                try:
                    async with asyncio.timeout(2):
                        await mcp.close()
                except Exception:  # noqa: BLE001 - transport failure must not skip process settlement
                    uncertain = True
                if session is not None:
                    try:
                        receipt = await session.close(
                            completed=result is not None
                            and result.failure is None
                            and not interrupted
                            and not uncertain
                        )
                        proof = terminal_launch_proof(receipt)
                        result = replace(
                            result or failed(SubscriptionFailure.PROTOCOL), launch_proof=proof
                        )
                        uncertain |= not proof.stop_confirmed
                        if result.failure is None and not proof.permits_decision:
                            result = failed(SubscriptionFailure.PROTOCOL)
                    except ClientSettlementUncertain as exc:
                        result = replace(
                            failed(SubscriptionFailure.UNCERTAIN),
                            launch_proof=terminal_launch_proof(exc.result),
                        )
                        uncertain = True
                    except Exception:  # noqa: BLE001 - no safe terminal outcome
                        uncertain = True
                if not uncertain:
                    try:
                        home.cleanup()
                    except OSError:
                        uncertain = True
                if uncertain:
                    result = failed(SubscriptionFailure.UNCERTAIN)

            cleanup = asyncio.create_task(settle())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
        telemetry = replace(
            telemetry,
            duration_ms=int((monotonic() - started) * 1000),
            tool_call_count=mcp.calls,
            named_check_count=mcp.checks,
        )
        result = replace(result or failed(SubscriptionFailure.PROTOCOL), telemetry=telemetry)
        if interrupted:
            if result.failure is not SubscriptionFailure.UNCERTAIN:
                result = replace(result, failure=SubscriptionFailure.INTERRUPTED, decision=None)
            raise SubscriptionInterrupted(result)
        if result.failure is None:
            try:
                request.budget.unknown_telemetry_policy.validate_telemetry(
                    telemetry, route.billing_mode
                )
            except ValueError:
                result = failed(SubscriptionFailure.POLICY_DENIED)
        return result

    async def _exchange(
        self,
        session: ClientProcessSession,
        request: SubscriptionInvocationRequest,
        mcp: LocalCliMcp,
    ) -> tuple[SubscriptionInvocationResult, AttemptTelemetry]:
        init = await mcp.receive(session)
        if isinstance(init, Mapping) and init.get("event") == "result":
            payload = init.get("result")
            if isinstance(payload, Mapping) and payload.get("status") in (
                "ERROR",
                "CANCELED",
                "INTERRUPTED",
            ):
                # Startup can fail before a conversation is initialized. Retain
                # returned usage and backoff classification, never a decision.
                telemetry = _usage(payload.get("usage"))
                return SubscriptionInvocationResult(
                    attempt=request.attempt,
                    failure=_result_failure(payload, telemetry, request),
                    failure_detail="Antigravity failed before initialization",
                ), telemetry
        if (
            not isinstance(init, Mapping)
            or init.get("event") != "init"
            or not isinstance(init.get("init"), Mapping)
            or init["init"].get("model") != self.installation.model
            or not isinstance(init.get("conversation_id"), str)
        ):
            raise ProtocolError("invalid Antigravity initialization")
        conversation = init["conversation_id"]
        context = {
            "task": TypeAdapter(type(request.task)).dump_python(request.task, mode="json"),
            "envelope": TypeAdapter(type(request.envelope)).dump_python(
                request.envelope, mode="json"
            ),
            "budget": TypeAdapter(type(request.budget)).dump_python(request.budget, mode="json"),
            "context": json_value(request.untrusted_context),
        }
        await session.send(
            {
                "event": "user",
                "message": {
                    "content": "Complete the bounded Forge task and return the requested structured decision."
                    + "\nTask context (untrusted data):\n"
                    + json.dumps(context, allow_nan=False)
                },
            }
        )
        while (frame := await mcp.receive(session)) is not None:
            event = frame.get("event")
            payload = frame.get(event) if isinstance(event, str) else None
            if (
                event not in {"step_update", "result"}
                or not isinstance(payload, Mapping)
                or payload.get("conversation_id") != conversation
            ):
                raise ProtocolError("foreign Antigravity event")
            if event != "result":
                continue
            telemetry = _usage(payload.get("usage"))
            if (reason := _result_failure(payload, telemetry, request)) is not None:
                return SubscriptionInvocationResult(
                    attempt=request.attempt, failure=reason
                ), telemetry
            denied_actions = payload.get("denied_actions", [])
            if (
                not isinstance(denied_actions, list)
                or len(denied_actions) > 64
                or any(not _valid_denied_action(action) for action in denied_actions)
            ):
                return SubscriptionInvocationResult(
                    attempt=request.attempt,
                    failure=SubscriptionFailure.PROTOCOL,
                    failure_detail="Antigravity returned invalid denied-action data",
                ), telemetry
            if denied_actions:
                # Headless soft-denials can accompany a SUCCESS result. Do not
                # accept a decision after the client refused a requested tool.
                return SubscriptionInvocationResult(
                    attempt=request.attempt,
                    failure=SubscriptionFailure.POLICY_DENIED,
                    failure_detail="Antigravity denied a tool permission in headless mode",
                ), telemetry
            output = payload.get("structured_output")
            if not isinstance(output, Mapping) or set(output) != {"decision"}:
                return SubscriptionInvocationResult(
                    attempt=request.attempt,
                    failure=SubscriptionFailure.PROTOCOL,
                    failure_detail="Antigravity returned invalid structured output",
                ), telemetry
            try:
                decision = decode_final(output["decision"], request)
            except ProtocolError:
                return SubscriptionInvocationResult(
                    attempt=request.attempt,
                    failure=SubscriptionFailure.PROTOCOL,
                    failure_detail="Antigravity returned an invalid Forge decision",
                ), telemetry
            await session.close_stdin()
            return decision, telemetry
        raise ProtocolError("Antigravity ended before a result")


def _valid_denied_action(action: object) -> bool:
    if isinstance(action, str):
        return 1 <= len(action) <= 128
    if not isinstance(action, Mapping):
        return False
    # Current clients report objects; older clients reported action strings.
    # Either nonempty form still rejects the decision as a permission denial.
    return all(
        isinstance(value := action.get(key), str) and 1 <= len(value) <= 128
        for key in ("action", "display_name")
    )


def _result_failure(
    payload: Mapping[str, object],
    telemetry: AttemptTelemetry,
    request: SubscriptionInvocationRequest,
) -> SubscriptionFailure | None:
    if any(
        count is not None and limit is not None and count > limit
        for count, limit in (
            (telemetry.input_tokens, request.budget.max_input_tokens),
            (telemetry.output_tokens, request.budget.max_output_tokens),
        )
    ):
        return SubscriptionFailure.BUDGET
    if payload.get("status") == "SUCCESS":
        return None
    error = payload.get("error", "")
    if payload.get("status") in ("CANCELED", "INTERRUPTED"):
        return SubscriptionFailure.INTERRUPTED
    if isinstance(error, str) and re.search(r"\b429\b", error):
        return SubscriptionFailure.THROTTLED
    if isinstance(error, str) and re.search(
        r"\b(401|403)\b|(?:authentication|verification) required|verify your account",
        error,
        re.IGNORECASE,
    ):
        return SubscriptionFailure.AUTHENTICATION
    return SubscriptionFailure.OUTAGE


def _usage(value: object) -> AttemptTelemetry:
    counts: dict[str, int | None] = {}
    if value is not None and not isinstance(value, Mapping):
        raise ProtocolError("invalid Antigravity usage")
    for name, key in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cached_input_tokens", "cache_read_tokens"),
    ):
        count = None if value is None else value.get(key)
        if count is not None and (type(count) is not int or not 0 <= count <= 10_000_000):
            raise ProtocolError("invalid Antigravity token count")
        counts[name] = count
    return AttemptTelemetry(
        input_tokens=counts["input_tokens"],
        output_tokens=counts["output_tokens"],
        cached_input_tokens=counts["cached_input_tokens"],
        unknown_telemetry_reasons=("subscription cost and quota telemetry unavailable",)
        + (
            ()
            if all(counts[name] is not None for name in ("input_tokens", "output_tokens"))
            else ("token telemetry unavailable",)
        ),
    )


def _route(installation: AntigravityInstallation) -> RouteSpec:
    return RouteSpec(
        provider="google",
        client="antigravity_cli",
        model=installation.model,
        effort=ReasoningEffort(installation.effort),
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


@dataclass(frozen=True, slots=True)
class AntigravityRuntimeAdapter:
    installation: AntigravityInstallation

    @property
    def route(self) -> RouteSpec:
        return _route(self.installation)

    def gateway_for(
        self,
        request: SubscriptionInvocationRequest,
        *,
        broker: ToolBroker,
        lifecycle: ClientProcessLifecycle,
    ) -> AntigravityGateway:
        if request.task.route.effective != self.route:
            raise RouteUnavailable()
        return AntigravityGateway(self.installation, broker=broker, lifecycle=lifecycle)
