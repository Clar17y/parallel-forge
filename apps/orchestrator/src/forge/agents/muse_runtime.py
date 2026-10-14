"""Personal Muse Code headless runtime using operator trust and Forge callbacks."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID

from forge.agents.antigravity_runtime import _ObservationPublisher
from forge.agents.capability_verification import stable_executable_digest
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
from forge.agents.muse_approval import permission_hook_settings
from forge.agents.runtime_factory import RouteUnavailable
from forge.agents.subscription_protocol import (
    ProtocolError,
    RoleDecisionError,
    decode_final,
    json_value,
    output_schema,
    parse_json,
)
from forge.application.ports.subscription_gateway import (
    RoleDecisionRejection,
    SubscriptionFailure,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.application.ports.task_usage import TaskUsageObserver
from forge.domain.provider_quota import classify_muse_error
from forge.domain.subscription import (
    AttemptTelemetry,
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteSpec,
    SpecialistPurpose,
)
from forge.domain.task_usage_contract import TokenCounts, WorkUnitBinding
from pydantic import TypeAdapter

_ROUTE_EFFORT = {"none": ReasoningEffort.NONE, "low": ReasoningEffort.LOW,
                 "medium": ReasoningEffort.MEDIUM, "high": ReasoningEffort.HIGH,
                 "maximum": ReasoningEffort.MAXIMUM}

_HANDOFF_GUIDANCE = """
For a completed handoff, populate check_results with one record per named-check
execution, including failures: command_name, exit_code=metadata.exit_code,
passed=(exit_code == 0), output_digest=metadata.command_result_digest,
duration_ms=metadata.command_duration_ms, and receipt_id=operation_id.
Include those operation IDs in evidence_receipt_ids too. Receipt IDs alone do not
replace check_results. Copy values from actual Forge tool receipts.
After self-review, obtain git.diff with scope snapshot. Copy its
metadata.candidate_tree_digest and report only changed paths within your ownership.
For an implementation handoff, candidate_commit must be null without a git.commit receipt;
snapshot metadata.head_sha alone does not prove that your edits were committed.
An independent reviewer instead identifies the selected closed candidate HEAD.
evidence_receipt_ids contains the named-check operation IDs, the final snapshot
operation ID, and a git.commit operation ID only if you actually committed.
Do not include read, write, status or textual-diff operation IDs in that list.
"""

_PRIMARY_DECISION_GUIDANCE = """
The primary coordinates the run: do not return a task handoff. In planning,
return the plan; in work phases, choose a primary decision permitted by the
supplied schema. A plan for future implementation is not completed work.
When a work phase starts and known_tasks lists no child of your task, return
delegate to create the worker tasks that do the approved work; nothing runs
until you do. wait may only list task IDs of existing direct children from
known_tasks, never your own task_id.
"""


def _decision_guidance(purpose: SpecialistPurpose) -> str:
    return _PRIMARY_DECISION_GUIDANCE if purpose is SpecialistPurpose.PRIMARY else _HANDOFF_GUIDANCE


def resolve_muse_executable(executable: str) -> tuple[str, str]:
    """Resolve the installed native build without running the updater or shell."""
    path = Path(executable)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        if path.is_symlink():
            raise ValueError("Muse launcher link is unavailable")
        parent = path.resolve(strict=True).parent
        marker = parent / ".muse-version"
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 64:
            raise ValueError("Muse native version marker is unavailable")
        try:
            version = marker.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise ValueError("Muse native version marker is invalid") from None
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-R[0-9]+(?:\.[0-9]+)?", version) is None:
            raise ValueError("Muse native version marker is invalid")
        path = parent / f"muse-bin-{version}.exe"
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True).parent != parent:
            raise ValueError("Muse native binary is unavailable")
    digest = stable_executable_digest(str(path))
    if digest is None:
        raise ValueError("Muse native executable is unavailable")
    return str(path.resolve(strict=True)), digest


def _keyed_decision_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Key each decision branch by its kind so Muse can select any branch.

    Muse writes structured-output object keys in alphabetical order. With the
    discriminator as a sibling field, only branches whose first sorted key is
    ``kind`` (``wait``, ``plan``) remained reachable once ``kind`` was emitted,
    so primaries could never return ``delegate``. ``{"delegate": {...}}`` makes
    the branch the first and only key; ``_unkey_decision`` restores the shape.
    """
    keyed = deepcopy(dict(schema))
    decision = keyed.get("properties", {}).get("decision")
    if not isinstance(decision, dict) or not isinstance(decision.get("anyOf"), list):
        return keyed
    branches = []
    for branch in decision["anyOf"]:
        kind = branch.get("properties", {}).get("kind", {}).get("const")
        if not isinstance(kind, str):
            return deepcopy(dict(schema))
        inner = deepcopy(branch)
        inner["properties"].pop("kind")
        inner["required"] = [name for name in inner.get("required", []) if name != "kind"]
        branches.append(
            {
                "type": "object",
                "properties": {kind: inner},
                "required": [kind],
                "additionalProperties": False,
            }
        )
    decision["anyOf"] = branches
    return keyed


def _unkey_decision(value: Mapping[str, object]) -> Mapping[str, object]:
    """Undo ``_keyed_decision_schema``; an explicit ``kind`` passes through unchanged."""
    if "kind" in value or len(value) != 1:
        return value
    (kind, body), = value.items()
    if not isinstance(body, Mapping) or "kind" in body:
        return value
    return {"kind": kind, **body}


_MUSE_TOOL_NAME = re.compile(r"[^A-Za-z0-9_]")

def muse_launch_spec(
    installation: MuseInstallation, home: MuseAttemptHome | MuseAuthoringHome,
    *, mcp_name: str, mcp_server: Mapping[str, object], tools: tuple[str, ...], duration: float,
) -> ClientLaunchSpec:
    """Bind the installed client, configured account home, and exact Forge toolset."""
    # Muse exposes an MCP tool such as `repository.read_file` or
    # `validation-results.read` under a normalized name in which every character
    # outside [A-Za-z0-9_] becomes `_` (`mcp__<server>__validation_results_read`).
    # The original spelling is rejected as an unknown tool, so the toolset and
    # permission hook use Muse's names.
    tools = tuple(_MUSE_TOOL_NAME.sub("_", name) for name in tools)
    info = home.path.lstat()
    if (
        home._identity != (info.st_dev, info.st_ino)
        or home.path.resolve(strict=True) != home.path
    ):
        raise OSError("Muse private home changed before launch")
    server_config = dict(mcp_server)
    server_env = server_config.get("env")
    environment = home.environment()
    if not isinstance(server_env, Mapping):
        raise TypeError("Muse Forge server requires environment")
    forwarded: dict[str, str] = {}
    for key, value in server_env.items():
        if (not isinstance(key, str) or not key.startswith("FORGE_GEMINI_BRIDGE_")
            or not isinstance(value, str)):
            raise ValueError("invalid Muse Forge bridge environment")
        environment[key] = value
        forwarded[key] = "${" + key + "}"
    server_config["env"] = forwarded
    native, digest = resolve_muse_executable(
        installation.configured_executable or installation.executable
    )
    hooks: dict[str, object] = {}
    if tools:
        hooks = permission_hook_settings(
            home.path / "approval-policy.json", home.path, mcp_name, tools,
        )
    command = (
        native, *installation.script, "exec", "--json",
        "--prompt-file", str(home._prompt_file), "--output-schema", str(home._schema_file),
        "--workspace", str(home.path), "--trust-workspace", "--model", installation.model,
        "--reasoning-effort", "max" if installation.effort == "maximum" else installation.effort,
        "--max-model-steps", str(installation.max_model_steps),
        "--disable-shell", "--disable-write", "--disable-web-tools",
        "--approval-mode", "on-request", "--approval-judge", "off",
        "--no-foreign-personal-context", "--no-session-log", "--disable-reminders",
        "-c", "hooks=" + json.dumps(hooks, separators=(",", ":")),
        "-c", "mcp_servers." + mcp_name + "=" + json.dumps(server_config, separators=(",", ":")),
        "-c", "run.toolset=" + json.dumps(["mcp__" + mcp_name + "__" + name for name in tools]),
    )
    return ClientLaunchSpec(
        argv=command, cwd=home.path, environment=environment,
        allowed_environment=frozenset(environment), executable_digest=digest,
        duration_seconds=duration, protocol="jsonl",
    )


def _muse_environment(installation: MuseInstallation) -> dict[str, str]:
    # Account state stays at the configured home; no credential bytes are copied.
    home = Path(installation.home)
    env = {"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
    for key in ("PATH", "TEMP", "TMP", "TMPDIR", "LOCALAPPDATA"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


async def muse_root_terminal(
    receive: Callable[[], Awaitable[Mapping[str, object] | None]],
) -> Mapping[str, object]:
    """Select one native JSONL-v1 root terminal after consuming the whole stream."""
    root: tuple[str, str] | None = None
    terminal: Mapping[str, object] | None = None
    last_sequence = 0
    for _ in range(4096):
        frame = await receive()
        if frame is None:
            break
        if not isinstance(frame, Mapping) or frame.get("schema_version") != 1 or frame.get("payload_schema_version") != 1:
            raise ProtocolError("invalid Muse JSONL version")
        sequence = frame.get("sequence")
        if type(sequence) is not int or sequence <= last_sequence:
            raise ProtocolError("invalid Muse JSONL order")
        last_sequence = sequence
        event = frame.get("payload_type")
        payload = frame.get("payload")
        stream = frame.get("stream")
        if not isinstance(event, str) or not isinstance(payload, Mapping) or not isinstance(stream, Mapping):
            raise ProtocolError("invalid Muse JSONL frame")
        if event == "session.run.linked":
            run_stream = payload.get("run_stream")
            if (frame.get("record_type") != "event" or stream.get("kind") != "session"
                or payload.get("kind") != "session_run_linked" or not isinstance(stream.get("id"), str)
                or not isinstance(payload.get("command_id"), str) or not isinstance(run_stream, Mapping)
                or run_stream.get("kind") != "run" or run_stream.get("id") != payload.get("command_id")
                or root is not None):
                raise ProtocolError("invalid Muse root link")
            root = (stream["id"], payload["command_id"])
        elif event.startswith("run.terminal."):
            run_stream = payload.get("run_stream")
            if (frame.get("record_type") != "event" or stream.get("kind") != "session"
                or payload.get("kind") != "run_terminal" or not isinstance(run_stream, Mapping)
                or run_stream.get("kind") != "run" or run_stream.get("id") != payload.get("command_id")
                or event not in {"run.terminal.completed", "run.terminal.failed", "run.terminal.cancelled"}
                or payload.get("terminal") != event.removeprefix("run.terminal.")):
                raise ProtocolError("invalid Muse terminal")
            if root == (stream.get("id"), payload.get("command_id")):
                if terminal is not None:
                    raise ProtocolError("duplicate Muse root terminal")
                terminal = payload
    else:
        raise ProtocolError("Muse stream exceeded event bound")
    if root is None or terminal is None:
        raise ProtocolError("Muse ended before root terminal")
    return terminal


def muse_output(payload: Mapping[str, object]) -> Mapping[str, object]:
    raw = payload.get("text")
    if not isinstance(raw, str):
        raise ProtocolError("Muse output is missing")
    return parse_json(raw)


@dataclass(frozen=True, slots=True)
class MuseInstallation:
    executable: str
    cwd: str
    home: str = field(repr=False)
    model: str
    effort: str
    executable_digest: str
    account: str = "meta-account"
    client_version: str = "1.4.7"
    script: tuple[str, ...] = ()
    duration_seconds: float = 300
    max_model_steps: int = 100
    configured_executable: str | None = None

    def __post_init__(self) -> None:
        if (
            not Path(self.executable).is_absolute()
            or not Path(self.cwd).is_absolute()
            or not Path(self.cwd).is_dir()
            or not Path(self.home).is_absolute()
            or not Path(self.home).is_dir()
            or not self.model.strip() or self.model == "cli-default"
            or self.effort not in _ROUTE_EFFORT
            or re.fullmatch(r"[0-9a-f]{64}", self.executable_digest) is None
            or type(self.duration_seconds) not in (int, float)
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
            or type(self.max_model_steps) is not int
            or self.max_model_steps <= 0
            or (self.configured_executable is not None
                and not Path(self.configured_executable).is_absolute())
        ):
            raise ValueError("invalid Muse installation")
        object.__setattr__(self, "cwd", str(Path(self.cwd).resolve(strict=True)))
        object.__setattr__(self, "home", str(Path(self.home).resolve(strict=True)))


def _route(installation: MuseInstallation) -> RouteSpec:
    return RouteSpec(
        provider="meta",
        client="muse_code",
        model=installation.model,
        effort=_ROUTE_EFFORT[installation.effort],
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


def _cleanup_home(path: Path, identity: tuple[int, int] | None, changed: str) -> None:
    """Delete a prepared home visibly; raise (retaining retry) on any failure."""
    if identity is None:
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        return  # already removed: the deletion goal is met
    if path.resolve(strict=True) != path or (info.st_dev, info.st_ino) != identity:
        raise OSError(changed)
    shutil.rmtree(path)
    if path.exists():
        raise OSError("Muse private home deletion incomplete")


class MuseAttemptHome:
    def __init__(self, installation: MuseInstallation, mcp: LocalCliMcp):
        self.installation = installation
        self.mcp = mcp
        self.path = (
            Path(tempfile.gettempdir()).resolve() / ("forge-muse-" + mcp.request.attempt.attempt_id.hex)
        )
        self._workspace = Path(installation.cwd)
        self._identity: tuple[int, int] | None = None
        self._prompt_file = self.path / "prompt.txt"
        self._schema_file = self.path / "output_schema.json"

    def prepare(self) -> dict[str, str]:
        if self.path.is_relative_to(self._workspace) or any(
            (parent / ".git").exists() for parent in self.path.parents
        ):
            raise OSError("Muse private home must be outside repositories")
        self.path.mkdir(mode=0o700)
        info = self.path.lstat()
        self._identity = (info.st_dev, info.st_ino)

        # Write prompt file
        context = {
            "task": TypeAdapter(type(self.mcp.request.task)).dump_python(self.mcp.request.task, mode="json"),
            "envelope": TypeAdapter(type(self.mcp.request.envelope)).dump_python(
                self.mcp.request.envelope, mode="json"
            ),
            "budget": TypeAdapter(type(self.mcp.request.budget)).dump_python(self.mcp.request.budget, mode="json"),
            "context": json_value(self.mcp.request.untrusted_context),
        }
        prompt_content = (
            self.mcp.request.trusted_system_prompt
            + _decision_guidance(self.mcp.request.task.purpose)
            + f"\nUse only the Forge MCP server {self.mcp.name} for task operations."
            + "\nRead task files with repository.read_file through that server."
            + " Do not use native filesystem, shell, search, editing or subagent tools.\n"
            + "\nComplete the bounded Forge task and return the requested structured decision."
            + "\nForge final response schema:\n"
            + json.dumps(_keyed_decision_schema(output_schema(self.mcp.request)), allow_nan=False)
            + "\nTask context (untrusted data):\n"
            + json.dumps(context, allow_nan=False)
        )
        self._prompt_file.write_text(prompt_content, encoding="utf-8")

        # Write output schema file
        self._schema_file.write_text(
            json.dumps(_keyed_decision_schema(output_schema(self.mcp.request)), allow_nan=False),
            encoding="utf-8",
        )

        return self.environment()

    def environment(self) -> dict[str, str]:
        return _muse_environment(self.installation)

    def cleanup(self) -> None:
        _cleanup_home(self.path, self._identity, "Muse attempt directory changed")
        self._identity = None


class MuseAuthoringHome:
    """Private official-client settings for a read-only subject-authoring job."""

    def __init__(self, installation: MuseInstallation, attempt_id: UUID) -> None:
        self.installation = installation
        self.path = (
            Path(tempfile.gettempdir()).resolve() / ("forge-muse-author-" + attempt_id.hex)
        )
        self._identity: tuple[int, int] | None = None
        self._prompt_file = self.path / "prompt.txt"
        self._schema_file = self.path / "output_schema.json"

    def prepare(
        self,
        prompt: str,
        schema: Mapping[str, object],
    ) -> dict[str, str]:
        if self.path.is_relative_to(Path(self.installation.cwd)) or any(
            (parent / ".git").exists() for parent in self.path.parents
        ):
            raise OSError("Muse authoring home must be outside repositories")
        self.path.mkdir(mode=0o700)
        info = self.path.lstat()
        self._identity = (info.st_dev, info.st_ino)

        full_prompt = (
            prompt
            + "\nUse only Forge read-only MCP tools. Do not use native filesystem, shell, edit or subagent tools.\n"
        )
        self._prompt_file.write_text(full_prompt, encoding="utf-8")
        self._schema_file.write_text(json.dumps(schema), encoding="utf-8")

        return self.environment()

    def environment(self) -> dict[str, str]:
        return _muse_environment(self.installation)

    def cleanup(self) -> None:
        _cleanup_home(self.path, self._identity, "Muse authoring directory changed")
        self._identity = None


class MuseGateway:
    def __init__(
        self,
        installation: MuseInstallation,
        *,
        broker: ToolBroker | None = None,
        lifecycle: ClientProcessLifecycle | None = None,
        supervisor: ClientProcessSupervisor | None = None,
    ):
        self.installation = installation
        self.broker = broker
        self.lifecycle = lifecycle
        self.supervisor = supervisor or ClientProcessSupervisor()

    async def execute(self, request: SubscriptionInvocationRequest) -> SubscriptionInvocationResult:
        return await self._execute_internal(request)

    async def execute_observed(
        self,
        request: SubscriptionInvocationRequest,
        *,
        usage_binding: WorkUnitBinding,
        usage_sink: TaskUsageObserver,
    ) -> SubscriptionInvocationResult:
        from forge.agents.antigravity_runtime import _validate_subscription_usage_binding

        _validate_subscription_usage_binding(usage_binding, request, self.installation.model)
        if not hasattr(usage_sink, "observe_usage"):
            raise TypeError("usage sink must implement observe_usage")
        publisher = _ObservationPublisher(usage_binding, usage_sink)
        interrupted_exc: SubscriptionInterrupted | None = None
        try:
            try:
                result = await self._execute_internal(request, publisher=publisher)
            except SubscriptionInterrupted as exc:
                interrupted_exc = exc
                result = exc.result

            status = result.failure.value if result.failure else (
                "interrupted" if interrupted_exc is not None else "succeeded"
            )
            tokens = publisher.terminal_tokens or TokenCounts(
                input_tokens=result.telemetry.input_tokens,
                output_tokens=result.telemetry.output_tokens,
                cached_input_tokens=result.telemetry.cached_input_tokens,
            )
            await publisher.emit_final(
                tokens=tokens, status=status, context=publisher.terminal_context
            )
        finally:
            await publisher.close()

        if publisher.delivery_incomplete:
            result = replace(
                result,
                telemetry=replace(
                    result.telemetry,
                    unknown_telemetry_reasons=result.telemetry.unknown_telemetry_reasons
                    + ("usage observation delivery incomplete",),
                ),
            )
        if interrupted_exc is not None:
            raise SubscriptionInterrupted(result)
        return result

    async def _execute_internal(
        self,
        request: SubscriptionInvocationRequest,
        *,
        publisher: _ObservationPublisher | None = None,
    ) -> SubscriptionInvocationResult:
        route = request.task.route.effective
        if (
            route != _route(self.installation)
            or (request.authorization.permitted_tools and self.broker is None)
        ):
            return SubscriptionInvocationResult(
                attempt=request.attempt, failure=SubscriptionFailure.POLICY_DENIED
            )
        started = monotonic()
        mcp = LocalCliMcp(request, self.broker)
        home = MuseAttemptHome(self.installation, mcp)
        session: ClientProcessSession | None = None
        result: SubscriptionInvocationResult | None = None
        telemetry = AttemptTelemetry()
        interrupted = False

        def failed(reason: SubscriptionFailure, detail: str = "") -> SubscriptionInvocationResult:
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=reason,
                failure_detail=detail or f"Muse attempt {reason.value}",
                telemetry=telemetry,
                launch_proof=None if result is None else result.launch_proof,
                quota_exhaustion=None if result is None else result.quota_exhaustion,
            )

        try:
            duration = min(self.installation.duration_seconds, request.budget.max_duration_seconds)
            async with asyncio.timeout(duration):
                await mcp.start()
                home.prepare()
                config = mcp.configuration()["mcpServers"]
                assert isinstance(config, Mapping)
                server = config[mcp.name]
                assert isinstance(server, Mapping)
                spec = muse_launch_spec(
                    self.installation, home, mcp_name=mcp.name,
                    mcp_server={"transport": "stdio", **server},
                    tools=tuple(sorted(tool.value for tool in mcp.allowed))
                    if request.budget.max_tool_calls else (),
                    duration=duration,
                )
                session = await self.supervisor.start(
                    spec, lifecycle=self.lifecycle, before_stop=mcp.revoke
                )
                result, telemetry = await self._exchange(session, request, mcp, publisher=publisher)
        except asyncio.CancelledError:
            interrupted = True
            result = failed(SubscriptionFailure.INTERRUPTED)
        except (TimeoutError, ClientProcessTimeout):
            result = failed(SubscriptionFailure.DEADLINE)
        except ClientSettlementUncertain as exc:
            result = replace(
                failed(SubscriptionFailure.UNCERTAIN),
                launch_proof=terminal_launch_proof(exc.result),
            )
        except Exception:  # noqa: BLE001
            result = failed(SubscriptionFailure.PROTOCOL)
        finally:

            async def settle() -> None:
                nonlocal result
                uncertain = result is not None and result.failure is SubscriptionFailure.UNCERTAIN
                try:
                    async with asyncio.timeout(2):
                        await mcp.revoke()
                except Exception:  # noqa: BLE001
                    uncertain = True
                try:
                    async with asyncio.timeout(2):
                        await mcp.close()
                except Exception:  # noqa: BLE001
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
                        if result.failure is None and (
                            not proof.permits_decision or receipt.return_code != 0
                        ):
                            result = failed(SubscriptionFailure.PROTOCOL)
                        elif result.failure in {
                            SubscriptionFailure.QUOTA, SubscriptionFailure.THROTTLED,
                            SubscriptionFailure.AUTHENTICATION, SubscriptionFailure.OUTAGE,
                            SubscriptionFailure.POLICY_DENIED, SubscriptionFailure.INTERRUPTED,
                        } and receipt.return_code == 0 and not interrupted:
                            result = SubscriptionInvocationResult(
                                attempt=request.attempt, failure=SubscriptionFailure.PROTOCOL,
                                failure_detail="Muse failed terminal exited successfully",
                                telemetry=telemetry, launch_proof=proof,
                            )
                    except ClientSettlementUncertain as exc:
                        result = replace(
                            failed(SubscriptionFailure.UNCERTAIN),
                            launch_proof=terminal_launch_proof(exc.result),
                        )
                        uncertain = True
                    except Exception:  # noqa: BLE001
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
            unknown_telemetry_reasons=telemetry.unknown_telemetry_reasons
            + ("subscription cost and quota telemetry unavailable",),
        )
        result = replace(result or failed(SubscriptionFailure.PROTOCOL), telemetry=telemetry)
        if interrupted and result.failure is not SubscriptionFailure.UNCERTAIN:
            result = replace(result, failure=SubscriptionFailure.INTERRUPTED, decision=None)
        if interrupted:
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
        *,
        publisher: _ObservationPublisher | None = None,
    ) -> tuple[SubscriptionInvocationResult, AttemptTelemetry]:
        telemetry = AttemptTelemetry(
            unknown_telemetry_reasons=("subscription cost and quota telemetry unavailable",)
        )
        payload = await muse_root_terminal(lambda: mcp.receive(session))
        terminal_state = payload.get("terminal")
        reason = payload.get("reason")

        if terminal_state == "failed":
            failure_kind, quota_exc = classify_muse_error(reason if isinstance(reason, str) else None)
            fail_enum = {
                "quota": SubscriptionFailure.QUOTA,
                "throttled": SubscriptionFailure.THROTTLED,
                "authentication": SubscriptionFailure.AUTHENTICATION,
                "policy_denied": SubscriptionFailure.POLICY_DENIED,
                "unsupported": SubscriptionFailure.POLICY_DENIED,
                "outage": SubscriptionFailure.OUTAGE,
            }.get(failure_kind, SubscriptionFailure.OUTAGE)
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=fail_enum,
                failure_detail=f"Muse terminal failed: {fail_enum.value}",
                quota_exhaustion=quota_exc,
            ), telemetry

        if terminal_state == "cancelled":
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=SubscriptionFailure.INTERRUPTED,
                failure_detail="Muse terminal cancelled",
            ), telemetry

        if terminal_state != "completed":
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                failure_detail="Muse unknown terminal state",
            ), telemetry

        try:
            parsed = muse_output(payload)
        except ProtocolError:
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                failure_detail="Muse output is not valid JSON",
            ), telemetry

        # If wrapped under 'decision'
        decision_obj = parsed.get("decision")
        if isinstance(decision_obj, Mapping):
            decision_obj = _unkey_decision(decision_obj)
        if not isinstance(decision_obj, Mapping):
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                failure_detail="Muse decision payload is not an object",
            ), telemetry

        try:
            decision = decode_final(decision_obj, request)
        except RoleDecisionError as error:
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                role_rejection=RoleDecisionRejection.for_kind(error.kind),
            ), telemetry
        except ProtocolError:
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                failure_detail="Muse returned an invalid Forge decision",
            ), telemetry

        return decision, telemetry


@dataclass(frozen=True, slots=True)
class MuseRuntimeAdapter:
    installation: MuseInstallation

    @property
    def route(self) -> RouteSpec:
        return _route(self.installation)

    def gateway_for(
        self,
        request: SubscriptionInvocationRequest,
        *,
        broker: ToolBroker,
        lifecycle: ClientProcessLifecycle,
    ) -> MuseGateway:
        if request.task.route.effective != self.route:
            raise RouteUnavailable()
        return MuseGateway(self.installation, broker=broker, lifecycle=lifecycle)


__all__ = [
    "MuseAttemptHome",
    "MuseAuthoringHome",
    "MuseGateway",
    "MuseInstallation",
    "MuseRuntimeAdapter",
    "resolve_muse_executable",
]
