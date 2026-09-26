"""Candidate inspection performs Git IO outside the durable application transaction."""

import asyncio

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_review_selection import selection_case


@pytest.mark.integration
async def test_candidate_inspection_persists_real_identity_and_replays_without_io(
    session_factory, tmp_path
):
    from forge.application.services.subscription_candidate import SubscriptionCandidateInspection

    factory, admission, _ = await selection_case(session_factory, tmp_path)
    calls = []

    async def snapshot(proposal):
        # A second transaction can lock the run while Git observation is active.
        async with factory() as work:
            await asyncio.wait_for(work.runs.get_for_update(admission.task.run_id), 5)
            row = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
            assert row.candidate_state == "closed"
        calls.append(proposal)
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionCandidateInspection(factory, snapshot)
    observed = await service.inspect(admission.attempt.attempt_id)
    assert observed.head_sha == "b" * 40
    assert observed.tree_digest != "a" * 64  # Proposed identity is not trusted.
    assert await service.inspect(admission.attempt.attempt_id) == observed
    assert len(calls) == 1
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.disposition == "candidate_prepared"
        assert result.application_payload["observation"]["tree_digest"] == observed.tree_digest


@pytest.mark.integration
@pytest.mark.parametrize(
    "change", ["epoch", "phase", "version", "pause", "barrier", "base", "run_version"]
)
async def test_inspection_rechecks_current_source_after_git_io(session_factory, tmp_path, change):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.application.services.subscription_candidate import SubscriptionCandidateInspection
    from forge.persistence.models.run import Run
    from forge.persistence.models.subscription import SubscriptionTask

    factory, admission, _ = await selection_case(session_factory, tmp_path)

    async def snapshot(proposal):
        async with factory() as work:
            if change in {"epoch", "barrier"}:
                row = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
                if change == "epoch":
                    row.candidate_epoch += 1
                else:
                    row.candidate_state = "open"
            elif change == "run_version":
                (await work.session.get(Run, admission.task.run_id)).version += 1
            elif change == "phase":
                run = await work.runs.get_for_update(admission.task.run_id)
                await work.runs.pause(run.id, run.version, "run.paused", {})
            elif change != "base":
                row = await work.session.get(SubscriptionTask, admission.task.task_id)
                if change == "version":
                    row.version += 1
                else:
                    row.pause_requested = True
            await work.commit()
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40,
            base_sha="c" * 40 if change == "base" else proposal.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionCandidateInspection(factory, snapshot).inspect(
            admission.attempt.attempt_id
        )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert "observation" not in result.application_payload


@pytest.mark.integration
async def test_inspection_cancellation_leaves_recoverable_intent(session_factory, tmp_path):
    import asyncio

    from forge.application.services.subscription_candidate import SubscriptionCandidateInspection

    factory, admission, _ = await selection_case(session_factory, tmp_path)

    async def cancelled(proposal):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await SubscriptionCandidateInspection(factory, cancelled).inspect(
            admission.attempt.attempt_id
        )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.disposition == "candidate_prepared"
        assert "observation" not in result.application_payload

    async def recovered(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40,
            base_sha=proposal.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    result = await SubscriptionCandidateInspection(factory, recovered).inspect(
        admission.attempt.attempt_id
    )
    assert result.head_sha == "b" * 40


@pytest.mark.integration
async def test_inspection_receipt_rejects_invalid_shape_even_with_rehashed_payload(
    session_factory, tmp_path
):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.application.services.subscription_candidate import SubscriptionCandidateInspection
    from forge.domain.operation import canonical_digest

    factory, admission, _ = await selection_case(session_factory, tmp_path)

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40,
            base_sha=proposal.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    service = SubscriptionCandidateInspection(factory, snapshot)
    await service.inspect(admission.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        result.application_payload = {
            **result.application_payload,
            "observation": {"tree_digest": "a" * 64},
        }
        result.application_digest = canonical_digest(result.application_payload)
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await service.inspect(admission.attempt.attempt_id)


@pytest.mark.integration
async def test_proposal_loading_does_not_prepare_a_pending_selection(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError

    factory, admission, _ = await selection_case(session_factory, tmp_path)
    async with factory() as work:
        with pytest.raises(SubscriptionDecisionError, match="not prepared"):
            await work.subscription_decisions.review_selection_proposal(
                admission.attempt.attempt_id
            )
        await work.commit()
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.disposition == "decision_pending"
        row = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        assert row.candidate_state == "open"
