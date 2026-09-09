"""Reviewer execution uses the exact validation command evidence."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from forge.application.ports.worktrees import GitCandidateDiff, GitDiff
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.review import ReviewRecoveryRequired, ReviewService
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus, AgentResult, ReviewDecision, ReviewOutput
from forge.domain.run import RunState
from forge.domain.tool import ToolName
from forge.observability.usage import UsageRecord
from forge.persistence.models import (
    AgentExecution,
    AgentExecutionEvidenceInput,
    Artifact,
    ArtifactLineage,
    EvidenceSet,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_decisions import _decision_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


class _Reader:
    def read_instructions(self, _target):
        return ()


class _Gateway:
    def __init__(self, factory):
        self.factory, self.requests, self.admitted = factory, [], False

    async def execute(self, request):
        self.requests.append(request)
        async with self.factory() as session:
            row = await session.get(AgentExecution, request.execution_id)
            self.admitted = row is not None and row.status == "RUNNING"
            assert row is not None and row.instruction_digest == request.instruction_digest
            binding = await session.get(
                AgentExecutionEvidenceInput, (request.execution_id, "validation_results")
            )
            assert binding is not None and binding.run_id == request.run_id
            assert (
                str(binding.evidence_set_id) == request.context.check_evidence[0].source_reference
            )
        assert request.role is AgentRole.REVIEWER
        assert request.parent_execution_id is None
        assert ToolName.GIT_COMMIT not in request.allowed_tools
        assert ToolName.REPOSITORY_WRITE_FILE not in request.allowed_tools
        return AgentResult(
            execution_id=request.execution_id,
            role=request.role,
            finish_status=AgentFinishStatus.SUCCEEDED,
            provider=request.provider,
            model=request.model,
            instruction_digest=request.instruction_digest,
            output=ReviewOutput(
                decision=ReviewDecision.APPROVE,
                tested_claims=("named checks",),
                missing_evidence=(),
                summary="reviewed",
            ),
            usage=UsageRecord(
                provider=request.provider,
                model=request.model,
                prompt_version=request.instruction_version,
                pricing_version="fixture-v1",
                estimated_cost_minor=1,
                currency="USD",
            ),
            tool_call_count=0,
            duration_ms=0,
        )


async def _review_case(tmp_path, workflow_session_factory):
    case, validate, delivery, _runner = await _decision_case(tmp_path, workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        decision = await delivery.validate(validate, work)
    await PostgresCommandRepository(workflow_session_factory).complete(
        validate.id, worker_id="test-worker"
    )
    # Use the actual queued review command; the helper's controller has already
    # established the validation evidence and reviewing state.
    repository = PostgresCommandRepository(workflow_session_factory)
    command = await repository.claim_next(worker_id="reviewer", lease_seconds=60)
    assert command is not None and command.command_type == "review"
    git = delivery._git_factory(None)
    git.candidate_diff = lambda tree: GitCandidateDiff(
        head_sha=git.head_sha(tree),
        diff=GitDiff(text="diff --git a/a b/a\n", original_byte_count=20, truncated=False),
        changed_paths=("a",),
    )
    gateway = _Gateway(workflow_session_factory)
    service = ReviewService(
        gateway,
        case.artifact_store,
        __import__("forge.agents.prompt_loader", fromlist=["PromptLoader"]).PromptLoader(
            Path(__file__).resolve().parents[2] / "agents"
        ),
        ApprovedPlanLoader(case.artifact_store),
        lambda _policy: git,
        lambda _policy, _tree: _Reader(),
    )
    return case, command, service, gateway, git, decision


async def test_review_is_fresh_bound_and_persists_canonical_evidence(
    tmp_path, workflow_session_factory
):
    _case, command, service, gateway, _git, decision = await _review_case(
        tmp_path, workflow_session_factory
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        evidence = await service.execute(command, work)
    from fastapi import FastAPI
    from forge.api.dependencies import require_operator
    from forge.api.routes.artifacts import router_for
    from forge.application.services.artifact_reads import ArtifactReadService
    from forge.persistence.queries.artifacts import PostgresArtifactReadQuery
    from httpx import ASGITransport, AsyncClient

    async with workflow_session_factory() as session:
        execution = await session.get(AgentExecution, gateway.requests[0].execution_id)
        context_artifact = await session.get(Artifact, execution.input_artifact_id)
    app = FastAPI()
    app.state.artifact_read_service = ArtifactReadService(
        PostgresArtifactReadQuery(workflow_session_factory), _case.artifact_store
    )
    app.dependency_overrides[require_operator] = lambda: object()
    app.include_router(router_for(), prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/artifacts/{context_artifact.digest}/reviewer-diff")
    assert response.status_code == 200, response.text
    assert response.json()["text"] == gateway.requests[0].context.current_diff.content
    assert response.json()["diff_digest"] == gateway.requests[0].context.current_diff.content_digest
    assert response.json()["run_id"] == str(command.run_id)
    assert response.json()["producer_execution_id"] == str(execution.id)
    assert response.json()["validation_evidence_set_id"] == str(decision.validation_evidence_set_id)
    assert response.json()["truncated"] is False
    assert gateway.admitted and gateway.requests[0].parent_execution_id is None
    assert gateway.requests[0].context.check_evidence
    async with workflow_session_factory() as session:
        review = await session.get(EvidenceSet, evidence.evidence_set_id)
    assert (
        review is not None
        and review.validation_evidence_set_id == decision.validation_evidence_set_id
    )


async def test_wrong_validation_binding_never_calls_gateway(tmp_path, workflow_session_factory):
    case, validate, delivery, _runner = await _decision_case(tmp_path, workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await delivery.validate(validate, work)
    await PostgresCommandRepository(workflow_session_factory).complete(
        validate.id, worker_id="test-worker"
    )

    command = await PostgresCommandRepository(workflow_session_factory).claim_next(
        worker_id="reviewer", lease_seconds=60
    )
    assert command is not None
    command = replace(
        command,
        payload={
            "semantic_attempt": 1,
            "validation_evidence_set_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    gateway = _Gateway(workflow_session_factory)
    service = ReviewService(
        gateway,
        case.artifact_store,
        __import__("forge.agents.prompt_loader", fromlist=["PromptLoader"]).PromptLoader(
            Path(__file__).resolve().parents[2] / "agents"
        ),
        ApprovedPlanLoader(case.artifact_store),
        delivery._git_factory,
        lambda _policy, _tree: _Reader(),
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewRecoveryRequired):
            await service.execute(command, work)
    assert gateway.requests == []


@pytest.mark.parametrize("phase", ("context", "gateway", "artifact"))
async def test_changed_candidate_cannot_admit_or_publish_review(
    tmp_path, workflow_session_factory, phase
):
    case, command, service, gateway, git, _decision = await _review_case(
        tmp_path, workflow_session_factory
    )
    if phase == "context":
        original_context = service._context

        async def changed_context(*args):
            result = await original_context(*args)
            git.head = "c" * 40
            return result

        service._context = changed_context
    elif phase == "gateway":
        original_gateway = gateway.execute

        async def changed_gateway(request):
            result = await original_gateway(request)
            git.head = "c" * 40
            return result

        gateway.execute = changed_gateway
    else:
        original_put = case.artifact_store.put_bytes

        async def changed_put(data, **kwargs):
            result = await original_put(data, **kwargs)
            if kwargs.get("media_type") == "application/vnd.forge.evidence-manifest+json":
                git.head = "c" * 40
            return result

        case.artifact_store.put_bytes = changed_put
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewRecoveryRequired):
            await service.execute(command, work)
    assert len(gateway.requests) == (0 if phase == "context" else 1)
    async with workflow_session_factory() as session:
        evidence = await session.scalars(
            select(EvidenceSet).where(
                EvidenceSet.run_id == case.run_id, EvidenceSet.kind == "review"
            )
        )
        assert list(evidence) == []


async def test_invalid_reviewer_identity_intervenes_without_evidence(
    tmp_path, workflow_session_factory
):
    case, command, service, gateway, _git, _decision = await _review_case(
        tmp_path, workflow_session_factory
    )
    original = gateway.execute

    async def invalid(request):
        result = await original(request)
        return result.model_copy(update={"execution_id": request.run_id})

    gateway.execute = invalid
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewRecoveryRequired):
            await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert (await work.runs.get(case.run_id)).state is RunState.AWAITING_HUMAN_INTERVENTION
    assert len(gateway.requests) == 1


async def test_uncertain_reviewer_is_not_reinvoked(tmp_path, workflow_session_factory):
    _case, command, service, gateway, _git, _decision = await _review_case(
        tmp_path, workflow_session_factory
    )

    async def uncertain(request):
        gateway.requests.append(request)
        raise RuntimeError("unknown outcome")

    gateway.execute = uncertain
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(RuntimeError):
            await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewRecoveryRequired):
            await service.execute(command, work)
    assert len(gateway.requests) == 1


async def test_completed_review_replays_same_evidence_without_provider(
    tmp_path, workflow_session_factory
):
    _case, command, service, gateway, _git, _decision = await _review_case(
        tmp_path, workflow_session_factory
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        first = await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        replay = await service.execute(command, work)
    assert first == replay
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("target", (RunState.PAUSED, RunState.CANCELLED))
async def test_suspended_reviewer_retains_known_result_without_publishing(
    tmp_path, workflow_session_factory, target
):
    case, command, service, gateway, _git, _decision = await _review_case(
        tmp_path, workflow_session_factory
    )
    original = gateway.execute

    async def suspended(request):
        result = await original(request)
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            run = await work.runs.get(case.run_id)
            if target is RunState.PAUSED:
                await work.runs.pause(run.id, run.version, "test.paused", {})
            else:
                await work.runs.transition(run.id, run.version, target, "test.cancelled", {})
            await work.commit()
        return result

    gateway.execute = suspended
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewRecoveryRequired):
            await service.execute(command, work)
    async with workflow_session_factory() as session:
        digest = await session.scalar(
            select(Artifact.digest)
            .join(ArtifactLineage, ArtifactLineage.artifact_id == Artifact.id)
            .where(
                ArtifactLineage.run_id == case.run_id,
                ArtifactLineage.producer_kind == "review_late_result",
                ArtifactLineage.producer_id == gateway.requests[0].execution_id,
            )
        )
        assert digest is not None
        assert (
            await session.scalar(
                select(EvidenceSet).where(
                    EvidenceSet.run_id == case.run_id, EvidenceSet.kind == "review"
                )
            )
            is None
        )
    retained = json.loads(await case.artifact_store.open_bytes(digest))
    assert retained["output"]["decision"] == "approve"
    assert retained["usage"][0]["estimated_cost_minor"] == 1
