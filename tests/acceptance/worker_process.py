"""Test-owned worker entry point retaining Forge's production composition path.

Only the paid ADK provider is replaced. ``compose_worker_handlers`` still
builds BoundDeliveryGateway and ControlledToolService for delivery requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

from uuid import uuid4

from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessSupervisor,
    terminal_launch_proof,
)
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.ports.epic_brainstorm import BrainstormGatewayResult
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
    SubscriptionInvocationResult,
)
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentResult,
    DeveloperOutput,
    ReviewDecision,
    ReviewOutput,
)
from forge.domain.epic_brainstorm import BrainstormProposal
from forge.domain.plan import PlanOutput, ScopedPlanOutput
from forge.domain.subscription import (
    AttemptTelemetry,
    DelegateDecision,
    LogicalTaskContract,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    UnknownTelemetryPolicy,
    WaitDecision,
)
from forge.domain.task_usage_contract import CounterKind, Observation, TokenCounts
from forge.domain.tool import ToolName
from forge.observability.redaction import Redactor
from forge.observability.usage import UsageRecord
from forge.persistence.database import create_engine, create_session_factory
from forge.settings import Settings
from forge.worker import composition
from forge.worker import main as worker_main
from forge.worker.composition import ReleaseDependencies, compose_worker_handlers
from forge.worker.epic_brainstorm_composition import make_brainstorm_reader_factory
from forge.worker.main import run_worker

from tests.acceptance.fake_github_service import (
    FakeGitHubHttpClient,
    FakeGitHubHttpWriteClient,
    LocalBareAdoption,
    LocalBarePush,
)


class ScriptedDeliveryGateway:
    """Deterministic offline provider executing real controlled tools."""

    def __init__(self, **kwargs: object) -> None:
        self._tools = kwargs["tool_provider"]

    async def execute(self, request):
        tools = {tool.name: tool.func for tool in self._tools.tools_for(request).tools}

        async def call(name, **kw):
            result = await tools[name](
                tool_context=SimpleNamespace(
                    invocation_id=str(request.execution_id), function_call_id=name
                ),
                **kw,
            )
            if result.get("status") != "succeeded":
                redacted_result = Redactor().redact(result)
                raise AssertionError(f"Tool {name} failed: {json.dumps(redacted_result, sort_keys=True)}")
            return result.get("metadata", {})

        if request.role is AgentRole.PLANNER:
            output = PlanOutput(
                summary="Acceptance README update",
                assumptions=(),
                affected_components=("README.md",),
                steps=("Update README",),
                required_checks=("unit",),
                risks=("fixture only",),
                security_considerations=(),
                dependency_changes=(),
            )
            tool_count = 0

        elif request.role is AgentRole.DEVELOPER:
            ctx = request.context
            if getattr(ctx, "remote_evidence", None) is not None:
                content = "Verified delivery\nRemote repair README\n"
                msg = "Remote repair README"
            elif getattr(ctx, "operator_feedback", None) is not None:
                content = "Verified delivery\nOperator revised README\n"
                msg = "Operator revised README"
            elif getattr(ctx, "check_evidence", None) or getattr(ctx, "remediation_findings", None):
                content = "Verified delivery\n"
                msg = "Local repair README"
            else:
                # First turn: deliberate failure to verify bounded remediation
                content = "Needs repair\n"
                msg = "Initial README candidate"

            await call("repository.write_file", path="README.md", content=content)
            await call("git.commit", message=msg)
            candidate = await call("git.diff", scope="candidate")
            output = DeveloperOutput(
                summary="Updated README",
                changed_paths=tuple(candidate.get("changed_paths", ["README.md"])),
                tests_added_or_changed=(),
                named_checks_run=(),
                local_commit_sha=candidate["head_sha"],
                diff_digest=candidate["diff_digest"],
                unresolved_concerns=(),
                plan_deviations=(),
            )
            tool_count = 3

        elif request.role is AgentRole.REVIEWER:
            await call("validation-results.read")
            output = ReviewOutput(
                decision=ReviewDecision.APPROVE,
                findings=(),
                tested_claims=("README and check",),
                missing_evidence=(),
                summary="Verified candidate",
            )
            tool_count = 1

        else:
            raise AssertionError(f"Unexpected agent role: {request.role}")

        return AgentResult(
            execution_id=request.execution_id,
            role=request.role,
            finish_status=AgentFinishStatus.SUCCEEDED,
            provider=request.provider,
            model=request.model,
            instruction_digest=request.instruction_digest,
            output=output,
            tool_call_count=tool_count,
            duration_ms=1,
            usage=UsageRecord(
                provider=request.provider,
                model=request.model,
                prompt_version=request.instruction_version,
                input_tokens=1,
                output_tokens=1,
                input_tokens_complete=True,
                output_tokens_complete=True,
                cached_input_tokens_complete=True,
                tool_call_count=tool_count,
                duration_ms=1,
                estimated_cost_minor=0,
                pricing_version="acceptance-v1",
                currency="USD",
            ),
        )


class ScriptedObservedAuthoringGateway:
    """Emit measured authoring usage while an actual supervised child is live."""

    def __init__(self) -> None:
        self.invocations = 0
        self.final_only = os.environ.get("FORGE_TASK_USAGE_FINAL_ONLY") == "1"
        self.streaming_success = os.environ.get("FORGE_TASK_USAGE_STREAMING_SUCCESS") == "1"
        self.learned_crossing = os.environ.get("FORGE_TASK_USAGE_LEARNED_CROSSING") == "1"
        self.slow_settlement = os.environ.get("FORGE_TASK_USAGE_SLOW_SETTLEMENT") == "1"

    async def execute(self, job, turns, reader, *, cancelled, lifecycle):
        raise AssertionError("observed authoring execution was bypassed")

    async def execute_observed(
        self, job, turns, reader, *, cancelled, lifecycle, usage_binding, usage_sink
    ):
        self.invocations += 1
        first_attempt = self.invocations == 1 and not (self.final_only or self.streaming_success)
        crossing_attempt = self.learned_crossing and self.invocations == 2
        normal_tokens = ((60, 40) if first_attempt or self.final_only or self.streaming_success
                         else (10, 5))
        input_tokens, output_tokens = (120, 100) if crossing_attempt else normal_tokens
        spec = ClientLaunchSpec(
            argv=(sys.executable, "-u", "-c", f"import time; time.sleep({15 if first_attempt else 0.1})"),
            cwd=str(Path.cwd()), environment={}, duration_seconds=20,
        )
        process = await ClientProcessSupervisor().start(spec, lifecycle=lifecycle)
        logger.info("[AUTHORING OBSERVED] process started")
        try:
            if self.final_only:
                await process.wait_closed()
                logger.info("[AUTHORING FINAL ONLY] process settled before telemetry")
            await usage_sink.observe_usage(Observation(
                binding=usage_binding,
                event_id=f"scripted-authoring-{usage_binding.source.source_id}", sequence=1,
                counter_kind=CounterKind.CUMULATIVE,
                final=self.final_only,
                final_status="succeeded" if self.final_only else None,
                tokens=TokenCounts(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            ))
            logger.info("[AUTHORING OBSERVED] usage persisted")
            if self.streaming_success:
                logger.info("[AUTHORING STREAMING SUCCESS] usage persisted before process settled")
            if crossing_attempt:
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    logger.info("[AUTHORING LEARNED CROSSING] checkpoint cancelled live provider")
                    await process.close()
                    return BrainstormGatewayResult(
                        proposal=None,
                        telemetry=AttemptTelemetry(
                            input_tokens=input_tokens, output_tokens=output_tokens,
                            duration_ms=100,
                        ),
                        failure="interrupted",
                    )
            if first_attempt:
                for _ in range(200):
                    if await cancelled():
                        logger.info("[AUTHORING OBSERVED] checkpoint cancellation observed")
                        break
                    await asyncio.sleep(0.05)
            if first_attempt:
                if self.slow_settlement:
                    logger.info("[AUTHORING SLOW SETTLEMENT] child remains live after cancel")
                    await asyncio.sleep(5)
                await process.close()
            else:
                await process.wait_closed()
            logger.info("[AUTHORING OBSERVED] process settled")
            if self.streaming_success:
                await usage_sink.observe_usage(Observation(
                    binding=usage_binding,
                    event_id=f"scripted-authoring-final-{usage_binding.source.source_id}",
                    sequence=2,
                    counter_kind=CounterKind.CUMULATIVE,
                    tokens=TokenCounts(input_tokens=input_tokens, output_tokens=output_tokens),
                    final=True,
                    final_status="succeeded",
                ))
                logger.info("[AUTHORING STREAMING SUCCESS] final observation persisted")
            return BrainstormGatewayResult(
                proposal=None if first_attempt else BrainstormProposal(
                    turn_id=uuid4(), problem="A resumed observed proposal"
                ),
                telemetry=AttemptTelemetry(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=100,
                ),
                failure="interrupted" if first_attempt else None,
            )
        finally:
            await process.close()


class ScriptedObservedSubscriptionAdapter:
    """A pinned primary/child adapter with live supervised provider processes."""

    def __init__(self, route: RouteSpec, child_id):
        self.route = route
        self.child_id = child_id
        self.delegated = False
        self.effect_attempted = False

    def gateway_for(self, request, *, broker, lifecycle):
        adapter = self

        class Gateway:
            async def execute(self, value):
                assert value == request
                logger.info("[DELEGATED PRIMARY] executing %s", request.run_state)
                spec = ClientLaunchSpec(
                    argv=(sys.executable, "-u", "-c", "import time; time.sleep(0.1)"),
                    cwd=str(Path.cwd()), environment={}, duration_seconds=10,
                )
                process = await ClientProcessSupervisor().start(spec, lifecycle=lifecycle)
                result = await process.wait_closed()
                logger.info("[DELEGATED PRIMARY] process settled")
                if request.run_state.value == "PLANNING":
                    decision = ScopedPlanOutput(
                        summary="Observed delegated README task", assumptions=(),
                        affected_components=("README.md",), steps=("Delegate bounded update",),
                        required_checks=("unit",), risks=("Fixture only",), security_considerations=(),
                        dependency_changes=(), owned_paths=("README.md",),
                    )
                elif request.task.purpose is SpecialistPurpose.PRIMARY and not adapter.delegated:
                    adapter.delegated = True
                    decision = DelegateDecision(
                        run_id=request.task.run_id, parent_task_id=request.task.task_id,
                        child_tasks=(LogicalTaskContract(
                            run_id=request.task.run_id, task_id=adapter.child_id,
                            parent_task_id=request.task.task_id,
                            purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                            route=request.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
                            budget=TaskBudget(
                                max_provider_attempts=2,
                                unknown_telemetry_policy=UnknownTelemetryPolicy(
                                    max_uncertain_attempts=2
                                ),
                            ),
                            owned_paths=("README.md",), max_repairs=0,
                        ),), rationale="One bounded child observes measured usage",
                    )
                else:
                    decision = WaitDecision(
                        run_id=request.task.run_id, task_id=request.task.task_id,
                        waiting_on_task_ids=(adapter.child_id,), reason="Await child settlement",
                    )
                return SubscriptionInvocationResult(
                    attempt=request.attempt, decision=decision,
                    telemetry=AttemptTelemetry(input_tokens=1, output_tokens=1, duration_ms=100),
                    launch_proof=terminal_launch_proof(result),
                )

            async def execute_observed(self, value, *, usage_binding, usage_sink):
                assert value == request
                if request.task.purpose is SpecialistPurpose.PRIMARY:
                    return await self.execute(value)
                spec = ClientLaunchSpec(
                    argv=(sys.executable, "-u", "-c", "import time; time.sleep(15)"),
                    cwd=str(Path.cwd()), environment={}, duration_seconds=20,
                )
                process = await ClientProcessSupervisor().start(spec, lifecycle=lifecycle)
                logger.info("[DELEGATED OBSERVED] process started")
                check_task = None
                effect_root = os.environ.get("FORGE_TASK_USAGE_EFFECT_REPOSITORY")
                if effect_root and not adapter.effect_attempted:
                    adapter.effect_attempted = True
                    check_task = asyncio.create_task(broker(ProviderToolCall(
                        call_key=f"held-check-{request.attempt.attempt_id}",
                        thread_id="acceptance-thread", turn_id="acceptance-turn",
                        name=ToolName.BUILD_RUN_NAMED_CHECK.value,
                        arguments={"command_name": "held-check"},
                    )))
                    root = Path(effect_root) / ".worktrees"

                    def check_entered() -> bool:
                        import time

                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            if list(root.rglob("check.entered")):
                                return True
                            if check_task.done():
                                return False
                            time.sleep(0.05)
                        return False

                    if not await asyncio.to_thread(check_entered):
                        raise AssertionError(f"held check did not enter: {check_task.result() if check_task.done() else 'pending'}")
                try:
                    await usage_sink.observe_usage(Observation(
                        binding=usage_binding, event_id=f"scripted-child-{request.attempt.attempt_id}",
                        sequence=1, counter_kind=CounterKind.CUMULATIVE,
                        tokens=TokenCounts(input_tokens=60, output_tokens=40),
                    ))
                    logger.info("[DELEGATED OBSERVED] usage persisted while process live")
                    if check_task is not None:
                        # Keep the observed invocation active until checkpoint
                        # cancellation. A three-second sleep races the runner's
                        # five-second renewal and can leave this catch too early.
                        await asyncio.shield(check_task)
                    else:
                        await asyncio.sleep(3)
                except asyncio.CancelledError:
                    logger.info("[DELEGATED OBSERVED] invocation cancelled")
                    result = await process.close()
                    assert result.stop_confirmed
                    logger.info("[DELEGATED OBSERVED] cancelled process settled")
                    if check_task is not None:
                        receipt = await asyncio.wait_for(asyncio.shield(check_task), timeout=10)
                        assert receipt.get("status") == "succeeded", receipt
                        logger.info("[DELEGATED OBSERVED] held check settled")
                    raise SubscriptionInterrupted(SubscriptionInvocationResult(
                        attempt=request.attempt, failure=SubscriptionFailure.INTERRUPTED,
                        telemetry=AttemptTelemetry(input_tokens=60, output_tokens=40),
                        launch_proof=terminal_launch_proof(result),
                    )) from None
                result = await process.close()
                if check_task is not None:
                    receipt = await asyncio.shield(check_task)
                    assert receipt.get("status") == "succeeded", receipt
                logger.info("[DELEGATED OBSERVED] process settled")
                return SubscriptionInvocationResult(
                    attempt=request.attempt, failure=SubscriptionFailure.INTERRUPTED,
                    telemetry=AttemptTelemetry(input_tokens=60, output_tokens=40),
                    launch_proof=terminal_launch_proof(result),
                )

        return Gateway()


async def main() -> None:
    composition.GoogleAdkGateway = ScriptedDeliveryGateway

    settings = Settings(process_role="worker")

    fake_github_url = os.environ.get("FORGE_FAKE_GITHUB_URL")
    bare_remote_str = os.environ.get("FORGE_BARE_REMOTE_PATH")
    handlers = None
    factory = None

    if fake_github_url and bare_remote_str:
        bare_remote_path = Path(bare_remote_str)
        engine = create_engine(settings.database_url)
        factory = create_session_factory(engine)
        read = FakeGitHubHttpClient(fake_github_url)
        writes = FakeGitHubHttpWriteClient(fake_github_url)
        push = lambda policy: LocalBarePush(bare_remote_path, fake_github_url)
        adoption = lambda policy: LocalBareAdoption(bare_remote_path)
        release_deps = ReleaseDependencies(
            read=read,
            writes=writes,
            push=push,
            adoption=adoption,
            queue=None,
        )
        handlers = compose_worker_handlers(
            settings,
            factory,
            release_dependencies=release_deps,
        )

        redactor = Redactor()

        def _logged_handler(name: str, fn):
            async def wrapper(*args, **kwargs):
                logging.info("[HANDLER START] %s", name)  # noqa: LOG015 - child process root logger
                try:
                    res = await fn(*args, **kwargs)
                    logging.info("[HANDLER SUCCESS] %s", name)  # noqa: LOG015 - child process root logger
                    return res
                except Exception as exc:
                    logging.error(  # noqa: LOG015 - child process root logger
                        "[HANDLER FAILED] %s failed: %s (%s)",
                        name,
                        redactor.redact(str(exc)),
                        type(exc).__name__,
                    )
                    raise
            return wrapper

        for cmd_name, cmd_fn in list(handlers.items()):
            handlers[cmd_name] = _logged_handler(cmd_name, cmd_fn)

        if os.environ.get("FORGE_TASK_USAGE_DELEGATED") == "1":
            await handlers.aclose()
            handlers = None

            def compose_with_fixture(*args, **kwargs):
                return compose_worker_handlers(
                    *args, release_dependencies=release_deps, **kwargs
                )

            worker_main.compose_worker_handlers = compose_with_fixture

    try:
        if os.environ.get("FORGE_TASK_USAGE_PROCESS") == "1":
            if factory is None:
                raise RuntimeError("task usage process fixture requires a managed bare remote")
            subscription_adapters = None
            if os.environ.get("FORGE_TASK_USAGE_DELEGATED") == "1":
                from forge.domain.subscription import ReasoningEffort

                child_id = uuid4()
                subscription_adapters = (
                    ScriptedObservedSubscriptionAdapter(RouteSpec(
                        provider="openai", client="codex_app_server", model="gpt-6-astra",
                        effort=ReasoningEffort.LOW,
                    ), child_id),
                    ScriptedObservedSubscriptionAdapter(RouteSpec(
                        provider="google", client="gemini_cli", model="gemini-3.8-flash",
                        effort=ReasoningEffort.MEDIUM,
                    ), child_id),
                )
            authoring_gateway = ScriptedObservedAuthoringGateway()
            await run_worker(
                settings, handlers=handlers, poll_interval=0.05,
                decision_retry_interval=0.1,
                subscription_adapters=subscription_adapters,
                brainstorm_gateway_factory=lambda snapshot: authoring_gateway,
                decomposition_gateway_factory=lambda snapshot: authoring_gateway,
                brainstorm_reader_factory=make_brainstorm_reader_factory(factory),
            )
        else:
            await run_worker(settings, handlers=handlers, poll_interval=0.05)
    except Exception as exc:
        logging.error("Worker process exception: %s", Redactor().redact(str(exc)))  # noqa: LOG015
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        sys.stderr.write(f"Fatal worker exception: {Redactor().redact(str(exc))}\n")
        sys.stderr.flush()
        raise
