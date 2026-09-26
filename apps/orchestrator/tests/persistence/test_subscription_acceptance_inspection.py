"""Final acceptance inspects the current candidate without holding a DB transaction."""

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case


@pytest.mark.integration
async def test_prepared_acceptance_loads_current_review_and_worktree(session_factory, tmp_path):
    factory, primary, decision = await acceptance_case(session_factory, tmp_path)
    await SubscriptionDecisionApplication(factory).prepare_acceptance(primary.attempt.attempt_id)
    async with factory() as work:
        proposal = await work.subscription_decisions.acceptance_proposal(primary.attempt.attempt_id)
        run = await work.runs.get(primary.task.run_id)
        assert proposal.decision == decision and proposal.attempt_id == primary.attempt.attempt_id
        assert proposal.review.candidate_epoch == primary.candidate_epoch
        assert proposal.review.candidate.tree_digest == decision.candidate_tree_digest
        assert proposal.worktree.base_sha == run.base_sha
        assert proposal.run_version == run.version
        assert proposal.task_version == primary.task_version + 2
        assert proposal.inspection is None


@pytest.mark.integration
async def test_acceptance_observation_runs_outside_transaction_and_rechecks_git_on_retry(
    session_factory, tmp_path
):
    import asyncio

    from forge.application.ports.worktrees import GitWorkingTreeSnapshot
    from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    calls = []

    async def snapshot(proposal):
        async with factory() as work:
            await asyncio.wait_for(work.runs.get_for_update(primary.task.run_id), 5)
        calls.append(proposal)
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionAcceptanceInspection(factory, snapshot)
    observed = await service.inspect(primary.attempt.attempt_id)
    assert observed == await service.inspect(primary.attempt.attempt_id)
    assert len(calls) == 2
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.application_payload["observation"] == observed.payload()
        assert result.disposition == "acceptance_prepared"
        assert (await work.runs.get(primary.task.run_id)).state.value == "IMPLEMENTING"


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "epoch",
        "open",
        "draining",
        "admission",
        "task_version",
        "task_pause",
        "run_pause",
        "run_version",
        "cancel_command",
        "worktree",
        "base",
        "policy",
        "effect",
        "fence",
    ],
)
async def test_acceptance_revalidates_authority_after_git_io(session_factory, tmp_path, change):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from forge.domain.operation import canonical_digest
    from forge.persistence.models.execution import RunCommand
    from forge.persistence.models.run import Run
    from forge.persistence.models.scheduling import (
        SubscriptionScheduledEffect,
        SubscriptionSchedulerRun,
    )
    from forge.persistence.models.subscription import SubscriptionTask
    from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def snapshot(proposal):
        async with factory() as work:
            if change in {"epoch", "open", "draining", "admission"}:
                row = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
                if change == "epoch":
                    row.candidate_epoch += 1
                elif change == "admission":
                    row.admitted = False
                else:
                    row.candidate_state = change
            elif change in {"task_version", "task_pause"}:
                row = await work.session.get(SubscriptionTask, primary.task.task_id)
                if change == "task_version":
                    row.version += 1
                else:
                    row.pause_requested = True
            elif change in {"run_pause", "run_version", "worktree"}:
                if change == "run_pause":
                    run = await work.runs.get_for_update(primary.task.run_id)
                    await work.runs.pause(run.id, run.version, "run.paused", {})
                else:
                    row = await work.session.get(Run, primary.task.run_id)
                    if change == "run_version":
                        row.version += 1
                    else:
                        row.worktree_path += "-changed"
            elif change == "cancel_command":
                work.session.add(
                    RunCommand(
                        run_id=primary.task.run_id,
                        idempotency_key="cancel-during-acceptance-observation",
                        command_type="cancel",
                        expected_run_version=proposal.run_version,
                        actor_id=uuid4(),
                        payload={},
                    )
                )
            elif change == "policy":
                document = proposal.policy.model_dump(mode="json")
                document["version"] += 1
                await work.projects.append_policy(
                    project_id=proposal.policy.id,
                    expected_policy_version=proposal.policy.version,
                    policy_digest=canonical_digest(document),
                    policy_document=document,
                )
            elif change == "effect":
                work.session.add(
                    SubscriptionScheduledEffect(
                        id=uuid4(),
                        run_id=primary.task.run_id,
                        task_id=primary.task.task_id,
                        lease_owner=primary.lease.owner,
                        lease_generation=primary.lease.generation,
                        candidate_epoch=primary.candidate_epoch,
                    )
                )
            elif change == "fence":
                work.session.add(
                    SubscriptionHandoffFence(
                        worktree_id=proposal.worktree.identity.worktree_name,
                        run_id=primary.task.run_id,
                        attempt_id=primary.attempt.attempt_id,
                        token=uuid4(),
                        result_digest=proposal.result_digest,
                        expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    )
                )
            await work.commit()
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40,
            base_sha="c" * 40 if change == "base" else proposal.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionAcceptanceInspection(factory, snapshot).inspect(
            primary.attempt.attempt_id
        )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert (
            result.disposition == "acceptance_prepared"
            and "observation" not in result.application_payload
        )


@pytest.mark.integration
async def test_acceptance_git_failure_retries_without_losing_prepared_source(
    session_factory, tmp_path
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def unavailable(proposal):
        raise OSError("Git observation unavailable")

    with pytest.raises(OSError):
        await SubscriptionAcceptanceInspection(factory, unavailable).inspect(
            primary.attempt.attempt_id
        )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert "observation" not in result.application_payload

    async def recovered(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    assert (
        await SubscriptionAcceptanceInspection(factory, recovered).inspect(
            primary.attempt.attempt_id
        )
    ).head_sha == "b" * 40


@pytest.mark.integration
async def test_changed_git_retry_preserves_first_acceptance_observation(session_factory, tmp_path):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    head = "b" * 40

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha=head, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    service = SubscriptionAcceptanceInspection(factory, snapshot)
    observed = await service.inspect(primary.attempt.attempt_id)
    head = "c" * 40
    with pytest.raises(SubscriptionDecisionError, match="observation replay"):
        await service.inspect(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.application_payload["observation"] == observed.payload()


@pytest.mark.integration
@pytest.mark.parametrize("corruption", ["shape", "base", "digest"])
async def test_acceptance_replay_refuses_corrupt_observation(session_factory, tmp_path, corruption):
    from forge.domain.operation import canonical_digest

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)

    async def snapshot(proposal):
        return GitWorkingTreeSnapshot(
            head_sha="b" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
        )

    await SubscriptionAcceptanceInspection(factory, snapshot).inspect(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        payload = dict(result.application_payload)
        if corruption == "shape":
            payload["observation"] = {**payload["observation"], "unexpected": True}
        elif corruption == "base":
            payload["observation"] = {**payload["observation"], "base_sha": "c" * 40}
        result.application_payload = payload
        result.application_digest = (
            "f" * 64 if corruption == "digest" else canonical_digest(payload)
        )
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).prepare_acceptance(
            primary.attempt.attempt_id
        )
