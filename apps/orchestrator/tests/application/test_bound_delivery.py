"""Bind reasoning to the approved delivery role before tools or provider effects."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from forge.agents.errors import AgentGatewayError
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.tools import CapabilityMatrix
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentRequest,
    DeveloperInput,
    ReviewerInput,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.plan import PlanOutput
from forge.domain.policy import AgentModelPolicy, ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import ToolName
from forge.worker.bound_delivery import BoundDeliveryGateway


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "state"), [
    (AgentRole.DEVELOPER, RunState.IMPLEMENTING),
    (AgentRole.DEVELOPER, RunState.REMEDIATING),
    (AgentRole.REVIEWER, RunState.REVIEWING),
])
@pytest.mark.parametrize(("approved_effort", "supplied_effort"), [
    (None, None), ("low", "low"), ("medium", "medium"), ("high", "high"),
    (None, "high"), ("low", None), ("low", "high"), ("medium", "low"), ("high", "low"),
])
async def test_delivery_reasoning_matches_approved_role_before_effects(
    tmp_path, role, state, approved_effort, supplied_effort
):
    model = AgentModelPolicy(model="gemini-3.8-flash", reasoning_effort=approved_effort)
    other_model = model.model_copy(update={
        "reasoning_effort": "low" if approved_effort != "low" else "high"
    })
    policy = ProjectPolicy(
        id=uuid4(), version=1, repository_path=str(tmp_path),
        github_repository="forge/test", default_branch="main",
        developer_model=model if role is AgentRole.DEVELOPER else other_model,
        reviewer_model=model if role is AgentRole.REVIEWER else other_model,
    )
    run = RunSnapshot(
        id=uuid4(), project_id=policy.id, task_id=uuid4(), state=state,
        policy_version=1, base_sha="a" * 40, branch_name="forge/test",
        worktree_path=str(tmp_path / "managed"),
    )
    identity = WorktreeIdentity.for_run(policy.id, run.id, run.branch_name, False)
    tree = ManagedWorktree(identity=identity, path=Path(run.worktree_path), base_sha=run.base_sha)
    task = UntrustedContent.from_text(
        "Implement the approved plan", source_kind=UntrustedSourceKind.TASK, source_reference="task"
    )
    plan = PlanOutput(
        summary="Approved plan", assumptions=(), affected_components=(), steps=("Implement",),
        required_checks=("tests",), risks=("regression",), security_considerations=(),
        dependency_changes=(),
    )
    context = (DeveloperInput(
        original_task=task, plan=plan, worktree_id=identity.worktree_name, base_commit=run.base_sha,
    ) if role is AgentRole.DEVELOPER else ReviewerInput(
        original_task=task, plan=plan, current_diff=UntrustedContent.from_text(
            "diff", source_kind=UntrustedSourceKind.DIFF, source_reference="diff"
        ), check_evidence=(),
    ))
    capabilities = CapabilityMatrix().capabilities_for(role)
    tools = tuple(name for name in ToolName if name in capabilities)
    instruction = "Approved role instructions"
    request = AgentRequest(
        execution_id=uuid4(), run_id=run.id, task_id=run.task_id, role=role, context=context,
        provider=model.provider, model=model.model, instruction_version="v1",
        system_instruction=instruction,
        instruction_digest=hashlib.sha256(instruction.encode()).hexdigest(),
        allowed_tools=tools, budget=AgentBudget.from_model_policy(model),
        reasoning_effort=supplied_effort,
    )
    admitted_at = datetime.now(UTC)
    admission = SimpleNamespace(
        step_id=uuid4(), status=ExecutionStatus.RUNNING,
        kind="implement" if role is AgentRole.DEVELOPER else "review", role=role,
        agent_execution_id=request.execution_id, provider=model.provider, model=model.model,
        instruction_version="v1", input_artifact_id=uuid4(), attempt=1, admitted_at=admitted_at,
    )
    event = SimpleNamespace(
        event_type="agent_execution.admitted", actor_class="system", actor_id=None,
        occurred_at=admitted_at, payload={
            "step_id": str(admission.step_id), "agent_execution_id": str(request.execution_id),
            "kind": admission.kind, "attempt": 1, "role": role.value, "status": "RUNNING",
            "instruction_version": "v1", "provider": model.provider, "model": model.model,
            "input_artifact_id": str(admission.input_artifact_id),
        },
    )
    wire = json.dumps(context.model_dump(mode="json"), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":")).encode()
    artifact = SimpleNamespace(
        artifact_id=admission.input_artifact_id, digest=hashlib.sha256(wire).hexdigest(),
        media_type="application/json", byte_count=len(wire), truncated=False,
    )
    work = SimpleNamespace(
        commit=AsyncMock(),
        executions=SimpleNamespace(get_admission=AsyncMock(return_value=admission)),
        events=SimpleNamespace(list_for_version=AsyncMock(return_value=[event])),
        artifacts=SimpleNamespace(get_by_producer=AsyncMock(return_value=[artifact])),
    )
    transaction = AsyncMock()
    transaction.__aenter__.return_value = work
    approved = SimpleNamespace(run=run, policy=policy, plan=plan)
    tool_factory = AsyncMock()
    gateway_factory = Mock()
    gateway = BoundDeliveryGateway(
        unit_of_work_factory=lambda: transaction,
        artifact_store=SimpleNamespace(open_bytes=AsyncMock(return_value=wire)),
        prompt_loader=Mock(),
        git_factory=lambda _: SimpleNamespace(
            inspect_worktree=Mock(return_value=tree), is_ancestor=Mock(return_value=True)
        ),
        tool_service_factory=tool_factory, gateway_factory=gateway_factory,
    )
    gateway._approved.load = AsyncMock(return_value=approved)

    if supplied_effort == approved_effort:
        assert await gateway._binding(request, work) == (approved, admission, tree)
    else:
        with pytest.raises(AgentGatewayError, match="^agent gateway execution failed$"):
            await gateway.execute(request)
        work.events.list_for_version.assert_not_awaited()
    tool_factory.assert_not_awaited()
    gateway_factory.assert_not_called()
