"""Subscription planning reads the canonical repository with no write authority."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.domain.approval import ApprovalGate
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunState
from forge.domain.subscription import SPECIALIST_ALLOWED_TOOLS, SpecialistPurpose, TaskBudget
from forge.domain.tool import (
    SubscriptionToolAuthorizationContext,
    ToolCallStatus,
    ToolErrorCode,
    ToolName,
    ToolRequest,
    repository_resource_identity,
)
from test_planner_tools import PROJECT_ID, RUN_ID, TASK_ID, _service


def context(purpose=SpecialistPurpose.PRIMARY):
    operation_id = uuid4()
    return SubscriptionToolAuthorizationContext(
        run_id=RUN_ID,
        task_id=TASK_ID,
        attempt_id=uuid4(),
        worktree_id=repository_resource_identity(PROJECT_ID),
        purpose=purpose,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
        invocation_id=operation_id,
        operation_intent_id=operation_id,
    )


@pytest.mark.parametrize("purpose", [SpecialistPurpose.PRIMARY, SpecialistPurpose.PLANNING])
async def test_subscription_planning_reads_canonical_repository(tmp_path, purpose):
    (tmp_path / "README.md").write_text("Planning source", encoding="utf-8")
    service, work, reader = _service(tmp_path)

    async def authorize(*args):
        return SimpleNamespace(budget=TaskBudget())

    async def find(*args):
        return None

    async def count(*args):
        return 0

    work.subscription = SimpleNamespace(authorize_tool=authorize)
    work.tool_calls.find = find
    work.tool_calls.count_for_subscription_task = count
    result = await service.invoke(
        context(purpose),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.metadata["content"] == "Planning source"
    assert reader.calls == 1 and work.committed
    record = work.tool_calls.records[0]
    assert record.agent_execution_id is None
    assert record.subscription_task_id == TASK_ID
    assert record.resource_id == repository_resource_identity(PROJECT_ID)


@pytest.mark.parametrize("change", ["worker", "paused", "gate", "resource", "exclusions", "root"])
async def test_subscription_planning_denies_without_current_read_authority(tmp_path, change):
    service, work, reader = _service(tmp_path)
    authority = context()
    if change == "worker":
        authority = context(SpecialistPurpose.ROUTINE_IMPLEMENTATION)
    elif change == "paused":
        work.runs.run = replace(work.runs.run, state=RunState.PAUSED)
    elif change == "gate":
        work.runs.run = replace(
            work.runs.run,
            state=RunState.AWAITING_PLAN_APPROVAL,
            pending_gate=ApprovalGate.PLAN,
            pending_evidence_digest="a" * 64,
        )
    elif change == "resource":
        authority = replace(authority, worktree_id="foreign-resource")
    elif change == "exclusions":
        reader.exclusion_result = False
    else:
        other = tmp_path / "other"
        other.mkdir()
        service, work, reader = _service(tmp_path, project_root=other)

    async def find(*args):
        return None

    work.tool_calls.find = find
    result = await service.invoke(
        authority, ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})
    )
    assert result.status is ToolCallStatus.DENIED and reader.calls == 0


@pytest.mark.parametrize(
    "tool_request",
    [
        ToolRequest(name=ToolName.REPOSITORY_WRITE_FILE, arguments={"path": "x", "content": "x"}),
        ToolRequest(
            name=ToolName.REPOSITORY_DELETE_FILE,
            arguments={"path": "x", "expected_digest": "a" * 64},
        ),
        ToolRequest(
            name=ToolName.REPOSITORY_RENAME_FILE,
            arguments={"source": "x", "destination": "y", "expected_digest": "a" * 64},
        ),
        ToolRequest(name=ToolName.GIT_STATUS, arguments={}),
        ToolRequest(name=ToolName.GIT_COMMIT, arguments={"message": "attempt"}),
        ToolRequest(name=ToolName.BUILD_RUN_NAMED_CHECK, arguments={"command_name": "unit"}),
    ],
)
def test_planning_phase_never_inherits_primary_write_check_or_git_authority(tmp_path, tool_request):
    service, work, reader = _service(tmp_path)
    authority = replace(
        context(), permitted_tools=SPECIALIST_ALLOWED_TOOLS[SpecialistPurpose.PRIMARY]
    )
    authorization, error = service._validate(
        authority,
        tool_request,
        work.runs.run,
        ProjectPolicy.model_validate(work.projects.project.policy.document),
    )
    assert authorization is None and error is not None and reader.calls == 0
    assert error[0] is ToolErrorCode.RUN_NOT_ACTIVE
