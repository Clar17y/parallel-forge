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

from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentResult,
    DeveloperOutput,
    ReviewDecision,
    ReviewOutput,
)
from forge.domain.plan import PlanOutput
from forge.observability.redaction import Redactor
from forge.observability.usage import UsageRecord
from forge.persistence.database import create_engine, create_session_factory
from forge.settings import Settings
from forge.worker import composition
from forge.worker.composition import ReleaseDependencies, compose_worker_handlers
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
                tool_call_count=tool_count,
                duration_ms=1,
                estimated_cost_minor=0,
                pricing_version="acceptance-v1",
                currency="USD",
            ),
        )


async def main() -> None:
    composition.GoogleAdkGateway = ScriptedDeliveryGateway

    settings = Settings(process_role="worker")

    fake_github_url = os.environ.get("FORGE_FAKE_GITHUB_URL")
    bare_remote_str = os.environ.get("FORGE_BARE_REMOTE_PATH")
    handlers = None

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

    try:
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
