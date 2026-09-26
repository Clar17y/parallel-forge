"""Primary acceptance intent retains its exact source before external verification."""

from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import AcceptDecision, TaskBudget
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_candidate_reads import reader_case
from test_subscription_usage import _known


async def acceptance_case(session_factory, tmp_path, *, mutate=None, plan_scope=None):
    factory, primary = await reader_case(
        session_factory,
        tmp_path,
        review_required=False,
        primary_budget=TaskBudget(max_provider_attempts=8),
        plan_scope=plan_scope,
    )
    async with factory() as work:
        review = await work.subscription_decisions.candidate_review_evidence(
            primary.task.run_id, primary.task.task_id
        )
    decision = AcceptDecision(
        run_id=primary.task.run_id,
        task_id=primary.task.task_id,
        candidate_commit=review.candidate.head_sha,
        candidate_tree_digest=review.candidate.tree_digest,
        evidence_receipt_ids=(str(uuid4()),),
        rationale="Propose acceptance of the selected candidate",
    )
    if mutate is not None:
        decision = mutate(decision)
    proof = await record_stopped_launch(session_factory, primary)
    result = SubscriptionInvocationResult(
        attempt=primary.attempt, decision=decision, telemetry=_known(), launch_proof=proof
    )
    assert (
        await SubscriptionDecisionExecutor(factory).settle(primary, result)
    ).disposition == "decision_pending"
    return factory, primary, decision


@pytest.mark.integration
async def test_acceptance_preparation_blocks_primary_without_final_approval(
    session_factory, tmp_path
):
    factory, primary, _decision = await acceptance_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    result = await service.prepare_acceptance(primary.attempt.attempt_id)
    assert result.accepted and result.disposition == "acceptance_prepared"
    assert (await service.prepare_acceptance(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        task = await work.session.get(SubscriptionTask, primary.task.task_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        run = await work.runs.get(primary.task.run_id)
        assert source.application_payload["kind"] == "acceptance_intent"
        assert source.application_payload["candidate_epoch"] == primary.candidate_epoch
        assert task.state == "blocked" and scheduler.candidate_state == "closed"
        assert run.state.value == "IMPLEMENTING" and run.pending_gate is None


@pytest.mark.integration
@pytest.mark.parametrize("change", ["tree", "commit", "receipt", "nil_receipt"])
async def test_invalid_acceptance_claim_is_rejected_without_opening_candidate(
    session_factory, tmp_path, change
):
    from dataclasses import replace

    values = {
        "tree": {"candidate_tree_digest": "f" * 64},
        "commit": {"candidate_commit": "f" * 40},
        "receipt": {"evidence_receipt_ids": ("not-a-receipt",)},
        "nil_receipt": {"evidence_receipt_ids": ("00000000-0000-0000-0000-000000000000",)},
    }
    factory, primary, _ = await acceptance_case(
        session_factory, tmp_path, mutate=lambda decision: replace(decision, **values[change])
    )
    service = SubscriptionDecisionApplication(factory)
    result = await service.prepare_acceptance(primary.attempt.attempt_id)
    assert not result.accepted and result.disposition in {
        "decision_repair_queued",
        "decision_rejected",
    }
    assert (await service.prepare_acceptance(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert source.application_payload is None
        assert (
            scheduler.candidate_state == "closed"
            and scheduler.candidate_epoch == primary.candidate_epoch
        )


@pytest.mark.integration
async def test_acceptance_intent_replay_survives_reopen_but_refuses_corruption(
    session_factory, tmp_path
):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    await service.prepare_acceptance(primary.attempt.attempt_id)
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        scheduler.candidate_state = "open"
        scheduler.candidate_epoch += 1
        await work.commit()
    assert (await service.prepare_acceptance(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        source.application_digest = "f" * 64
        await work.commit()
    with pytest.raises(SubscriptionDecisionError, match="acceptance intent replay"):
        await service.prepare_acceptance(primary.attempt.attempt_id)


@pytest.mark.integration
@pytest.mark.parametrize("change", ["epoch", "draining", "cancel", "unadmitted"])
async def test_acceptance_intent_requires_current_authority(session_factory, tmp_path, change):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    async with factory() as work:
        if change == "cancel":
            (await work.session.get(SubscriptionTask, primary.task.task_id)).cancel_requested = True
        else:
            scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
            if change == "epoch":
                scheduler.candidate_epoch += 1
            elif change == "unadmitted":
                scheduler.admitted = False
            else:
                scheduler.candidate_state = "draining"
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).prepare_acceptance(
            primary.attempt.attempt_id
        )


@pytest.mark.integration
async def test_pending_acceptance_never_overwrites_existing_application_receipt(
    session_factory, tmp_path
):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.domain.operation import canonical_digest

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    retained = {"kind": "unexpected_existing_receipt"}
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        source.application_payload = retained
        source.application_digest = canonical_digest(retained)
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).prepare_acceptance(
            primary.attempt.attempt_id
        )
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert source.application_payload == retained and source.disposition == "decision_pending"
