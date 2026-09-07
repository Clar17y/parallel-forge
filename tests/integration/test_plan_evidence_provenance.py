"""PostgreSQL proof that plan approval evidence names its producing attempt."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from forge.application.ports.projects import RepositoryInspection
from forge.application.services.plan_evidence import (
    PlanEvidenceValidationError,
    PlanEvidenceValidator,
)
from forge.domain.approval import PlanApprovalEvidence
from forge.domain.plan import PlanOutput
from forge.persistence.models import AgentExecution, Project, Run
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_planning_failed_usage import _build_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414 - export fixture
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


class _StoredBaseInspector:
    def __init__(self, project: Project) -> None:
        self._project = project

    def inspect(self, **_kwargs: object) -> RepositoryInspection:
        return RepositoryInspection(
            canonical_path=self._project.canonical_path,
            github_repository=self._project.github_repository,
            default_branch=self._project.default_branch,
            base_ref="refs/heads/main",
            base_sha="a" * 40,
        )


async def test_rejects_alternate_plan_with_genuine_execution_producer(
    tmp_path, workflow_session_factory
):
    case = await _build_case(tmp_path, workflow_session_factory, fail_invalid=False)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await case.service.execute(case.command, work)

    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        project = await session.get(Project, run.project_id)
        execution = (
            await session.scalars(
                select(AgentExecution).where(AgentExecution.run_id == case.run_id)
            )
        ).one()
        evidence_digest = run.pending_evidence_digest
    original_evidence = PlanApprovalEvidence.model_validate_json(
        await case.artifact_store.open_bytes(evidence_digest)
    )
    original_plan = PlanOutput.model_validate_json(
        await case.artifact_store.open_bytes(original_evidence.plan_digest)
    )
    alternate_plan = original_plan.model_copy(update={"summary": "Unapproved alternate plan."})
    alternate_plan_bytes = json.dumps(
        alternate_plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    alternate_plan_descriptor = await case.artifact_store.put_bytes(
        alternate_plan_bytes, media_type="application/json"
    )
    alternate_evidence = original_evidence.model_copy(
        update={"plan_digest": alternate_plan_descriptor.digest}
    )
    alternate_evidence_bytes = json.dumps(
        alternate_evidence.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    alternate_evidence_descriptor = await case.artifact_store.put_bytes(
        alternate_evidence_bytes, media_type="application/json"
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await work.artifacts.record(
            alternate_plan_descriptor,
            run_id=case.run_id,
            producer_type="implementation_plan",
            producer_id=uuid4(),
        )
        await work.artifacts.record(
            alternate_evidence_descriptor,
            run_id=case.run_id,
            producer_type="plan_approval_evidence",
            producer_id=execution.id,
            parent_digests=(alternate_plan_descriptor.digest,),
        )
        await work.commit()
    async with workflow_session_factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        run.pending_evidence_digest = alternate_evidence_descriptor.digest

    validator = PlanEvidenceValidator(
        case.artifact_store, _StoredBaseInspector(project), data_root=str(tmp_path)
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(PlanEvidenceValidationError):
            await validator.validate(work, case.run_id)


@pytest.mark.parametrize("mutation", ["producer", "parent"])
async def test_rejects_corrupt_evidence_metadata_with_genuine_plan(
    tmp_path, workflow_session_factory, mutation, monkeypatch
):
    case = await _build_case(tmp_path, workflow_session_factory, fail_invalid=False)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        original_record = work.artifacts.record

        async def record_with_corrupt_metadata(descriptor, **kwargs):
            if kwargs.get("producer_type") == "plan_approval_evidence":
                if mutation == "producer":
                    kwargs["producer_id"] = uuid4()
                else:
                    kwargs["parent_digests"] = ()
            return await original_record(descriptor, **kwargs)

        # Corrupt the first insertion, retaining the real immutable database
        # constraints and genuine Planner output. Only one lineage field differs.
        monkeypatch.setattr(work.artifacts, "record", record_with_corrupt_metadata)
        await case.service.execute(case.command, work)
    async with workflow_session_factory() as session:
        run = await session.get(Run, case.run_id)
        project = await session.get(Project, run.project_id)
        evidence_digest = run.pending_evidence_digest
    evidence = PlanApprovalEvidence.model_validate_json(
        await case.artifact_store.open_bytes(evidence_digest)
    )
    validator = PlanEvidenceValidator(
        case.artifact_store, _StoredBaseInspector(project), data_root=str(tmp_path)
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        outcome = await work.executions.get_outcome(case.run_id, "plan", evidence.plan_attempt)
        plan = await work.artifacts.get_by_digest(evidence.plan_digest, run_id=case.run_id)
        assert outcome.output_artifact_id == plan.artifact_id
        with pytest.raises(PlanEvidenceValidationError):
            await validator.validate(work, case.run_id)
