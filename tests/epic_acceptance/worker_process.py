"""Test-owned provider boundary for distinct frozen epic item changes."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus, AgentResult, DeveloperOutput
from forge.observability.redaction import Redactor
from forge.observability.usage import UsageRecord

from tests.acceptance import worker_process as base


class EpicScriptedDeliveryGateway(base.ScriptedDeliveryGateway):
    async def execute(self, request):
        if request.role is not AgentRole.DEVELOPER:
            return await super().execute(request)

        # Runtime agents receive the normalized task (title, blank line, body).
        title, separator, body = request.context.original_task.content.partition("\n\n")
        assert separator and title.startswith("Deliver item "), "unexpected task envelope"
        frozen = json.loads(body)
        requirement, = frozen["requirements"]
        criterion, = requirement["acceptance_criteria"]
        item_criterion, = frozen["item"]["acceptance_criteria"]
        assert criterion == item_criterion, "the item changed its original requirement criterion"
        number = int(criterion.removeprefix("Deliverable ").removesuffix(" is integrated"))
        assert number in (1, 2, 3), "unexpected frozen work item"
        tools = {tool.name: tool.func for tool in self._tools.tools_for(request).tools}

        tool_call_count = 0

        async def call(name, **kw):
            nonlocal tool_call_count
            tool_call_count += 1
            result = await tools[name](
                tool_context=SimpleNamespace(
                    invocation_id=str(request.execution_id),
                    function_call_id=f"{name}-{tool_call_count}",
                ),
                **kw,
            )
            if result.get("status") != "succeeded":
                raise AssertionError(
                    f"Tool {name} failed: {Redactor().redact(json.dumps(result))}"
                )
            return result.get("metadata", {})

        await call("repository.write_file", path="README.md", content=f"Verified delivery\nItem {number}\n")
        await call(
            "repository.write_file", path=f"item-{number}-criterion.txt",
            content=(
                f"Requirement: {requirement['id']}\n"
                f"Criterion: {criterion}\n"
                f"Item: {frozen['item_id']}\n"
            ),
        )
        await call("git.commit", message=f"Integrate required item {number}")
        candidate = await call("git.diff", scope="candidate")
        output = DeveloperOutput(
            summary=f"Integrated original requirement {requirement['id']}",
            changed_paths=tuple(candidate.get("changed_paths", ["README.md", f"item-{number}-criterion.txt"])),
            tests_added_or_changed=(), named_checks_run=(),
            local_commit_sha=candidate["head_sha"], diff_digest=candidate["diff_digest"],
            unresolved_concerns=(), plan_deviations=(),
        )
        return AgentResult(
            execution_id=request.execution_id, role=request.role,
            finish_status=AgentFinishStatus.SUCCEEDED,
            provider=request.provider, model=request.model,
            instruction_digest=request.instruction_digest,
            output=output, tool_call_count=4, duration_ms=1,
            usage=UsageRecord(
                provider=request.provider, model=request.model,
                prompt_version=request.instruction_version,
                input_tokens=1, output_tokens=1, tool_call_count=4,
                duration_ms=1, estimated_cost_minor=0,
                pricing_version="acceptance-v1", currency="USD",
            ),
        )


async def main() -> None:
    base.ScriptedDeliveryGateway = EpicScriptedDeliveryGateway
    await base.main()


if __name__ == "__main__":
    asyncio.run(main())
