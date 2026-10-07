"""Production brainstorm gateway running subscription client subprocesses under ClientProcessSupervisor."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID, uuid4

from forge.agents.antigravity_runtime import AntigravityAuthoringHome, AntigravityInstallation
from forge.agents.capability_verification import stable_executable_digest
from forge.agents.claude_gateway import (
    CLAUDE_SUBSCRIPTION_LAUNCH_ENVIRONMENT,
    ClaudeGateway,
    ClaudeInstallation,
    _build_claude_launch_arguments,
)
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessError,
    ClientProcessSession,
    ClientProcessSupervisor,
    ClientProcessTimeout,
    ClientSettlementUncertain,
)
from forge.agents.codex_gateway import (
    CodexGateway,
    CodexInstallation,
)
from forge.agents.epic_brainstorm_protocol import (
    AUTHORING_TOOL_NAMES,
    AuthoringProviderFailure,
    AuthoringTools,
    AuthoringUsage,
    antigravity_exchange,
    authoring_schema,
    claude_exchange,
    codex_exchange,
    gemini_exchange,
)
from forge.agents.gemini_configuration import GeminiLaunchDirectory
from forge.agents.subscription_protocol import (
    ProtocolError,
)
from forge.application.ports.epic_brainstorm import (
    BrainstormGatewayResult,
    BrainstormProcessLifecycle,
)
from forge.domain.epic_brainstorm import AuthoringJobSnapshot, BrainstormProposal, BrainstormTurn
from forge.domain.epic_decomposition import DecompositionProposal
from forge.domain.local_cli import LocalCliTrust
from forge.domain.subscription import AttemptTelemetry
from forge.domain.subscription_installations import (
    AntigravityInstallationSpec,
    ClaudeInstallationSpec,
    CodexInstallationSpec,
    GeminiInstallationSpec,
    SubscriptionInstallationSpec,
)
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from pydantic import ValidationError


def _parse_telemetry(data: Mapping[str, Any], elapsed_ms: int) -> AttemptTelemetry:
    try:
        return AttemptTelemetry(
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            cached_input_tokens=data.get("cached_input_tokens"),
            duration_ms=data.get("duration_ms", elapsed_ms),
            tool_call_count=data.get("tool_call_count", 0),
            named_check_count=data.get("named_check_count", 0),
            estimated_api_cost_minor=data.get("estimated_api_cost_minor"),
            currency=data.get("currency"),
        )
    except TypeError, ValueError:
        return AttemptTelemetry(duration_ms=elapsed_ms)


class EpicBrainstormGateway:
    """Supervised brainstorm gateway communicating over standard stdin/stdout JSON frames."""

    def __init__(
        self,
        installation: SubscriptionInstallationSpec | None = None,
        *,
        supervisor: ClientProcessSupervisor | None = None,
        launch_spec: ClientLaunchSpec | None = None,
        trust: LocalCliTrust = LocalCliTrust.OPERATOR,
    ) -> None:
        self.installation = installation
        self.supervisor = supervisor or ClientProcessSupervisor()
        self.launch_spec = launch_spec
        self.trust = trust

    async def execute(
        self,
        job: AuthoringJobSnapshot,
        turns: tuple[BrainstormTurn, ...],
        reader: BrainstormReadOnlyTools,
        *,
        cancelled: Callable[[], Awaitable[bool]],
        lifecycle: BrainstormProcessLifecycle,
    ) -> BrainstormGatewayResult:
        if self.installation is not None:
            return await self._execute_official(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
        return await self._execute_inner(
            job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
        )

    async def _execute_official(
        self,
        job: AuthoringJobSnapshot,
        turns: tuple[BrainstormTurn, ...],
        reader: BrainstormReadOnlyTools,
        *,
        cancelled: Callable[[], Awaitable[bool]],
        lifecycle: BrainstormProcessLifecycle,
    ) -> BrainstormGatewayResult:
        if await cancelled():
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="cancelled")
        installation = self.installation
        assert installation is not None
        actual_digest = stable_executable_digest(installation.executable)
        if actual_digest is None or (
            self.trust is LocalCliTrust.VERIFIED
            and not hmac.compare_digest(actual_digest, installation.executable_digest)
        ):
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="unavailable")
        lifecycle_attempt = getattr(lifecycle, "attempt_id", None)
        attempt_id = lifecycle_attempt if isinstance(lifecycle_attempt, UUID) else uuid4()
        duration = float(job.budget.max_duration_seconds)
        started = time.monotonic()
        usage = AuthoringUsage()
        tools = AuthoringTools(reader, job.budget.max_tool_calls, usage)
        session = None
        launch_files: GeminiLaunchDirectory | AntigravityAuthoringHome | None = None
        proposal: BrainstormProposal | DecompositionProposal | None = None
        failure: str | None = None
        quota_reset_at: str | None = None
        interrupted = False
        use_bridge = False
        try:
            exchange: Callable[
                [ClientProcessSession], Awaitable[BrainstormProposal | DecompositionProposal]
            ]
            if isinstance(installation, CodexInstallationSpec):
                codex_runtime = CodexInstallation(
                    executable=installation.executable,
                    cwd=installation.cwd,
                    model=installation.model,
                    effort=installation.effort,
                    client_home=installation.home,
                    account=installation.account,
                    executable_digest=actual_digest,
                    client_version=installation.client_version,
                    quota_limit_id=installation.quota_limit_id,
                    duration_seconds=duration,
                    disabled_mcp_servers=installation.disabled_mcp_servers,
                )
                codex_gateway = CodexGateway(
                    codex_runtime, supervisor=self.supervisor, trust=self.trust
                )
                spec = ClientLaunchSpec(
                    argv=(codex_runtime.executable, *codex_gateway._command()),
                    cwd=codex_runtime.cwd,
                    environment={"CODEX_HOME": codex_runtime.client_home},
                    allowed_environment=frozenset({"CODEX_HOME"}),
                    executable_digest=codex_runtime.executable_digest,
                    duration_seconds=duration,
                )
                exchange = lambda session: codex_exchange(
                    session, codex_gateway, job, turns, tools, usage
                )
            elif isinstance(installation, ClaudeInstallationSpec):
                claude_runtime = ClaudeInstallation(
                    executable=installation.executable,
                    cwd=installation.cwd,
                    model=installation.model,
                    effort=installation.effort,
                    client_home=installation.home,
                    account=installation.account,
                    executable_digest=actual_digest,
                    client_version=installation.client_version,
                    duration_seconds=duration,
                    quota_limit_types=frozenset(installation.quota_limit_types),
                )
                claude_gateway = ClaudeGateway(
                    claude_runtime, supervisor=self.supervisor, trust=self.trust
                )
                claude_prompt = (
                    "You are a read-only epic decomposition assistant. Use only Forge read tools."
                    if job.kind == "decomposition"
                    else "You are a read-only epic brief authoring assistant. Use only Forge read tools."
                )
                arguments = _build_claude_launch_arguments(
                    script=claude_runtime.script,
                    model=claude_runtime.model,
                    effort=claude_runtime.effort,
                    session_id=str(job.job_id),
                    system_prompt=claude_prompt,
                    schema_json=json.dumps(authoring_schema(job.kind), separators=(",", ":")),
                    allowed_tools=",".join("mcp__forge__" + name for name in AUTHORING_TOOL_NAMES),
                )
                environment = {
                    "CLAUDE_CONFIG_DIR": claude_runtime.client_home,
                    **CLAUDE_SUBSCRIPTION_LAUNCH_ENVIRONMENT,
                }
                spec = ClientLaunchSpec(
                    argv=(claude_runtime.executable, *arguments),
                    cwd=claude_runtime.cwd,
                    environment=environment,
                    allowed_environment=frozenset(environment),
                    executable_digest=claude_runtime.executable_digest,
                    duration_seconds=duration,
                )
                exchange = lambda session: claude_exchange(
                    session, claude_gateway, job, turns, tools, usage
                )
            elif isinstance(installation, GeminiInstallationSpec):
                use_bridge = True
                launch_files = GeminiLaunchDirectory(installation.cwd, attempt_id)
                gemini_prompt = (
                    "Read-only epic decomposition. Use only Forge MCP read tools. Return the proposal schema."
                    if job.kind == "decomposition"
                    else "Read-only epic brief authoring. Use only Forge MCP read tools. Return the proposal schema."
                )
                environment = launch_files.prepare(
                    home=installation.home,
                    model=installation.model,
                    effort=installation.effort,
                    prompt=gemini_prompt,
                )
                spec = ClientLaunchSpec(
                    argv=(
                        installation.executable,
                        "--acp",
                        "--model",
                        installation.model,
                        "--allowed-mcp-server-names=forge",
                        "--extensions=none",
                    ),
                    cwd=str(launch_files.path),
                    environment=environment,
                    allowed_environment=frozenset(environment),
                    executable_digest=actual_digest,
                    duration_seconds=duration,
                )
                gemini_model = installation.model
                gemini_cwd = str(launch_files.path)
                exchange = lambda session: gemini_exchange(
                    session,
                    job,
                    turns,
                    tools,
                    usage,
                    model=gemini_model,
                    cwd=gemini_cwd,
                )
                await tools.start()
            elif isinstance(installation, AntigravityInstallationSpec):
                use_bridge = True
                antigravity_runtime = AntigravityInstallation(
                    executable=installation.executable,
                    cwd=installation.cwd,
                    home=installation.home,
                    model=installation.model,
                    effort=installation.effort,
                    executable_digest=actual_digest,
                    duration_seconds=duration,
                )
                launch_files = AntigravityAuthoringHome(antigravity_runtime, attempt_id)
                # The private MCP endpoint starts before the client sees its configuration.
                await tools.start()
                descriptor = tools.bridge.descriptor()
                pairs = descriptor["env"]
                assert isinstance(pairs, list)
                antigravity_prompt = (
                    "Read-only epic decomposition."
                    if job.kind == "decomposition"
                    else "Read-only epic brief authoring."
                )
                environment = launch_files.prepare(
                    "forge",
                    {
                        "mcpServers": {
                            "forge": {
                                "command": descriptor["command"],
                                "args": descriptor["args"],
                                "env": {pair["name"]: pair["value"] for pair in pairs},
                            }
                        }
                    },
                    antigravity_prompt,
                )
                spec = ClientLaunchSpec(
                    argv=(
                        antigravity_runtime.executable,
                        *antigravity_runtime.script,
                        "--model",
                        antigravity_runtime.model,
                        "--effort",
                        antigravity_runtime.effort,
                        "--input-format",
                        "stream-json",
                        "--output-format",
                        "stream-json",
                        "--json-schema",
                        json.dumps(authoring_schema(job.kind)),
                        "--disable-slash-commands",
                    ),
                    cwd=str(launch_files.path),
                    environment=environment,
                    allowed_environment=frozenset(environment),
                    executable_digest=antigravity_runtime.executable_digest,
                    duration_seconds=duration,
                )
                antigravity_model = installation.model
                exchange = lambda session: antigravity_exchange(
                    session,
                    job,
                    turns,
                    tools,
                    usage,
                    model=antigravity_model,
                )
            else:
                return BrainstormGatewayResult(proposal=None, telemetry=None, failure="unavailable")

            async with asyncio.timeout(duration):
                session = await self.supervisor.start(
                    spec, lifecycle=lifecycle, before_stop=tools.revoke
                )
                proposal = await exchange(session)
        except asyncio.CancelledError:
            interrupted = True
            failure = "cancelled"
        except TimeoutError, ClientProcessTimeout:
            failure = "timeout"
        except ClientSettlementUncertain:
            failure = "process_unsettled"
        except AuthoringProviderFailure as error:
            failure = error.failure
            quota_reset_at = error.quota_reset_at
        except ProtocolError, ClientProcessError, ValueError, TypeError, KeyError:
            failure = "invalid_output"
        except Exception:  # noqa: BLE001 - provider and repository errors stay private
            failure = "unavailable"
        finally:

            async def settle() -> None:
                nonlocal failure
                cleanup_failed = False
                try:
                    await tools.revoke()
                except asyncio.CancelledError, Exception:  # noqa: BLE001 - cleanup must continue
                    cleanup_failed = True
                if use_bridge:
                    try:
                        await tools.close()
                    except asyncio.CancelledError, Exception:  # noqa: BLE001 - still stop client
                        cleanup_failed = True
                if session is not None:
                    try:
                        receipt = await session.close(
                            completed=proposal is not None
                            and failure is None
                            and not interrupted
                            and not cleanup_failed
                        )
                        if not receipt.stop_confirmed:
                            cleanup_failed = True
                        elif proposal is not None and (
                            receipt.return_code != 0
                            or receipt.outcome not in {"exited", "completed"}
                        ):
                            failure = "process_failed"
                    except asyncio.CancelledError, Exception:  # noqa: BLE001 - uncertain stop
                        cleanup_failed = True
                if cleanup_failed:
                    failure = "process_unsettled"
                if launch_files is not None and failure != "process_unsettled":
                    try:
                        launch_files.cleanup()
                    except asyncio.CancelledError, Exception:  # noqa: BLE001 - private scratch
                        failure = "process_unsettled"

            cleanup = asyncio.create_task(settle())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
            if interrupted and failure != "process_unsettled":
                failure = "cancelled"
        if failure is not None:
            proposal = None
        elapsed_ms = max(1, int((time.monotonic() - started) * 1000))
        telemetry = usage.telemetry(elapsed_ms)
        if (
            job.budget.max_input_tokens is not None
            and telemetry.input_tokens is not None
            and telemetry.input_tokens > job.budget.max_input_tokens
        ) or (
            job.budget.max_output_tokens is not None
            and telemetry.output_tokens is not None
            and telemetry.output_tokens > job.budget.max_output_tokens
        ):
            failure, proposal = "budget_exhausted", None
        return BrainstormGatewayResult(
            proposal=proposal, telemetry=telemetry, failure=failure, quota_reset_at=quota_reset_at
        )

    async def _execute_inner(
        self,
        job: AuthoringJobSnapshot,
        turns: tuple[BrainstormTurn, ...],
        reader: BrainstormReadOnlyTools,
        *,
        cancelled: Callable[[], Awaitable[bool]],
        lifecycle: BrainstormProcessLifecycle,
    ) -> BrainstormGatewayResult:
        if await cancelled():
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="cancelled")

        spec = self.launch_spec
        if spec is None:
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="unavailable")

        start_time = time.monotonic()
        try:
            session = await self.supervisor.start(spec, lifecycle=lifecycle)
        except ClientProcessTimeout:
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="timeout")
        except Exception:  # noqa: BLE001 - boundary failure becomes a stable public failure
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="unavailable")

        payload = {
            "schema_version": 1,
            "job_id": str(job.job_id),
            "epic_id": str(job.epic_id),
            "project_id": str(job.project_id),
            "conversation_id": str(job.conversation_id),
            "prompt_turn_id": str(job.prompt_turn_id),
            "turns": [turn.model_dump(mode="json") for turn in turns],
            "draft_content": job.draft_content.model_dump(mode="json"),
            "accepted_content": (
                job.accepted_content.model_dump(mode="json") if job.accepted_content else None
            ),
            "budget": {
                "max_duration_seconds": job.budget.max_duration_seconds,
                "max_tool_calls": job.budget.max_tool_calls,
                "max_named_checks": job.budget.max_named_checks,
                "max_provider_attempts": job.budget.max_provider_attempts,
                "max_repairs": job.budget.max_repairs,
                "max_input_tokens": job.budget.max_input_tokens,
                "max_output_tokens": job.budget.max_output_tokens,
                "max_cost_minor": job.budget.max_cost_minor,
                "billing_mode": job.budget.billing_mode.value,
            },
        }

        try:
            await session.send(payload)
        except ClientProcessTimeout:
            await session.close()
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="timeout")
        except ClientProcessError:
            await session.close()
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="invalid_output")

        frames: list[Mapping[str, Any]] = []
        try:
            while True:
                if await cancelled():
                    await session.close()
                    return BrainstormGatewayResult(
                        proposal=None, telemetry=None, failure="cancelled"
                    )
                try:
                    frame = await session.receive()
                except ClientProcessTimeout:
                    await session.close()
                    return BrainstormGatewayResult(proposal=None, telemetry=None, failure="timeout")
                except ClientProcessError:
                    break

                if frame is None:
                    break

                frames.append(frame)

                # Dispatch tool calls if child process requests read tools
                frame_type = frame.get("type") or frame.get("method")
                if frame_type in ("tool_call", "tools/call"):
                    tool = frame.get("tool") or frame.get("name")
                    call_id = frame.get("id")
                    args = frame.get("arguments") or frame.get("params") or {}
                    try:
                        if tool == "list_files":
                            entries = await reader.list_files(
                                args.get("path") or args.get("directory") or "."
                            )
                            tool_result: Any = [
                                {"path": e.path, "kind": e.kind, "byte_count": e.byte_count}
                                for e in entries
                            ]
                        elif tool == "read_file":
                            file_read = await reader.read_file(args.get("path", ""))
                            tool_result = {
                                "path": file_read.path,
                                "content": file_read.content,
                                "truncated": file_read.truncated,
                            }
                        elif tool == "search":
                            matches = await reader.search(
                                args.get("query") or args.get("literal", ""),
                                args.get("path", "."),
                            )
                            tool_result = [
                                {
                                    "path": m.path,
                                    "line_number": m.line_number,
                                    "line_text": m.line_text,
                                }
                                for m in matches
                            ]
                        elif tool == "read_instructions":
                            docs = await reader.read_instructions()
                            tool_result = [
                                {"path": d.path, "content": d.content, "truncated": d.truncated}
                                for d in docs
                            ]
                        else:
                            tool_result = {"error": f"unknown tool: {tool}"}
                        await session.send({"id": call_id, "result": tool_result})
                    except Exception:  # noqa: BLE001 - repository errors are private
                        await session.send({"id": call_id, "error": "repository read unavailable"})
                elif "proposal" in frame or "problem" in frame or "failure" in frame:
                    await session.close_stdin()
        finally:
            proc_result = await session.close(completed=True)

        elapsed_ms = max(1, int((time.monotonic() - start_time) * 1000))

        if proc_result.outcome == "timeout":
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="timeout")
        if proc_result.outcome == "cancelled":
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="cancelled")
        if proc_result.outcome == "protocol_error":
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="invalid_output")

        # Parse final proposal and telemetry from collected frames
        proposal: BrainstormProposal | DecompositionProposal | None = None
        failure: str | None = None
        telemetry: AttemptTelemetry | None = None
        quota_reset_at: str | None = None

        all_frames = proc_result.frames or tuple(frames)
        for f in reversed(all_frames):
            if "failure" in f:
                failure = str(f["failure"])
                quota_reset_at = f.get("quota_reset_at")
                if "telemetry" in f and isinstance(f["telemetry"], Mapping):
                    telemetry = _parse_telemetry(f["telemetry"], elapsed_ms)
                break
            if "proposal" in f and isinstance(f["proposal"], Mapping):
                prop_data = dict(f["proposal"])
                if "turn_id" not in prop_data:
                    prop_data["turn_id"] = str(job.prompt_turn_id)
                model = DecompositionProposal if job.kind == "decomposition" else BrainstormProposal
                try:
                    proposal = model.model_validate(prop_data)
                except ValidationError:
                    failure = "invalid_output"
                if "telemetry" in f and isinstance(f["telemetry"], Mapping):
                    telemetry = _parse_telemetry(f["telemetry"], elapsed_ms)
                break
            if "problem" in f:
                prop_data = dict(f)
                if "turn_id" not in prop_data:
                    prop_data["turn_id"] = str(job.prompt_turn_id)
                model = DecompositionProposal if job.kind == "decomposition" else BrainstormProposal
                try:
                    proposal = model.model_validate(prop_data)
                except ValidationError:
                    failure = "invalid_output"
                if "telemetry" in f and isinstance(f["telemetry"], Mapping):
                    telemetry = _parse_telemetry(f["telemetry"], elapsed_ms)
                break

        if failure is None and proposal is None:
            if proc_result.return_code != 0:
                failure = "process_failed"
            else:
                failure = "invalid_output"

        if proc_result.return_code != 0:
            failure, proposal = "process_failed", None
        if telemetry is None:
            telemetry = AttemptTelemetry(
                duration_ms=elapsed_ms,
                unknown_telemetry_reasons=(
                    "token telemetry unavailable",
                    "subscription cost and quota telemetry unavailable",
                ),
            )

        return BrainstormGatewayResult(
            proposal=proposal,
            telemetry=telemetry,
            failure=failure,
            quota_reset_at=quota_reset_at,
        )


__all__ = ["EpicBrainstormGateway"]
