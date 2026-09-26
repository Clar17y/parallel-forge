"""Task acceptance acknowledges retained worker evidence without accepting the run."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import AcceptDecision, TaskBudget
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_handoff_application import application_case
from test_subscription_usage import _known, _reservation


async def task_acceptance_case(session_factory, tmp_path, monkeypatch, *, mutate=None):
    factory, _, child, application, observation, proof = await application_case(
        session_factory,
        tmp_path,
        monkeypatch,
        primary_budget=TaskBudget(max_provider_attempts=8),
    )
    await application.apply_handoff(observation, proof)
    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("primary-accepts-task", _reservation())
    assert primary is not None
    handoff = observation.proposal.handoff
    decision = AcceptDecision(
        run_id=primary.task.run_id,
        task_id=child.task.task_id,
        candidate_commit=handoff.candidate_commit,
        candidate_tree_digest=handoff.candidate_tree_digest,
        evidence_receipt_ids=handoff.evidence_receipt_ids,
        rationale="Accept the completed bounded outcome",
    )
    if mutate is not None:
        decision = mutate(decision)
    launch = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt,
                decision=decision,
                telemetry=_known(),
                launch_proof=launch,
            ),
        )
    ).disposition == "decision_pending"
    return factory, primary, child, application


@pytest.mark.integration
async def test_primary_accepts_verified_child_without_final_candidate_approval(
    session_factory, tmp_path, monkeypatch
):
    factory, primary, child, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch
    )
    outcome = await application.prepare_acceptance(primary.attempt.attempt_id)
    assert outcome.accepted and outcome.disposition == "task_accepted"
    assert (await application.prepare_acceptance(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        worker = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        task = await work.session.get(SubscriptionTask, primary.task.task_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        run = await work.runs.get(primary.task.run_id)
        assert source.application_payload["kind"] == "task_acceptance"
        assert source.application_payload["handoff_attempt_id"] == str(child.attempt.attempt_id)
        assert source.application_payload["handoff_application_digest"] == worker.application_digest
        assert task.state == "queued" and scheduler.candidate_state == "open"
        assert run.state.value == "IMPLEMENTING" and run.pending_gate is None


@pytest.mark.integration
async def test_next_primary_invocation_retains_exact_task_acceptance(
    session_factory, tmp_path, monkeypatch
):
    from forge.agents.subscription_protocol import json_value
    from forge.application.services.subscription_requests import SubscriptionRequestBuilder
    from forge.domain.subscription import decode_subscription_record

    factory, primary, child, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch
    )
    await application.prepare_acceptance(primary.attempt.attempt_id)
    following = await SubscriptionDecisionExecutor(factory).admit_next(
        "primary-next", _reservation()
    )
    request = await SubscriptionRequestBuilder(factory).build(following)
    outcome = next(
        item
        for item in request.untrusted_context["task_outcomes"]
        if item["task_id"] == str(child.task.task_id)
    )
    assert outcome["acceptance_attempt_id"] == str(primary.attempt.attempt_id)
    assert outcome["accepted_handoff_attempt_id"] == str(child.attempt.attempt_id)
    assert (
        decode_subscription_record(json_value(outcome["acceptance"])).task_id == child.task.task_id
    )


@pytest.mark.integration
@pytest.mark.parametrize("change", ["tree", "commit", "receipt", "duplicate", "unknown"])
async def test_invalid_task_acceptance_is_bounded_rejection(
    session_factory, tmp_path, monkeypatch, change
):
    def mutate(decision):
        return replace(
            decision,
            **{
                "tree": {"candidate_tree_digest": "f" * 64},
                "commit": {"candidate_commit": "f" * 40},
                "receipt": {"evidence_receipt_ids": (str(uuid4()),)},
                "duplicate": {"evidence_receipt_ids": decision.evidence_receipt_ids * 2},
                "unknown": {"task_id": uuid4()},
            }[change],
        )

    factory, primary, _, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch, mutate=mutate
    )
    outcome = await application.prepare_acceptance(primary.attempt.attempt_id)
    assert not outcome.accepted and outcome.disposition in {
        "decision_repair_queued",
        "decision_rejected",
    }
    assert (await application.prepare_acceptance(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert source.application_payload is None


@pytest.mark.integration
@pytest.mark.parametrize("change", ["active", "cancelled", "lineage", "contract", "unverified"])
async def test_task_acceptance_requires_current_terminal_owned_child(
    session_factory, tmp_path, monkeypatch, change
):
    from forge.domain.subscription import encode_subscription_record
    from forge.persistence.models.scheduling import SubscriptionScheduledTask

    factory, primary, child, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch
    )
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        if change == "active":
            task.state = scheduled.state = "queued"
        elif change == "cancelled":
            task.cancel_requested = True
        elif change == "lineage":
            task.parent_task_id = scheduled.parent_task_id = None
        elif change == "unverified":
            source = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            source.disposition = "decision_pending"
        else:
            task.payload = encode_subscription_record(
                replace(child.task, owned_paths=("apps/new",))
            )
        await work.commit()
    if change == "contract":
        with pytest.raises(SubscriptionDecisionError):
            await application.prepare_acceptance(primary.attempt.attempt_id)
    else:
        assert not (await application.prepare_acceptance(primary.attempt.attempt_id)).accepted


@pytest.mark.integration
@pytest.mark.parametrize("change", ["receipt", "source", "removed", "pointer"])
async def test_task_acceptance_replay_refuses_changed_source_or_receipt(
    session_factory, tmp_path, monkeypatch, change
):
    from forge.domain.operation import canonical_digest
    from sqlalchemy import null

    factory, primary, child, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch
    )
    await application.prepare_acceptance(primary.attempt.attempt_id)
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        if change == "receipt":
            source.application_digest = "f" * 64
        elif change == "source":
            worker = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            worker.application_digest = "f" * 64
        elif change == "pointer":
            source.application_payload = {
                **source.application_payload,
                "handoff_attempt_id": str(uuid4()),
            }
            source.application_digest = canonical_digest(source.application_payload)
        else:
            source.application_payload, source.application_digest = null(), None
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.prepare_acceptance(primary.attempt.attempt_id)


@pytest.mark.integration
async def test_historical_task_acceptance_survives_new_candidate_and_child_contract(
    session_factory, tmp_path, monkeypatch
):
    from forge.domain.subscription import encode_subscription_record

    factory, primary, child, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch
    )
    await application.prepare_acceptance(primary.attempt.attempt_id)
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        scheduler.candidate_epoch += 2
        target = await work.session.get(SubscriptionTask, child.task.task_id)
        target.payload = encode_subscription_record(replace(child.task, owned_paths=("apps/new",)))
        target.state = "queued"
        await work.commit()
    assert (await application.prepare_acceptance(primary.attempt.attempt_id)).replayed


@pytest.mark.integration
async def test_concurrent_task_acceptance_applies_once(session_factory, tmp_path, monkeypatch):
    import asyncio

    _, primary, _, application = await task_acceptance_case(session_factory, tmp_path, monkeypatch)
    results = await asyncio.gather(
        *(application.prepare_acceptance(primary.attempt.attempt_id) for _ in range(2))
    )
    assert all(result.accepted and result.disposition == "task_accepted" for result in results)
    assert sorted(result.replayed for result in results) == [False, True]


@pytest.mark.integration
@pytest.mark.parametrize("target", ["child", "primary"])
async def test_pending_stop_prevents_fresh_acceptance(
    session_factory, tmp_path, monkeypatch, target
):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.persistence.models.execution import RunCommand
    from test_subscription_acceptance_preparation import acceptance_case

    if target == "child":
        factory, primary, _, application = await task_acceptance_case(
            session_factory, tmp_path, monkeypatch
        )
    else:
        factory, primary, _ = await acceptance_case(session_factory, tmp_path)
        application = SubscriptionDecisionApplication(factory)
    async with factory() as work:
        run = await work.runs.get(primary.task.run_id)
        work.session.add(
            RunCommand(
                run_id=run.id,
                idempotency_key="stop-before-accept",
                command_type="cancel",
                expected_run_version=run.version,
                actor_id=uuid4(),
                payload={},
            )
        )
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.prepare_acceptance(primary.attempt.attempt_id)
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert source.disposition == "decision_pending" and source.application_payload is None
