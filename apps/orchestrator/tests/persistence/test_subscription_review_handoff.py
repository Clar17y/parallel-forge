"""Selected reviewers return evidence while the exact candidate remains closed."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import HandoffStatus, TaskHandoff
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_candidate_reads import reader_case
from test_subscription_usage import _known


async def review_handoff_case(session_factory, tmp_path, *, mutate=None, primary_budget=None):
    factory, child = await reader_case(session_factory, tmp_path, primary_budget=primary_budget)
    async with factory() as work:
        selection = await work.subscription_decisions.review_selection_context(
            child.task.run_id, child.task.task_id
        )
    observation = selection["observation"]
    handoff = TaskHandoff(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id,
        status=HandoffStatus.COMPLETED,
        candidate_commit=observation["head_sha"],
        candidate_tree_digest=observation["tree_digest"],
        evidence_receipt_ids=(str(uuid4()),),
        summary="Candidate review is complete",
    )
    if mutate is not None:
        handoff = mutate(handoff)
    proof = await record_stopped_launch(session_factory, child)
    result = SubscriptionInvocationResult(
        attempt=child.attempt, decision=handoff, telemetry=_known(), launch_proof=proof
    )
    assert (
        await SubscriptionDecisionExecutor(factory).settle(child, result)
    ).disposition == "decision_pending"
    return factory, child, handoff


@pytest.mark.integration
async def test_selected_reviewer_handoff_loads_under_closed_candidate(session_factory, tmp_path):
    factory, child, handoff = await review_handoff_case(session_factory, tmp_path)
    proposal = await SubscriptionDecisionApplication(factory).handoff_proposal(
        child.attempt.attempt_id
    )
    assert proposal.task == child.task and proposal.handoff == handoff
    assert proposal.candidate_epoch == child.candidate_epoch
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, child.task.run_id)
        assert scheduler.candidate_state == "closed"


@pytest.mark.integration
@pytest.mark.parametrize("current_matches", [False, True])
@pytest.mark.parametrize("with_report", [None, "approve", "request_changes", "blocked"])
async def test_review_completion_wakes_primary_and_replays_after_reopen(
    session_factory, tmp_path, monkeypatch, current_matches, with_report
):
    from forge.application.ports.subscription_handoff import (
        HandoffCallProof,
        VerifiedSubscriptionHandoff,
    )
    from forge.domain.agent import ReviewDecision
    from forge.domain.operation import canonical_digest
    from forge.domain.subscription import encode_subscription_record
    from forge.persistence.models.subscription import SubscriptionTask
    from forge.persistence.repositories.subscription_handoff_evidence import (
        PostgresSubscriptionHandoffEvidence,
    )
    from test_subscription_review_report import reviewed

    factory, child, handoff = await review_handoff_case(
        session_factory,
        tmp_path,
        mutate=(lambda value: reviewed(value, ReviewDecision(with_report)))
        if with_report
        else None,
    )
    application = SubscriptionDecisionApplication(factory)
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    identity = UUID(handoff.evidence_receipt_ids[0])
    proof = VerifiedSubscriptionHandoff(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id,
        snapshot_call_id=identity,
        manifest_digest="d" * 64,
        candidate_tree_digest=handoff.candidate_tree_digest,
        policy_version=child.envelope.safety_policy_version,
        task_digest=canonical_digest(encode_subscription_record(child.task)),
        handoff_digest=canonical_digest(encode_subscription_record(handoff)),
        call_proofs=(HandoffCallProof(identity, "e" * 64, "f" * 64),),
        artifact_proofs=(("d" * 64, "1" * 64),),
        checks_match_snapshot=True,
        output_digest="2" * 64,
        current_tree_digest=handoff.candidate_tree_digest if current_matches else "9" * 64,
    )

    async def verified(self, supplied):
        return supplied == proof

    monkeypatch.setattr(PostgresSubscriptionHandoffEvidence, "verify", verified)
    if not current_matches:
        from forge.application.ports.subscription_decisions import SubscriptionDecisionError

        with pytest.raises(SubscriptionDecisionError, match="evidence or observation"):
            await application.apply_handoff(observation, proof)
        return
    result = await application.apply_handoff(observation, proof)
    assert result.accepted and result.disposition == "handoff_completed"
    assert (await application.handoff_replay(child.attempt.attempt_id)).replayed
    async with factory() as work:
        parent = await work.session.get(SubscriptionTask, child.task.parent_task_id)
        assert parent.state == "queued"
        outcomes = await work.subscription.invocation_outcomes(
            child.task.run_id, (child.task.task_id,)
        )
        assert outcomes[0].recorded_handoff == handoff
        if with_report:
            evidence = await work.subscription_decisions.candidate_review_evidence(
                child.task.run_id, child.task.parent_task_id
            )
            assert evidence.review_handoff == handoff
            assert evidence.review_attempt_id == child.attempt.attempt_id
            assert evidence.candidate.tree_digest == handoff.candidate_tree_digest
            from forge.application.ports.subscription_acceptance import acceptance_intent_payload
            from forge.domain.subscription import AcceptDecision

            intent = AcceptDecision(
                run_id=child.task.run_id,
                task_id=child.task.parent_task_id,
                candidate_commit=handoff.candidate_commit,
                candidate_tree_digest=handoff.candidate_tree_digest,
                evidence_receipt_ids=handoff.evidence_receipt_ids,
                rationale="Proposed acceptance",
            )
            assert (acceptance_intent_payload(intent, evidence, "a" * 64) is not None) is (
                with_report == "approve"
            )
            from forge.application.ports.subscription_decisions import SubscriptionDecisionError
            from forge.persistence.models.subscription_results import SubscriptionAttemptResult

            review_row = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            retained_digest = review_row.application_digest
            review_row.application_digest = "f" * 64
            await work.session.flush()
            with pytest.raises(SubscriptionDecisionError):
                await work.subscription_decisions.candidate_review_evidence(
                    child.task.run_id, child.task.parent_task_id
                )
            review_row.application_digest = retained_digest
            await work.session.flush()
        else:
            from forge.application.ports.subscription_decisions import SubscriptionDecisionError

            with pytest.raises(SubscriptionDecisionError, match="report binding"):
                await work.subscription_decisions.candidate_review_evidence(
                    child.task.run_id, child.task.parent_task_id
                )
        scheduler = await work.session.get(SubscriptionSchedulerRun, child.task.run_id)
        assert scheduler.candidate_state == "closed"
        scheduler.candidate_state = "open"
        scheduler.candidate_epoch += 1
        await work.commit()
    assert (await application.handoff_replay(child.attempt.attempt_id)).replayed


@pytest.mark.integration
@pytest.mark.parametrize("claim", ["tree", "commit"])
async def test_false_review_candidate_claim_is_bounded_without_git(
    session_factory, tmp_path, claim
):
    from forge.application.services.subscription_handoff_application import (
        SubscriptionHandoffApplication,
    )
    from forge.persistence.models.subscription import SubscriptionTask

    factory, child, _ = await review_handoff_case(
        session_factory,
        tmp_path,
        mutate=lambda handoff: replace(
            handoff,
            **{
                "candidate_tree_digest" if claim == "tree" else "candidate_commit": "c"
                * (64 if claim == "tree" else 40)
            },
        ),
    )

    async def no_snapshot(proposal):
        raise AssertionError("intrinsically false claim must not perform Git IO")

    application = SubscriptionHandoffApplication(factory, None, no_snapshot)
    result = await application.apply(child.attempt.attempt_id)
    assert not result.accepted and result.disposition == "handoff_rejected"
    assert (await application.apply(child.attempt.attempt_id)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        parent = await work.session.get(SubscriptionTask, child.task.parent_task_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, child.task.run_id)
        assert task.state == "terminal" and parent.state == "queued"
        assert scheduler.candidate_state == "closed"
