"""Remote remediation keeps its monitor authority across operator resume."""

import hashlib
from pathlib import Path

import pytest
from forge.agents.prompt_loader import PromptLoader
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.development import DevelopmentRecoveryRequired, DevelopmentService
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_development import _Gateway, _Reader
from test_pr_monitoring import published
from test_release_publication_resume import resumed_release
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("mode", ["once", "repeated", "approval_invalid"])
async def test_remote_developer_resume_keeps_observation_and_remote_budget(
    tmp_path, workflow_session_factory, mode
):
    factory = workflow_session_factory
    case, git, read, writes, validator, poll, policy, approval_id = await published(tmp_path, factory)
    repository = policy.github_repository.casefold()
    read.checks[repository, git.head] = (
        CheckSnapshot("ci", "completed", "failure", head_sha=git.head, summary="Repair this check"),
    )
    read.merge_protections[repository, "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )
    async with PostgresUnitOfWork(factory) as work:
        await ReleaseMonitor(case.artifact_store, validator, read, writes)(poll, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(poll.id, worker_id=poll.lease_owner)
    source = await commands.claim_next(worker_id="remote-source", lease_seconds=120)
    assert source.command_type == "remediate_remote"
    command = await resumed_release(case, source, factory)
    if mode == "repeated":
        command = await resumed_release(case, command, factory)

    class Gateway(_Gateway):
        async def execute(self, request):
            result = await super().execute(request)
            git.head = "c" * 40
            candidate = git.candidate_diff(git.worktree)
            return result.model_copy(update={"output": result.output.model_copy(update={
                "local_commit_sha": git.head,
                "diff_digest": hashlib.sha256(candidate.diff.text.encode()).hexdigest(),
                "changed_paths": candidate.changed_paths,
            })})

    gateway = Gateway(factory)
    service = DevelopmentService(
        gateway, case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store), lambda _: git, lambda _p, _w: _Reader(),
    )
    if mode == "approval_invalid":
        from datetime import UTC, datetime

        async with PostgresUnitOfWork(factory) as work:
            approval = await work.auth.get_approval(approval_id=approval_id)
            approval.invalidated_at = datetime.now(UTC)
            await work.commit()
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(DevelopmentRecoveryRequired):
                await service.execute(command, work)
        assert gateway.requests == []
        return
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await service.execute(command, work)
    assert len(gateway.requests) == 1
    assert gateway.requests[0].context.remote_evidence.source_reference == source.payload["observation_digest"]
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.VALIDATING and run.remote_remediation_count == 1
    await commands.complete(command.id, worker_id=command.lease_owner)
    from test_base_update_workflow import _review_adopted_candidate

    await _review_adopted_candidate(
        case, git, factory, commands, expect_push=True, writes=writes, validator=validator,
        check_repair_lineage=False,
    )
