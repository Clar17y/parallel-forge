"""Review decisions consume only the persisted reviewer evidence."""

import json
from dataclasses import replace

import pytest
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.review_decision import (
    ReviewDecisionRecoveryRequired,
    ReviewDecisionService,
)
from forge.domain.agent import ReviewDecision as AgentReviewDecision
from forge.domain.agent import ReviewOutput
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.domain.run import RunState
from forge.persistence.models import Artifact, ArtifactLineage, Run, RunCommand
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_delivery_review import _review_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("unicode_title", [False, True])
async def test_approved_review_freezes_pr_evidence_and_replays_without_mutation(
    tmp_path, workflow_session_factory, unicode_title
):
    case, command, reviewer, _gateway, git, validation = await _review_case(
        tmp_path, workflow_session_factory
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        review = await reviewer.execute(command, work)

    class UnicodeTitleLoader(ApprovedPlanLoader):
        async def load(self, work, run_id):
            approved = await super().load(work, run_id)
            # Inject approved Unicode text at the loader boundary, preserving
            # the immutable task rows created by the shared workflow fixture.
            return replace(approved, task=replace(approved.task, title="Improve café support"))

    service = ReviewDecisionService(
        case.artifact_store,
        git_factory=lambda _policy: git,
        approved_plans=UnicodeTitleLoader(case.artifact_store) if unicode_title else None,
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        result = await service.decide(command, work)
    assert result.state is RunState.AWAITING_PR_APPROVAL
    async with workflow_session_factory() as session:
        evidence = await session.execute(
            select(Artifact, ArtifactLineage)
            .join(ArtifactLineage, ArtifactLineage.artifact_id == Artifact.id)
            .where(
                ArtifactLineage.run_id == case.run_id,
                ArtifactLineage.producer_kind == "pr_approval_evidence",
            )
        )
        artifact, _lineage = evidence.one()
        body = await session.execute(
            select(Artifact)
            .join(ArtifactLineage)
            .where(
                ArtifactLineage.run_id == case.run_id,
                ArtifactLineage.producer_kind == "pr_approval_body",
            )
        )
        body_artifact = body.scalar_one()
        run = await session.get(Run, case.run_id)
    assert review.evidence_set_id == result.review_evidence_set_id
    assert run is not None and run.pending_evidence_digest == artifact.digest
    assert body_artifact.digest != artifact.digest
    frozen = json.loads(await case.artifact_store.open_bytes(artifact.digest))
    runner = json.loads(await case.artifact_store.open_bytes(frozen["runner_evidence_digest"]))
    assert runner["head_sha"] == git.head
    assert runner["results"]
    assert all(item["result"]["runner_mode"] == "docker" for item in runner["results"])
    assert all(item["result"]["image_digest"].startswith("sha256:") for item in runner["results"])
    assert frozen["runner_evidence_digest"] != frozen["validation_digest"]
    assert (
        "## Approved plan" in (await case.artifact_store.open_bytes(body_artifact.digest)).decode()
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        replay = await service.decide(command, work)
    assert replay == result
    assert validation.validation_evidence_set_id


async def _decide_case(tmp_path, factory, *, severity=None, exhausted=False):
    case, command, reviewer, gateway, git, _validation = await _review_case(tmp_path, factory)
    if severity is not None:
        original = gateway.execute

        async def with_finding(request):
            result = await original(request)
            return result.model_copy(
                update={
                    "output": ReviewOutput(
                        decision=AgentReviewDecision.REQUEST_CHANGES,
                        findings=(
                            ReviewFinding(
                                finding_id="R1",
                                severity=severity,
                                path="a",
                                start_line=1,
                                summary="A finding",
                                evidence="line 1",
                            ),
                        ),
                        tested_claims=("named checks",),
                        missing_evidence=(),
                        summary="Reviewed",
                    )
                }
            )

        gateway.execute = with_finding
    async with PostgresUnitOfWork(factory) as work:
        review = await reviewer.execute(command, work)
    if exhausted:
        async with factory() as session, session.begin():
            (await session.get(Run, case.run_id)).local_remediation_count = 3
    return (
        case,
        command,
        ReviewDecisionService(case.artifact_store, git_factory=lambda _: git),
        git,
        review,
    )


@pytest.mark.parametrize(
    "severity,exhausted,target,count",
    [
        (FindingSeverity.MAJOR, False, RunState.REMEDIATING, 1),
        (FindingSeverity.MAJOR, True, RunState.AWAITING_HUMAN_INTERVENTION, 3),
        (FindingSeverity.SUGGESTION, False, RunState.AWAITING_PR_APPROVAL, 0),
    ],
)
async def test_policy_and_budget_determine_review_decision(
    tmp_path, workflow_session_factory, severity, exhausted, target, count
):
    factory = workflow_session_factory
    case, command, service, _git, review = await _decide_case(
        tmp_path, factory, severity=severity, exhausted=exhausted
    )
    async with PostgresUnitOfWork(factory) as work:
        result = await service.decide(command, work)
    assert result.state is target
    async with factory() as session:
        assert (await session.get(Run, case.run_id)).local_remediation_count == count
        queued = await session.scalar(
            select(RunCommand).where(
                RunCommand.run_id == case.run_id, RunCommand.command_type == "remediate"
            )
        )
    if target is RunState.REMEDIATING:
        assert queued.payload["prior_review_evidence_set_id"] == str(review.evidence_set_id)
        assert queued.expected_run_version == result.version
    else:
        assert queued is None
    async with PostgresUnitOfWork(factory) as work:
        assert await service.decide(command, work) == result


async def test_review_decision_replay_rejects_changed_queue(tmp_path, workflow_session_factory):
    factory = workflow_session_factory
    case, command, service, _git, _review = await _decide_case(
        tmp_path, factory, severity=FindingSeverity.MAJOR
    )
    async with PostgresUnitOfWork(factory) as work:
        await service.decide(command, work)
    async with factory() as session, session.begin():
        queued = await session.scalar(
            select(RunCommand).where(
                RunCommand.run_id == case.run_id, RunCommand.command_type == "remediate"
            )
        )
        queued.payload = {"semantic_attempt": 99}
    async with PostgresUnitOfWork(factory) as work:
        with pytest.raises(ReviewDecisionRecoveryRequired):
            await service.decide(command, work)


async def test_candidate_changed_during_freeze_does_not_open_pr_gate(
    tmp_path, workflow_session_factory
):
    factory = workflow_session_factory
    case, command, service, git, _review = await _decide_case(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        original = work.artifacts.record

        async def change_candidate(*args, **kwargs):
            result = await original(*args, **kwargs)
            if kwargs.get("producer_type") == "pr_approval_evidence":
                git.head = "c" * 40
            return result

        work.artifacts.record = change_candidate
        with pytest.raises(ReviewDecisionRecoveryRequired):
            await service.decide(command, work)
    async with factory() as session:
        assert (await session.get(Run, case.run_id)).state == RunState.REVIEWING.value
