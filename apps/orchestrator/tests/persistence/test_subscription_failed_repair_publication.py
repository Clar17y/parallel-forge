"""A separately acknowledged failed repair can publish only fresh accepted evidence."""

from uuid import UUID

import pytest
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.domain.approval import canonical_digest
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.repositories.operations import PostgresOperationRepository
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_base_publication import adopted_acceptance_case
from test_subscription_remote_publication import remote_acceptance_case


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["remote", "base", "resumed_remote"])
async def test_failed_repair_returns_through_fresh_acceptance_to_reviewed_push(
    session_factory, tmp_path, kind
):
    case = (
        await adopted_acceptance_case(session_factory, tmp_path, fail_after_repair=True)
        if kind == "base"
        else await remote_acceptance_case(
            session_factory,
            tmp_path,
            fail_after_repair=True,
            resume_before_repair=kind == "resumed_remote",
        )
    )
    source = case.command if kind == "base" else case.repair_command
    async with case.factory() as work:
        failed = await work.commands.get(source.id)
        assert failed.status is CommandStatus.FAILED
        result = await case.controller.validate(case.validation_command, work)
        assert result.state is RunState.MONITORING_PR
    await case.commands.complete(
        case.validation_command.id, worker_id=case.validation_command.lease_owner
    )
    push = await case.commands.claim_next(worker_id="acknowledged-repair-push", lease_seconds=120)
    assert push is not None and push.command_type == "push_reviewed_pr"
    pushes = []

    class Push:
        async def push(self, tree, policy, head):
            pushes.append(head)

    release = ReleaseService(
        case.validator,
        case.writes,
        lambda _: Push(),
        OperationExecutor(PostgresOperationRepository(session_factory)),
    )
    for _ in range(2):
        async with case.factory() as work:
            await release.push_reviewed(push, work)
    async with case.factory() as work:
        assert await work.commands.get(source.id) == failed
        record = await work.releases.get_for_run(push.run_id)
        original = await work.operations.get(record.publication_intent_id)
        verified = await case.validator.validate_published(
            work, push.run_id, UUID(original.request_payload["approval_id"])
        )
        assert verified.evidence.base_sha == case.original.worktree.base_sha
        assert canonical_digest(verified.evidence) == result.pr_evidence_digest
        assert not record.pull_request.merged
    assert len(pushes) == len(case.writes.pull_requests) == 1
