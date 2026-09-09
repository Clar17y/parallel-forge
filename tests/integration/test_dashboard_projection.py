"""Project real local-delivery evidence into the operator cockpit."""

from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.projections import ProjectionService
from forge.application.services.review_decision import ReviewDecisionService
from forge.domain.run import RunState
from forge.persistence.models import PullRequest
from forge.persistence.queries.dashboard import DashboardQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_review import _review_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def test_cockpit_contains_actual_checks_independent_review_and_pull_request(
    tmp_path, workflow_session_factory
):
    factory = workflow_session_factory
    case, source, service, gateway, git, validation = await _review_case(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        review = await service.execute(source, work)
        await ReviewDecisionService(case.artifact_store, git_factory=lambda _: git).decide(
            source, work
        )
        run = await work.runs.get(case.run_id)
        validation_evidence = await work.evidence.get_by_id(
            validation.validation_evidence_set_id, run_id=run.id
        )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    projection_service = ProjectionService(DashboardQuery(factory))
    awaiting = await projection_service.run_projection(run.id, actor)
    approve = next(item for item in awaiting["available_commands"] if item["name"] == "approve_pr")
    assert awaiting["candidate"]["commit"] == git.head
    assert approve["evidence_digest"] == run.pending_evidence_digest
    assert approve["expected_run_version"] == run.version
    async with PostgresUnitOfWork(factory) as work:
        for state in (RunState.PUBLISHING_PR, RunState.MONITORING_PR):
            run = await work.runs.transition(run.id, run.version, state, "test.advance", {})
        await work.commit()
    async with factory() as session, session.begin():
        session.add(
            PullRequest(
                run_id=run.id,
                repository="owner/repo",
                branch=run.branch_name,
                base_ref=run.base_ref,
                pull_request_number=7,
                head_sha=git.head,
                base_sha=run.base_sha,
                checks={},
                review_state={},
                state="OPEN",
            )
        )
    result = await ProjectionService(DashboardQuery(factory)).run_projection(
        run.id, AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    )
    assert result is not None
    assert result["run"]["state"] == RunState.MONITORING_PR
    assert result["resource"]["worktree_path"] == run.worktree_path
    assert result["pull_request"]["head_sha"] == git.head
    assert result["candidate"]["validation_evidence_digest"] == validation_evidence.manifest_digest
    assert result["review"]["evidence_digest"] == review.manifest_digest
    assert result["checks"] and all(item["status"] == "PASSED" for item in result["checks"])
    assert result["agents"]["reviewer"]["independent"] is True
    assert result["agents"]["reviewer"]["execution_id"] == gateway.requests[0].execution_id
    assert result["usage"]["model_calls"] > 0
    assert result["next_gate"] == "merge"
    assert "approve_merge" not in {item["name"] for item in result["available_commands"]}
