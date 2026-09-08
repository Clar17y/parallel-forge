"""PR publication consumes the exact evidence frozen by review decision."""

import pytest
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.pr_evidence import PrEvidenceValidationError, PrEvidenceValidator
from forge.application.services.review_decision import ReviewDecisionService
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.fake_github import FakeGitHub
from test_delivery_review import _review_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _frozen(tmp_path, factory):
    case, command, reviewer, _gateway, git, _validation = await _review_case(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        await reviewer.execute(command, work)
    async with PostgresUnitOfWork(factory) as work:
        await ReviewDecisionService(case.artifact_store, git_factory=lambda _policy: git).decide(
            command, work
        )
    await PostgresCommandRepository(factory).complete(command.id, worker_id=command.lease_owner)
    async with PostgresUnitOfWork(factory) as work:
        repository = (
            await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)
        ).policy.github_repository
    github = FakeGitHub()
    github.bases[(repository.casefold(), "main")] = git.worktree.base_sha
    return case, git, github


async def test_validator_reuses_real_frozen_pr_evidence(tmp_path, workflow_session_factory):
    case, git, github = await _frozen(tmp_path, workflow_session_factory)
    validator = PrEvidenceValidator(
        case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _policy: git, github
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        result = await validator.validate(work, case.run_id)
    assert result.evidence.candidate_commit == git.head
    assert result.body.startswith(b"## Task")
    assert result.remote_base_sha == git.worktree.base_sha


async def test_validator_labels_local_candidate_drift_separately(
    tmp_path, workflow_session_factory
):
    case, git, github = await _frozen(tmp_path, workflow_session_factory)
    git.head = "c" * 40
    validator = PrEvidenceValidator(
        case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _policy: git, github
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(PrEvidenceValidationError, match="content_drift"):
            await validator.validate(work, case.run_id)


async def test_validator_requires_the_actual_command_receipts(
    tmp_path, workflow_session_factory, monkeypatch
):
    from forge.domain.evidence import decode_evidence_manifest

    case, git, github = await _frozen(tmp_path, workflow_session_factory)
    validator = PrEvidenceValidator(
        case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _: git, github
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        valid = await validator.validate(work, case.run_id)
    manifest = decode_evidence_manifest(
        await case.artifact_store.open_bytes(valid.evidence.validation_digest)
    )
    unavailable = manifest.members[0].command_result_digest
    original = case.artifact_store.open_bytes

    async def missing_receipt(digest, **kwargs):
        if digest == unavailable:
            raise FileNotFoundError()
        return await original(digest, **kwargs)

    monkeypatch.setattr(case.artifact_store, "open_bytes", missing_receipt)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(PrEvidenceValidationError):
            await validator.validate(work, case.run_id)
