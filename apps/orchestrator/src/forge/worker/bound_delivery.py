"""Bind delivery agents to their durable role, context and managed worktree."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from forge.agents.adk_gateway import BoundAdkTools
from forge.agents.errors import AgentGatewayError
from forge.agents.prompt_loader import PromptLoader
from forge.agents.tool_bridge import build_adk_tools
from forge.application.ports.agents import AgentGateway
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.executions import ExecutionAdmission, ExecutionStatus
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.application.services.tools import CapabilityMatrix, ControlledToolService
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentBudget, AgentRequest, AgentResult, DeveloperInput, ReviewerInput
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.tool import ToolAuthorizationContext, ToolName
from forge.worker.agent_tools import PerRequestToolProvider


class BoundDeliveryGateway:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        artifact_store: ArtifactStore,
        prompt_loader: PromptLoader,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
        tool_service_factory: Callable[
            [AgentRequest, ApprovedPlan, ManagedWorktree], Awaitable[ControlledToolService]
        ],
        gateway_factory: Callable[[PerRequestToolProvider], AgentGateway],
    ) -> None:
        self._uow = unit_of_work_factory
        self._store = artifact_store
        self._prompts = prompt_loader
        self._git = git_factory
        self._tools = tool_service_factory
        self._gateway = gateway_factory
        self._approved = ApprovedPlanLoader(artifact_store)

    async def execute(self, request: AgentRequest) -> AgentResult:
        if type(request) is not AgentRequest or request.role not in {
            AgentRole.DEVELOPER,
            AgentRole.REVIEWER,
        }:
            raise AgentGatewayError("delivery role is invalid")
        try:
            async with self._uow() as work:
                approved, admission, tree = await self._binding(request, work)
                await work.commit()
            service = await self._tools(request, approved, tree)
            context = ToolAuthorizationContext(
                role=request.role,
                run_id=request.run_id,
                worktree_id=tree.identity.worktree_name,
                policy_version=approved.policy.version,
                agent_execution_id=request.execution_id,
                step_id=admission.step_id,
            )
            tools = build_adk_tools(service, context)
            names = tuple(ToolName(tool.name) for tool in tools)
            if names != request.allowed_tools:
                raise AgentGatewayError("delivery tools differ from role")
            # Environment/tool construction may await external dependencies.
            # Refence the same durable admission after that work, before provider use.
            async with self._uow() as work:
                current, current_admission, current_tree = await self._binding(request, work)
                if current != approved or current_admission != admission or current_tree != tree:
                    raise AgentGatewayError("delivery authority changed")
                await work.commit()
            self._prompts.verify_unchanged(request)
        except AgentGatewayError:
            raise
        except Exception:  # noqa: BLE001 - configuration/authority errors contain no source diagnostics
            raise AgentGatewayError("delivery binding is unavailable") from None
        provider = PerRequestToolProvider(BoundAdkTools(names=names, tools=tools), request)
        return await self._gateway(provider).execute(request)

    async def _binding(
        self,
        request: AgentRequest,
        work: UnitOfWork,
    ) -> tuple[ApprovedPlan, ExecutionAdmission, ManagedWorktree]:
        approved = await self._approved.load(work, request.run_id)
        run = approved.run
        developer = request.role is AgentRole.DEVELOPER
        allowed_states = (
            {RunState.IMPLEMENTING, RunState.REMEDIATING} if developer else {RunState.REVIEWING}
        )
        model = approved.policy.developer_model if developer else approved.policy.reviewer_model
        capabilities = CapabilityMatrix().capabilities_for(request.role)
        expected_tools = tuple(tool for tool in ToolName if tool in capabilities)
        admission = await work.executions.get_admission(request.run_id, request.execution_id)
        if (
            run.task_id != request.task_id
            or run.state not in allowed_states
            or request.parent_execution_id is not None
            or request.provider != model.provider
            or request.model != model.model
            or request.budget != AgentBudget.from_model_policy(model)
            or request.allowed_tools != expected_tools
            or admission is None
            or admission.status is not ExecutionStatus.RUNNING
            or admission.kind != ("implement" if developer else "review")
            or admission.role is not request.role
            or admission.agent_execution_id != request.execution_id
            or admission.provider != request.provider
            or admission.model != request.model
            or admission.instruction_version != request.instruction_version
            or admission.input_artifact_id is None
            or not run.branch_name
            or not run.worktree_path
            or not run.base_sha
        ):
            raise AgentGatewayError("delivery admission differs")
        admitted = [
            event
            for event in await work.events.list_for_version(run.id, run.version)
            if event.event_type == "agent_execution.admitted"
            and event.payload.get("agent_execution_id") == str(request.execution_id)
        ]
        if (
            len(admitted) != 1
            or admitted[0].actor_class != "system"
            or admitted[0].actor_id is not None
            or admitted[0].occurred_at != admission.admitted_at
            or admitted[0].payload
            != {
                "step_id": str(admission.step_id),
                "agent_execution_id": str(request.execution_id),
                "kind": admission.kind,
                "attempt": admission.attempt,
                "role": request.role.value,
                "status": "RUNNING",
                "instruction_version": request.instruction_version,
                "provider": request.provider,
                "model": request.model,
                "input_artifact_id": str(admission.input_artifact_id),
            }
        ):
            raise AgentGatewayError("delivery admission version differs")
        artifacts = await work.artifacts.get_by_producer(
            run_id=run.id,
            producer_type="developer_context" if developer else "reviewer_context",
            producer_id=request.execution_id,
        )
        wire = json.dumps(
            request.context.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        # Older admissions contain the canonical context directly. Fresh
        # admissions bind that context to an execution, allowing identical
        # inputs to retain distinct immutable producer lineage on resume.
        if len(artifacts) == 1:
            retained = await self._store.open_bytes(artifacts[0].digest)
            if retained != wire:
                wire = json.dumps(
                    {
                        "schema_version": 1,
                        "execution_id": str(request.execution_id),
                        "context": request.context.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
        if (
            len(artifacts) != 1
            or artifacts[0].artifact_id != admission.input_artifact_id
            or artifacts[0].digest != hashlib.sha256(wire).hexdigest()
            or artifacts[0].media_type != "application/json"
            or artifacts[0].byte_count != len(wire)
            or artifacts[0].truncated
            or await self._store.open_bytes(artifacts[0].digest) != wire
        ):
            raise AgentGatewayError("delivery context differs")
        identity = WorktreeIdentity.for_run(
            run.project_id, run.id, run.branch_name, approved.policy.database.enabled
        )
        tree = ManagedWorktree(
            identity=identity, path=Path(run.worktree_path), base_sha=run.base_sha
        )
        git = self._git(approved.policy)
        if git.inspect_worktree(identity, run.base_sha) != tree or not git.is_ancestor(tree):
            raise AgentGatewayError("delivery worktree differs")
        if developer:
            if (
                not isinstance(request.context, DeveloperInput)
                or request.context.worktree_id != identity.worktree_name
                or request.context.base_commit != run.base_sha
                or request.context.plan != approved.plan
            ):
                raise AgentGatewayError("developer context authority differs")
        elif (
            not isinstance(request.context, ReviewerInput) or request.context.plan != approved.plan
        ):
            raise AgentGatewayError("reviewer context authority differs")
        return approved, admission, tree
