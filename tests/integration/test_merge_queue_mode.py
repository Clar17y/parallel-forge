from uuid import uuid4

import pytest
from forge.application.handlers.merge import ApproveMergeHandler
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.merge import MergeController
from test_pr_monitoring import published
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("queue", [False, True])
async def test_consumed_merge_mode_comes_from_approved_observation(
    tmp_path, workflow_session_factory, queue
):
    factory = workflow_session_factory
    case, git, read, writes, publication, poll, policy, _ = await published(tmp_path, factory)
    key = policy.github_repository.casefold()
    read.checks[key, git.head] = (CheckSnapshot("ci", "completed", "success", head_sha=git.head),)
    read.merge_protections[key, "main"] = MergeProtection(
        True, queue, False, "classic", required_check_names=("ci",),
        merge_queue_method="squash" if queue else None,
    )
    async with PostgresUnitOfWork(factory) as work:
        await ReleaseMonitor(case.artifact_store, publication, read, writes)(poll, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(poll.id, worker_id=poll.lease_owner)
    approval_id, actor = uuid4(), uuid4()
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        work.session.add(Approval(
            id=approval_id, run_id=run.id, gate="merge",
            evidence_digest=run.pending_evidence_digest, run_version=run.version,
            policy_version=run.policy_version, authenticated_actor_id=actor,
        ))
        await work.commands.enqueue(
            run_id=run.id, command_type="approve_merge", idempotency_key=f"{run.id}:approve-merge",
            payload={"approval_id": str(approval_id)}, expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    validator = MergeEvidenceValidator(case.artifact_store, publication, MergeController(read, writes))
    command = await commands.claim_next(worker_id="queue-mode-approval", lease_seconds=120)
    async with PostgresUnitOfWork(factory) as work:
        await ApproveMergeHandler(validator)(command, work)
    # Mutable remote mode no longer selects the effect for this consumed approval.
    read.merge_protections[key, "main"] = MergeProtection(
        True, not queue, False, "changed", required_check_names=("ci",),
        merge_queue_method="squash" if not queue else None,
    )
    async with PostgresUnitOfWork(factory) as work:
        approved = await validator.consumed(work, case.run_id, approval_id, recheck=False)
        assert await validator.queue_required(work, case.run_id, approval_id, approved) is queue
