"""A durable monitoring admission reaches the production planning wrapper."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.errors import AgentGatewayError
from forge.agents.prompt_loader import PromptLoader
from forge.application.services.task_usage_policy import resolve_agent_budget_for_admission
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentRequest,
    PlannerInput,
    PolicySummary,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.task_usage_contract import MonitoringPolicy
from forge.domain.tool import ToolName
from forge.observability.redaction import Redactor
from forge.persistence.models import ProjectPolicyVersion, Run
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.composition import BoundPlanningGateway


@pytest.mark.asyncio
async def test_postgres_frozen_monitoring_admission_reaches_planning_provider(
    session_factory, persisted_run, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = ProjectPolicy(
        id=persisted_run.project_id,
        version=1,
        repository_path=str(repo),
        github_repository="forge/test",
        default_branch="main",
    )
    async with session_factory() as session:
        row = await session.get(Run, persisted_run.id)
        assert row is not None
        session.add(
            ProjectPolicyVersion(
                project_id=persisted_run.project_id,
                version=2,
                policy_digest="c" * 64,
                document_schema_version=1,
                document=policy.model_copy(update={"version": 2}).model_dump(mode="json"),
            )
        )
        row.policy_version = 2
        row.state = "PLANNING"
        row.base_sha = "a" * 40
        await session.commit()
    policy = policy.model_copy(update={"version": 2})

    async with PostgresUnitOfWork(session_factory) as work:
        await work.task_usage_policy.append_policy_revision(
            MonitoringPolicy(project_id=persisted_run.project_id),
            expected_revision=0,
            actor_id=uuid4(),
        )
        await work.commit()

    prompt_root = tmp_path / "prompts"
    role_dir = prompt_root / "planner"
    role_dir.mkdir(parents=True)
    role_dir.joinpath("instructions.md").write_text(
        "<!-- forge-instruction-version: v1 -->\nInstructions for planner\n", encoding="utf-8"
    )
    prompt_loader = PromptLoader(prompt_root)
    provider_requests: list[AgentRequest] = []

    class Provider:
        async def execute(self, request: AgentRequest) -> object:
            provider_requests.append(request)
            return object()

    gateway = BoundPlanningGateway(
        unit_of_work_factory=lambda: PostgresUnitOfWork(session_factory),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        prompt_loader=prompt_loader,
        redactor=Redactor(),
        underlying_gateway_factory=lambda _: Provider(),
    )
    task = UntrustedContent.from_text(
        "Do task", source_kind=UntrustedSourceKind.TASK, source_reference="task"
    )
    context = PlannerInput(
        original_task=task,
        base_commit="a" * 40,
        repository_tree=UntrustedContent.from_text(
            ".", source_kind=UntrustedSourceKind.REPOSITORY_TREE, source_reference="."
        ),
        relevant_instructions=(),
        policy_summary=PolicySummary.from_policy(policy),
    )
    instruction = "<!-- forge-instruction-version: v1 -->\nInstructions for planner\n"
    for revision, cap, attempt in ((1, None, 1), (2, 400, 2), (3, None, 3)):
        async with PostgresUnitOfWork(session_factory) as work:
            budget = await resolve_agent_budget_for_admission(
                work, persisted_run.project_id, policy.planner_model
            )
            admitted = await work.executions.admit(
                persisted_run.id,
                uuid4(),
                uuid4(),
                "plan",
                attempt,
                AgentRole.PLANNER,
                "v1",
                policy.planner_model.provider,
                policy.planner_model.model,
                monitoring_policy_revision=budget.monitoring_policy_revision,
                hard_token_cap=budget.max_total_tokens,
            )
            await work.commit()
        assert admitted.monitoring_policy_revision == revision
        assert admitted.hard_token_cap == cap
        request = AgentRequest(
            execution_id=admitted.agent_execution_id,
            run_id=persisted_run.id,
            task_id=persisted_run.task_id,
            role=AgentRole.PLANNER,
            context=context,
            provider=policy.planner_model.provider,
            model=policy.planner_model.model,
            instruction_version="v1",
            system_instruction=instruction,
            instruction_digest=hashlib.sha256(instruction.encode()).hexdigest(),
            allowed_tools=(
                ToolName.REPOSITORY_LIST_FILES,
                ToolName.REPOSITORY_READ_FILE,
                ToolName.REPOSITORY_SEARCH,
                ToolName.REPOSITORY_SEARCH_SEMANTIC,
                ToolName.REPOSITORY_READ_INSTRUCTIONS,
            ),
            budget=budget,
        )
        await gateway.execute(request)
        assert provider_requests[-1] == request
        tampered = request.model_copy(
            update={
                "budget": request.budget.model_copy(
                    update={
                        "max_total_tokens": 401 if cap == 400 else 400,
                    }
                )
            }
        )
        with pytest.raises(AgentGatewayError):
            await gateway.execute(tampered)
        assert provider_requests[-1] == request
        if revision in {1, 2}:
            async with PostgresUnitOfWork(session_factory) as work:
                await work.task_usage_policy.append_policy_revision(
                    MonitoringPolicy(
                        project_id=persisted_run.project_id,
                        revision=revision + 1,
                        hard_token_cap=400 if revision == 1 else None,
                    ),
                    expected_revision=revision,
                    actor_id=uuid4(),
                )
                await work.commit()
            await gateway.execute(request)
            assert provider_requests[-1] == request

    assert len(provider_requests) == 5
