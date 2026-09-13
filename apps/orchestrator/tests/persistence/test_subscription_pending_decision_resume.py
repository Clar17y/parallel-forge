"""Operator resume preserves a stopped decision through retained application phases."""

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_candidate import (
    SubscriptionCandidateApplication,
    SubscriptionCandidateInspection,
)
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_resume_controls import pause_and_resume
from test_subscription_review_selection import selection_case


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["pending", "prepared", "observed"])
async def test_stopped_decision_resumes_without_another_provider_turn_or_repair(
    session_factory, tmp_path, phase
):
    snapshot = GitWorkingTreeSnapshot(
        head_sha="b" * 40,
        base_sha="a" * 40,
        files=(),
        changed_paths=(),
    )
    factory, admission, _ = await selection_case(
        session_factory,
        tmp_path,
        tree_digest=snapshot.candidate_tree_digest,
        candidate_commit=snapshot.head_sha,
    )
    commands = PostgresCommandRepository(session_factory)
    async with factory() as work:
        outstanding = await work.commands.list_outstanding_normal(
            run_id=admission.task.run_id, exclude_command_id=None
        )
    assert len(outstanding) == 1 and outstanding[0].command_type == "prepare_worktree"
    await commands.complete(outstanding[0].id, worker_id=outstanding[0].lease_owner)

    async def capture(proposal):
        return snapshot

    if phase == "prepared":
        await SubscriptionDecisionApplication(factory).prepare_review_selection(
            admission.attempt.attempt_id
        )
    elif phase == "observed":
        await SubscriptionCandidateInspection(factory, capture).inspect(
            admission.attempt.attempt_id
        )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        original = canonical_digest(result.result_payload), result.result_digest
        before = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionAttempt)
            .where(
                SubscriptionAttempt.run_id == admission.task.run_id,
            )
        )
    continued = await pause_and_resume(
        factory,
        session_factory,
        admission.task.run_id,
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    assert continued is None
    application = SubscriptionCandidateApplication(factory, capture)
    result = await application.apply(admission.attempt.attempt_id)
    assert result.accepted
    assert (await application.apply(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        assert (await work.runs.get(admission.task.run_id)).state is RunState.IMPLEMENTING
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert (canonical_digest(result.result_payload), result.result_digest) == original
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert scheduled.state == "queued" and scheduled.repairs == 0
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(SubscriptionAttempt)
                .where(
                    SubscriptionAttempt.run_id == admission.task.run_id,
                )
            )
            == before
        )
