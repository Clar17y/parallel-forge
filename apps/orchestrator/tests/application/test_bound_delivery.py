"""Bind reasoning to the approved delivery role before tools or provider effects."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from forge.agents.errors import AgentGatewayError
from forge.application.ports.executions import ExecutionAdmission, ExecutionStatus
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
from google.adk.tools.base_tool import BaseTool


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "state"), [
    (AgentRole.DEVELOPER, RunState.IMPLEMENTING),
    (AgentRole.DEVELOPER, RunState.REMEDIATING),
    (AgentRole.REVIEWER, RunState.REVIEWING),
])
@pytest.mark.parametrize(("approved_effort", "supplied_effort", "monitoring"), [
    (None, None, None), ("low", "low", None), ("medium", "medium", None),
    ("high", "high", None), (None, "high", None), ("low", None, None),
    ("low", "high", None), ("medium", "low", None), ("high", "low", None),
    (None, None, "null"), (None, None, "finite"),
    (None, None, "tamper_cap"), (None, None, "tamper_revision"),
    (None, None, "refence_cap"), (None, None, "refence_revision"),
    (None, None, "refence_terminal"),
    (None, None, "refence_authority"),
])
async def test_delivery_reasoning_matches_approved_role_before_effects(
    tmp_path, monkeypatch, role, state, approved_effort, supplied_effort, monitoring
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
        allowed_tools=tools, budget=AgentBudget.from_model_policy(model).model_copy(update={
            "monitoring_policy_revision": 1 if monitoring else None,
            "max_total_tokens": 400 if monitoring in {"finite", "tamper_cap", "tamper_revision"}
            or (monitoring or "").startswith("refence") else None,
        }),
        reasoning_effort=supplied_effort,
    )
    admitted_at = datetime.now(UTC)
    admission = ExecutionAdmission(
        run_id=run.id,
        step_id=uuid4(), status=ExecutionStatus.RUNNING,
        kind="implement" if role is AgentRole.DEVELOPER else "review", role=role,
        agent_execution_id=request.execution_id, provider=model.provider, model=model.model,
        instruction_version="v1", input_artifact_id=uuid4(), attempt=1, admitted_at=admitted_at,
        transition_from=None, transition_to=None, is_new=False,
        monitoring_policy_revision=1 if monitoring else None,
        hard_token_cap=400 if monitoring in {"finite", "tamper_cap", "tamper_revision"}
        or (monitoring or "").startswith("refence") else None,
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

    if monitoring == "tamper_cap":
        request = request.model_copy(update={
            "budget": request.budget.model_copy(update={"max_total_tokens": 401})
        })
    elif monitoring == "tamper_revision":
        request = request.model_copy(update={
            "budget": request.budget.model_copy(update={"monitoring_policy_revision": None})
        })

    if monitoring in {"null", "finite", "refence_cap", "refence_revision",
                      "refence_terminal", "refence_authority"}:
        monkeypatch.setattr(
            "forge.worker.bound_delivery.build_adk_tools",
            lambda *_: tuple(BaseTool(name=name.value, description="test") for name in tools),
        )
        result = object()
        provider = SimpleNamespace(execute=AsyncMock(return_value=result))
        gateway_factory.return_value = provider

        async def make_tools(*_):
            if monitoring == "refence_cap":
                work.executions.get_admission.return_value = replace(admission, hard_token_cap=401)
            elif monitoring == "refence_revision":
                work.executions.get_admission.return_value = replace(
                    admission, monitoring_policy_revision=2
                )
            elif monitoring == "refence_terminal":
                work.executions.get_admission.return_value = None
            elif monitoring == "refence_authority":
                gateway._approved.load.return_value = SimpleNamespace(
                    run=replace(run, state=RunState.CANCELLED), policy=policy, plan=plan
                )
            return object()

        tool_factory.side_effect = make_tools

    if supplied_effort == approved_effort and monitoring in {
        "refence_cap", "refence_revision", "refence_terminal", "refence_authority"
    }:
        with pytest.raises(AgentGatewayError, match="^agent gateway execution failed$"):
            await gateway.execute(request)
        provider.execute.assert_not_awaited()
    elif supplied_effort == approved_effort and monitoring not in {"tamper_cap", "tamper_revision"}:
        assert await gateway._binding(request, work) == (approved, admission, tree)
        if monitoring:
            assert await gateway.execute(request) is result
            provider.execute.assert_awaited_once_with(request)
    else:
        with pytest.raises(AgentGatewayError, match="^agent gateway execution failed$"):
            await gateway.execute(request)
        work.events.list_for_version.assert_not_awaited()
    if not monitoring or monitoring.startswith("tamper"):
        tool_factory.assert_not_awaited()
        gateway_factory.assert_not_called()
