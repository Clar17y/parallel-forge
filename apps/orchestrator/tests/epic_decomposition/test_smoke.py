"""End-to-end providerless production smoke test for epic decomposition.

Flow:
1. Create project with policy version in DB.
2. Save and adopt brief revision with requirements.
3. Submit a decomposition authoring job.
4. Process job via worker execution with supervised gateway producing valid DecompositionProposal.
5. Observe completed job and verify proposal content.
6. Adopt proposal into the epic graph.
7. Launch a child work item as a normal delivery run via epic_run_bridge.
8. Verify child run creates delivery task, run, and initial planner command,
   preserving human review gates and normal delivery invariants.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.projects import RepositoryInspection
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import (
    EpicBrainstormService,
    EpicBriefBrainstormAdapter,
)
from forge.application.services.epic_brief import EpicBriefService
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.application.services.epic_run_bridge import EpicRunBridgeService
from forge.application.services.runs import RunService
from forge.domain.epic_brief import (
    BriefAdoptionRequest,
    BriefContent,
    BriefRequirement,
    BriefRevisionCreateRequest,
    EpicCreateRequest,
)
from forge.domain.epic_decomposition import DecompositionProposal
from forge.domain.epic_run_bridge import EpicLaunchConflict, LaunchRequest
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    RouteBinding,
    RouteSpec,
    TaskBudget,
)
from forge.persistence.models import (
    EpicExecution,
    EpicItemAttempt,
    Project,
    ProjectPolicyVersion,
    Run,
    RunCommand,
    RunEvent,
    Task,
)
from forge.persistence.repositories.epic_decomposition import (
    PostgresEpicDecompositionUnitOfWork,
)
from forge.persistence.repositories.epic_run_bridge import PostgresEpicRunBridgeRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.epic_brainstorm import EpicBrainstormWorker
from forge.worker.epic_decomposition import ValidatedDecompositionGateway
from sqlalchemy import func, select

from .support import SupervisedGateway


class _Inspector:
    sha = "c" * 40

    def inspect(self, **kwargs):
        return RepositoryInspection(
            canonical_path=kwargs["repository_path"],
            github_repository=kwargs["github_repository"],
            default_branch=kwargs["default_branch"],
            base_ref=f"refs/heads/{kwargs['default_branch']}",
            base_sha=self.sha,
        )


class _BridgeWork(PostgresUnitOfWork):
    async def __aenter__(self):
        await super().__aenter__()
        self.epic_run_bridge = PostgresEpicRunBridgeRepository(self.session)
        return self


@pytest.mark.asyncio
async def test_end_to_end_decomposition_to_normal_run_smoke(
    decomposition_session_factory, tmp_path: Path
) -> None:
    factory = decomposition_session_factory
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    project_id = uuid4()
    requirement_id = uuid4()

    # 1. Create project with policy version in DB
    async with factory() as session, session.begin():
        project = Project(
            id=project_id,
            canonical_path=f"/tmp/forge-{project_id}",
            github_repository=f"owner/repo-{project_id}",
            default_branch="main",
        )
        session.add(project)
        session.add(
            ProjectPolicyVersion(
                project_id=project_id,
                version=1,
                policy_digest="a" * 64,
                document_schema_version=1,
                document={},
            )
        )
        await session.flush()
        project.current_policy_version = 1

    # 2. Save and adopt brief revision with requirements
    brief_service = EpicBriefService(lambda: PostgresUnitOfWork(factory))
    epic = await brief_service.create(
        actor=actor,
        idempotency_key="create-epic",
        request=EpicCreateRequest(project_id=project_id, title="Integration Smoke Epic"),
    )
    assert epic.epic_id is not None

    brief_content = BriefContent(
        problem="Need end-to-end automated decomposition pipeline",
        outcomes=["Child work items executed safely with normal gates"],
        scope=["Epic decomposition to run execution"],
        exclusions=["External provider calling"],
        requirements=[
            BriefRequirement(
                requirement_id=requirement_id,
                text="Implement child delivery run bridge from decomposition item",
                acceptance_criteria=["Child run is created in PENDING status with planner command"],
            )
        ],
        decisions=["Use PostgreSQL durable state"],
        assumptions=["Standard operator trust applies"],
        open_questions=["Which child item to launch first?"],
    )
    brief_content.require_adoptable()
    brief_digest = canonical_digest(brief_content.model_dump(mode="json"))

    brief = await brief_service.save_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="save-brief",
        request=BriefRevisionCreateRequest(
            expected_epic_version=1,
            content=brief_content,
        ),
    )
    assert brief.content_digest == brief_digest

    adopted_brief = await brief_service.adopt_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="adopt-brief",
        request=BriefAdoptionRequest(
            expected_epic_version=2,
            brief_revision_id=brief.brief_revision_id,
            brief_digest=brief_digest,
        ),
    )
    assert adopted_brief.version == 3

    # 3. Submit a decomposition authoring job
    route = RouteSpec(
        provider="fake",
        client="fake",
        model="fixture",
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    authoring_service = EpicBrainstormService(
        factory,
        EpicBriefBrainstormAdapter,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    decomp_service = EpicDecompositionService(
        lambda: PostgresEpicDecompositionUnitOfWork(factory),
        authoring_service=authoring_service,
    )

    conv_id, conv_ver = await decomp_service.create(
        epic_id=epic.epic_id,
        project_id=project_id,
        actor=actor,
        key="conv-init",
        text="Decompose the accepted brief into actionable work items",
    )
    turns = await decomp_service.turns(
        epic_id=epic.epic_id,
        project_id=project_id,
        conversation_id=conv_id,
    )
    prompt_turn = turns[-1]

    job_receipt = await decomp_service.submit(
        epic_id=epic.epic_id,
        project_id=project_id,
        conversation_id=conv_id,
        prompt_turn_id=prompt_turn.turn_id,
        expected_epic_version=3,
        expected_conversation_version=conv_ver,
        actor=actor,
        key="submit-decomp-job",
    )
    assert job_receipt.state == "queued"

    # 4. Process the job via worker execution with a scripted supervised gateway
    worker = EpicBrainstormWorker(
        factory,
        owner=f"smoke-worker-{uuid4().hex}",
        gateway_factory=lambda _job: ValidatedDecompositionGateway(SupervisedGateway()),
        reader_factory=lambda _job: AsyncMock(),
        kinds=frozenset({"decomposition"}),
    )
    claimed = await worker.run_once()
    assert claimed == job_receipt.job_id
    await worker.drain()

    # 5. Observe the completed job and verify proposal content
    outcome = await decomp_service.observe(
        epic_id=epic.epic_id,
        project_id=project_id,
        job_id=job_receipt.job_id,
    )
    assert outcome.state == "proposed"
    assert outcome.process_settled is True
    assert outcome.proposal is not None
    assert isinstance(outcome.proposal, DecompositionProposal)
    assert len(outcome.proposal.items) >= 1
    child_item = outcome.proposal.items[0]
    assert requirement_id in child_item.source_requirement_ids

    # 6. Adopt the proposal into the epic graph
    adoption_res = await decomp_service.adopt(
        actor=actor,
        epic_id=epic.epic_id,
        project_id=project_id,
        job_id=job_receipt.job_id,
        expected_job_version=outcome.job_version,
        expected_epic_version=3,
        proposal_digest=outcome.proposal_digest,
        key="adopt-decomp-graph",
    )
    assert adoption_res.epic_version == 5
    assert adoption_res.graph_revision_id is not None
    assert adoption_res.graph_digest is not None

    # 7. Launch a child work item as a normal delivery run via epic_run_bridge
    inspector = _Inspector()
    bridge_factory = lambda: _BridgeWork(factory)
    run_service = RunService(
        bridge_factory, repository_inspector=inspector, data_root=str(tmp_path)
    )
    bridge = EpicRunBridgeService(bridge_factory, run_service=run_service)

    launch_req = LaunchRequest(
        expected_epic_version=adoption_res.epic_version,
        brief_revision_id=brief.brief_revision_id,
        brief_digest=brief_digest,
        graph_revision_id=adoption_res.graph_revision_id,
        graph_digest=adoption_res.graph_digest,
        item_id=child_item.item_id,
    )
    with pytest.raises(EpicLaunchConflict) as blocked:
        await bridge.launch(
            actor=actor,
            epic_id=epic.epic_id,
            idempotency_key="launch-child-default",
            request=launch_req,
        )
    assert "epic_usage_unknown" in blocked.value.blocker_codes
    attempt = await bridge.launch(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="launch-child-run",
        request=launch_req.model_copy(update={"owner_override": True}),
    )
    assert "epic_usage_unknown" in attempt.blocker_codes
    assert attempt.attempt_number == 1
    assert attempt.run_id is not None
    assert attempt.task_id is not None
    assert attempt.base_sha == inspector.sha

    # 8. Verify delivery task, run, initial planner command, and human review gates
    async with factory() as session:
        # Verify Task
        task = await session.get(Task, attempt.task_id)
        assert task is not None
        assert task.title == child_item.title
        assert task.project_id == project_id
        # Verify Run
        run = await session.get(Run, attempt.run_id)
        assert run is not None
        assert run.task_id == task.id
        assert run.state in {"CREATED", "PLANNING", "PENDING"}
        # Invariants: Human approvals are not pre-granted and review gates remain preserved
        assert run.pending_gate is None
        # Verify RunCommand (queued planner command)
        commands = (
            await session.scalars(select(RunCommand).where(RunCommand.run_id == run.id))
        ).all()
        assert len(commands) == 1
        planner_cmd = commands[0]
        assert planner_cmd.command_type == "start_planning"
        assert planner_cmd.status == "PENDING"
        # Verify RunEvent
        events = (await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all()
        assert len(events) == 1
        # Verify Bridge Records
        exec_count = await session.scalar(select(func.count()).select_from(EpicExecution))
        attempt_count = await session.scalar(select(func.count()).select_from(EpicItemAttempt))
        assert exec_count == 1
        assert attempt_count == 1
