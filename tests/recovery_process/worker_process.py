"""Recovery test worker process entry point with crash instrumentation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
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

from forge.application.ports.operations import OperationAdapter
from forge.application.services.recovery import OperationExecutor, _request_values
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentResult,
    DeveloperOutput,
    ReviewDecision,
    ReviewOutput,
)
from forge.domain.operation import OperationOutcome, OperationRequest
from forge.domain.plan import PlanOutput
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot
from forge.observability.redaction import Redactor
from forge.observability.usage import UsageRecord
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.models import OperationIntent
from forge.settings import Settings
from forge.worker import composition
from forge.worker.composition import ReleaseDependencies, compose_worker_handlers
from forge.worker.delivery_runtime import DeliveryRuntime
from forge.worker.main import run_worker

from tests.acceptance.fake_github_service import (
    FakeGitHubHttpClient,
    FakeGitHubHttpWriteClient,
    LocalBareAdoption,
    LocalBarePush,
)
from tests.recovery_process.worker_crash_hook import crash_after_file_write, trigger_crash


class InstrumentingDeliveryGateway:
    """Offline provider executing controlled tools with crash points."""

    def __init__(self, **kwargs: object) -> None:
        self._tools = kwargs["tool_provider"]

    async def execute(self, request):
        trigger_crash("provider_request")

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
                raise AssertionError(
                    f"Tool {name} failed: {json.dumps(redacted_result, sort_keys=True)}"
                )
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
            elif getattr(ctx, "check_evidence", None) or getattr(
                ctx, "remediation_findings", None
            ):
                content = "Verified delivery\n"
                msg = "Local repair README"
            else:
                content = "Needs repair\n"
                msg = "Initial README candidate"

            await call("repository.write_file", path="README.md", content=content)
            crash_after_file_write()
            await call("git.commit", message=msg)
            trigger_crash("local_commit")
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
                tool_call_count=tool_count,
                duration_ms=1,
                estimated_cost_minor=0,
                pricing_version="acceptance-v1",
                currency="USD",
            ),
        )


class InstrumentingPush(LocalBarePush):
    async def push(self, worktree, policy, approved_sha: str) -> None:
        await super().push(worktree, policy, approved_sha)
        trigger_crash("push")


class InstrumentingGitHubClient(FakeGitHubHttpWriteClient):
    async def create_pull_request(
        self,
        repository: str,
        head_repository: str,
        head_ref: str,
        base_ref: str,
        title: str,
        body: str | None,
    ):
        res = await super().create_pull_request(
            repository, head_repository, head_ref, base_ref, title, body
        )
        trigger_crash("pr_creation")
        return res

    async def merge_pull_request(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        merge_method: str,
    ):
        res = await super().merge_pull_request(
            repository, pull_request_number, expected_head_sha, merge_method
        )
        trigger_crash("merge_request")
        return res


class InstrumentingOperationExecutor(OperationExecutor):
    async def execute(
        self,
        request: OperationRequest | Mapping[str, object],
        adapter: OperationAdapter,
    ) -> OperationOutcome:
        values = _request_values(request)
        kind = str(values.get("kind") or values.get("operation_type") or "")

        class _CrashingAdapter:
            def __init__(self, inner: OperationAdapter) -> None:
                self._inner = inner

            async def invoke(self, intent: OperationIntent) -> OperationOutcome:
                res = await self._inner.invoke(intent)
                if kind == "worktree.create":
                    trigger_crash("worktree_creation")
                elif kind == "database.provision":
                    trigger_crash("database_creation")
                elif kind == "worktree.teardown":
                    trigger_crash("worktree_removal")
                elif kind == "database.teardown":
                    trigger_crash("database_drop")
                return res

            async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
                return await self._inner.reconcile(intent)

        if kind == "worktree.create" and os.environ.get("FORGE_TEST_CRASH_POINT") == "worktree_intent":
            # Persist intent first, then trigger crash
            await self._operations.begin(**values)
            trigger_crash("worktree_intent")

        return await super().execute(request, _CrashingAdapter(adapter))


class InstrumentingDeliveryRuntime(DeliveryRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._operation_executor = InstrumentingOperationExecutor(
            self._operations,
            execution_lease_seconds=30,
        )

    async def prepare(self, run_id, policy: ProjectPolicy):
        if not policy.database.enabled and os.environ.get("FORGE_TEST_CRASH_POINT") == "database_disabled_no_intent":
            worktree = await super().prepare(run_id, policy)
            trigger_crash("database_disabled_no_intent")
            return worktree
        return await super().prepare(run_id, policy)

    async def teardown(self, run_id, policy: ProjectPolicy) -> RunSnapshot:
        if not policy.database.enabled and os.environ.get("FORGE_TEST_CRASH_POINT") == "database_disabled_teardown":
            snapshot = await super().teardown(run_id, policy)
            trigger_crash("database_disabled_teardown")
            return snapshot
        return await super().teardown(run_id, policy)


async def main() -> None:
    composition.GoogleAdkGateway = InstrumentingDeliveryGateway

    settings = Settings(process_role="worker")

    fake_github_url = os.environ.get("FORGE_FAKE_GITHUB_URL")
    bare_remote_str = os.environ.get("FORGE_BARE_REMOTE_PATH")
    handlers = None

    if fake_github_url and bare_remote_str:
        bare_remote_path = Path(bare_remote_str)
        engine = create_engine(settings.database_url)
        factory = create_session_factory(engine)
        read = FakeGitHubHttpClient(fake_github_url)
        writes = InstrumentingGitHubClient(fake_github_url)
        push = lambda policy: InstrumentingPush(bare_remote_path, fake_github_url)
        adoption = lambda policy: LocalBareAdoption(bare_remote_path)
        release_deps = ReleaseDependencies(
            read=read,
            writes=writes,
            push=push,
            adoption=adoption,
            queue=None,
        )
        artifact_store = composition.FilesystemArtifactStore(settings.artifact_root)
        delivery_runtime = InstrumentingDeliveryRuntime(
            settings, factory, artifact_store, composition.Redactor(), None
        )
        handlers = compose_worker_handlers(
            settings,
            factory,
            delivery_runtime=delivery_runtime,
            release_dependencies=release_deps,
        )

        redactor = Redactor()

        def _instrumented_handler(name: str, fn):
            async def wrapper(*args, **kwargs):
                logging.info("[HANDLER START] %s", name)  # noqa: LOG015 - child process root logger
                trigger_crash("command_lease")
                try:
                    res = await fn(*args, **kwargs)
                    logging.info("[HANDLER SUCCESS] %s", name)  # noqa: LOG015 - child process root logger
                    trigger_crash("state_event_commit")
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
            handlers[cmd_name] = _instrumented_handler(cmd_name, cmd_fn)

    try:
        await run_worker(settings, handlers=handlers, poll_interval=0.05)
    except Exception as exc:
        logging.error("Worker process exception: %s", Redactor().redact(str(exc)))  # noqa: LOG015 - child process root logger
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        sys.stderr.write(f"Fatal worker exception: {Redactor().redact(str(exc))}\n")
        sys.stderr.flush()
        raise
