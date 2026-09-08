"""Recovery loads frozen authority without repeating publication preflight."""

import pytest
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.pr_evidence import PrEvidenceValidationError, PrEvidenceValidator
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_pr_approval import authorized
from test_release_publication_resume import resumed_release
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("phase", ["historical", "paused", "body", "runner", "command"])
async def test_frozen_publication_recovery_authority(
    tmp_path, workflow_session_factory, monkeypatch, phase
):
    factory = workflow_session_factory
    case, git, github, handler, command, approval_id = await authorized(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        await handler(command, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(command.id, worker_id=command.lease_owner)
    source = await commands.claim_next(worker_id="publisher", lease_seconds=120)
    original_head = git.head
    git.head = "f" * 40

    def forbidden(*args, **kwargs):
        raise AssertionError("recovery must not inspect a live candidate")

    async def forbidden_read(*args, **kwargs):
        raise AssertionError("recovery must not preflight the current remote base")

    monkeypatch.setattr(github, "get_base", forbidden_read)
    validator = PrEvidenceValidator(
        case.artifact_store, ApprovedPlanLoader(case.artifact_store), forbidden, github
    )

    async def inspect():
        async with PostgresUnitOfWork(factory) as work:
            frozen = await validator.for_recovery(work, case.run_id, approval_id)
            assert frozen.evidence.candidate_commit == original_head
            assert frozen.body
            return frozen

    if phase == "paused":
        await resumed_release(case, source, factory, after_pause=inspect)
        return
    if phase == "command":
        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.get(RunCommand, source.id)
            row.payload = {"approval_id": str(approval_id), "extra": True}
            await work.commit()
        with pytest.raises(PrEvidenceValidationError):
            await inspect()
        return
    frozen = await inspect()
    if phase in {"body", "runner"}:
        original = case.artifact_store.open_bytes
        target = (
            frozen.evidence.body_digest if phase == "body" else frozen.evidence.runner_evidence_digest
        )

        async def corrupted(digest):
            return b"changed" if digest == target else await original(digest)

        monkeypatch.setattr(case.artifact_store, "open_bytes", corrupted)
        with pytest.raises(PrEvidenceValidationError):
            await inspect()
