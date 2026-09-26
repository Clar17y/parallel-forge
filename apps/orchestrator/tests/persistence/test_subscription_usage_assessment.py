"""Operator assessment distinguishes measurement, application and task outcomes."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_usage import SubscriptionUsagePage
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AttemptIdentity,
    AttemptTelemetry,
    LogicalTaskContract,
    RouteBinding,
    SpecialistPurpose,
    TaskBudget,
    encode_subscription_record,
)
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.queries.subscription_usage import SubscriptionUsageQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _remove_disposable_subscription_rows,
    _route,
)


async def _mixed_attempts(session_factory, run, *, primary=None, worker=None):
    async with PostgresUnitOfWork(session_factory) as work:
        parent = await _admit_run(work, run, (_route("primary"), _route("worker")))
        contract = LogicalTaskContract(
            run_id=run.id,
            task_id=uuid4(),
            parent_task_id=parent,
            purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
            route=RouteBinding(requested=_route("worker"), effective=_route("worker")),
            budget=TaskBudget(),
        )
        await work.subscription.create_task(contract, idempotency_key=str(contract.task_id))
        envelope = await work.subscription.envelope_for_run(run.id)
        for task_id, telemetry in ((parent, primary), (contract.task_id, worker)):
            task = await work.subscription.get_task(run.id, task_id)
            attempt_id = uuid4()
            await work.subscription.create_attempt(
                AttemptIdentity(
                    run_id=run.id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    attempt_number=1,
                ),
                route_payload=task.route,
                idempotency_key=str(attempt_id),
            )
            row = await work.session.get(SubscriptionAttempt, attempt_id)
            row.lease_owner = "assessment-fixture"
            row.lease_generation = 1
            row.task_version = 1
            row.candidate_epoch = 0
            row.task_digest = canonical_digest(encode_subscription_record(task))
            row.envelope_digest = canonical_digest(encode_subscription_record(envelope))
            row.telemetry_payload = (
                None if telemetry is None else encode_subscription_record(telemetry)
            )
        await work.commit()
    return parent, contract.task_id


@pytest.mark.integration
async def test_unmeasured_primary_has_unknown_share_with_measured_worker_across_pages(
    session_factory,
    persisted_run,
):
    await _mixed_attempts(session_factory, persisted_run, worker=AttemptTelemetry(input_tokens=10))
    query = SubscriptionUsageQuery(session_factory)
    first = SubscriptionUsagePage.model_validate(
        await query.usage(
            run_id=persisted_run.id,
            limit=1,
            include_assessment=True,
        )
    )
    second = SubscriptionUsagePage.model_validate(
        await query.usage(
            run_id=persisted_run.id,
            offset=1,
            limit=1,
            include_assessment=True,
        )
    )
    assert first.has_more and not second.has_more
    share = first.assessment.shares.input_tokens
    assert share == second.assessment.shares.input_tokens
    assert (share.numerator, share.denominator, share.share) == (None, 10, None)
    assert (share.primary_attempts, share.all_attempts) == (1, 2)
    assert (share.numerator_measured_attempts, share.numerator_unknown_attempts) == (0, 1)
    assert (share.denominator_measured_attempts, share.denominator_unknown_attempts) == (1, 1)
    assert share.coverage == 0.5 and first.assessment.primary_turns == 1


@pytest.mark.integration
async def test_applied_delegation_is_one_unfinished_interval_after_replay(
    session_factory, tmp_path
):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from test_subscription_delegation_application import delegation_case

    factory, primary, _, _ = await delegation_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    assert (await application.apply_delegation(primary.attempt.attempt_id)).accepted
    assert (await application.apply_delegation(primary.attempt.attempt_id)).replayed
    page = SubscriptionUsagePage.model_validate(
        await SubscriptionUsageQuery(session_factory).usage(
            run_id=primary.task.run_id,
            include_assessment=True,
        )
    )
    assessment = page.assessment
    assert (
        assessment.delegation_decisions,
        assessment.wait_decisions,
        assessment.unverified_decisions,
    ) == (1, 0, 0)
    assert assessment.waits.model_dump() == {
        "decisions": 1,
        "continued": 0,
        "unfinished": 1,
        "ended_without_continuation": 0,
        "measured_intervals": 0,
        "unknown_intervals": 1,
        "elapsed_ms": None,
    }
    assert sum(item.applied_decisions for item in assessment.outcomes) == 1


async def _assessment(session_factory, run_id):
    page = SubscriptionUsagePage.model_validate(
        await SubscriptionUsageQuery(session_factory).usage(
            run_id=run_id,
            include_assessment=True,
        )
    )
    assert page.assessment is not None
    return page.assessment


@pytest.mark.integration
@pytest.mark.parametrize("offset_seconds", [5, -1])
async def test_wait_uses_first_causal_primary_admission_and_not_mutable_update_time(
    session_factory,
    tmp_path,
    offset_seconds,
):
    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInvocationResult,
    )
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import WaitDecision
    from forge.persistence.models.subscription import SubscriptionDecisionRecord
    from sqlalchemy import select
    from subscription_launch_fixture import record_stopped_launch
    from test_subscription_delegation_application import delegation_case
    from test_subscription_usage import _known, _reservation

    factory, first, _, _ = await delegation_case(
        session_factory,
        tmp_path,
        primary_budget=TaskBudget(max_provider_attempts=12),
    )
    application = SubscriptionDecisionApplication(factory)
    await application.apply_delegation(first.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("child-finishes", _reservation())
    await executor.settle(
        child,
        SubscriptionInvocationResult(
            attempt=child.attempt,
            failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
        ),
    )
    second = await executor.admit_next("primary-waits", _reservation())
    assert second.task.task_id == first.task.task_id
    decision = WaitDecision(
        run_id=first.task.run_id,
        task_id=first.task.task_id,
        waiting_on_task_ids=(child.task.task_id,),
        reason="Observe terminal child",
    )
    await executor.settle(
        second,
        SubscriptionInvocationResult(
            attempt=second.attempt,
            decision=decision,
            telemetry=_known(),
            launch_proof=await record_stopped_launch(session_factory, second),
        ),
    )
    assert (await application.apply_wait(second.attempt.attempt_id)).accepted
    third = await executor.admit_next("primary-continues", _reservation())
    assert third.task.task_id == first.task.task_id
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with factory() as work:
        for admission, at in (
            (first, start),
            (second, start + timedelta(seconds=10 + offset_seconds)),
            (third, start + timedelta(seconds=30)),
        ):
            attempt = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
            attempt.created_at = at
        records = (
            await work.session.scalars(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.run_id == first.task.run_id,
                    SubscriptionDecisionRecord.record_type.in_(
                        ["DelegateDecision", "WaitDecision"]
                    ),
                )
            )
        ).all()
        for record in records:
            record.created_at = start + timedelta(
                seconds=10 if record.record_type == "DelegateDecision" else 20
            )
            record.updated_at = start + timedelta(days=900)
        await work.commit()
    value = await _assessment(session_factory, first.task.run_id)
    assert (value.delegation_decisions, value.wait_decisions) == (1, 1)
    assert value.waits.continued == 2 and value.waits.unfinished == 0
    assert value.waits.measured_intervals == (2 if offset_seconds == 5 else 1)
    assert value.waits.unknown_intervals == (0 if offset_seconds == 5 else 1)
    assert value.waits.elapsed_ms == (15_000 if offset_seconds == 5 else 10_000)
    primary = next(item for item in value.outcomes if item.purpose == "primary")
    # Includes the earlier plan proposal created by the production preparation fixture.
    assert (primary.attempts, primary.distinct_tasks, primary.pending_results) == (4, 1, 1)


@pytest.mark.integration
@pytest.mark.parametrize("state,ended", [("PAUSED", False), ("CANCELLED", True)])
async def test_applied_wait_without_continuation_distinguishes_ended_run(
    session_factory, tmp_path, state, ended
):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.persistence.models.run import Run
    from test_subscription_delegation_application import delegation_case

    factory, primary, _, _ = await delegation_case(session_factory, tmp_path)
    await SubscriptionDecisionApplication(factory).apply_delegation(primary.attempt.attempt_id)
    async with factory() as work:
        run = await work.session.get(Run, primary.task.run_id)
        run.state = state
        if state == "PAUSED":
            run.suspended_state = "IMPLEMENTING"
            run.suspension_kind = "PAUSE"
            run.suspension_context_schema_version = 1
            run.suspension_context = {
                "state": "IMPLEMENTING",
                "suspended_state": None,
                "suspension_kind": None,
            }
        await work.commit()
    value = await _assessment(session_factory, primary.task.run_id)
    assert value.waits.ended_without_continuation == int(ended)
    assert value.waits.unfinished == int(not ended)
    assert value.waits.elapsed_ms is None and value.waits.unknown_intervals == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation", ["source_digest", "record", "launch", "application", "rejected"]
)
async def test_accepted_flag_does_not_prove_applied_decision(session_factory, tmp_path, mutation):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.persistence.models.subscription import (
        SubscriptionClientLaunch,
        SubscriptionDecisionRecord,
    )
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from sqlalchemy import select
    from test_subscription_delegation_application import delegation_case

    factory, primary, _, _ = await delegation_case(session_factory, tmp_path)
    await SubscriptionDecisionApplication(factory).apply_delegation(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        if mutation == "source_digest":
            result.result_digest = "0" * 64
        elif mutation == "application":
            result.application_payload = {"kind": "invented", "result_digest": result.result_digest}
            result.application_digest = "0" * 64
        elif mutation == "record":
            record = await work.session.scalar(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.attempt_id == primary.attempt.attempt_id,
                    SubscriptionDecisionRecord.record_type == "DelegateDecision",
                )
            )
            record.payload = {}
        elif mutation == "launch":
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == primary.attempt.attempt_id,
                )
            )
            launch.state = "uncertain"
        else:
            result.accepted = False
            result.disposition = "decision_rejected"
        await work.commit()
    value = await _assessment(session_factory, primary.task.run_id)
    assert value.delegation_decisions == value.waits.decisions == 0
    assert value.unverified_decisions == int(mutation != "rejected")
    assert sum(item.applied_decisions for item in value.outcomes) == 0


@pytest.mark.integration
async def test_task_acceptance_is_attributed_once_to_proved_worker_handoff(
    session_factory, tmp_path, monkeypatch
):
    from test_subscription_task_acceptance import task_acceptance_case

    _, primary, child, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch
    )
    assert (await application.prepare_acceptance(primary.attempt.attempt_id)).accepted
    assert (await application.prepare_acceptance(primary.attempt.attempt_id)).replayed
    value = await _assessment(session_factory, primary.task.run_id)
    worker = next(item for item in value.outcomes if item.purpose == child.task.purpose.value)
    assert (
        worker.distinct_tasks,
        worker.terminal_tasks,
        worker.completed_handoffs,
        worker.task_acceptances,
    ) == (1, 1, 1, 1)
    assert sum(item.task_acceptances for item in value.outcomes) == 1


@pytest.mark.integration
async def test_created_identity_without_admission_does_not_count_as_a_primary_turn(
    session_factory, persisted_run
):
    from sqlalchemy import select

    primary, _ = await _mixed_attempts(session_factory, persisted_run)
    async with session_factory() as session:
        attempt = await session.scalar(
            select(SubscriptionAttempt).where(SubscriptionAttempt.task_row_id == primary)
        )
        attempt.lease_owner = None
        attempt.lease_generation = None
        attempt.task_version = None
        attempt.candidate_epoch = None
        attempt.task_digest = None
        attempt.envelope_digest = None
        await session.commit()
    value = await _assessment(session_factory, persisted_run.id)
    assert value.primary_turns == 0 and value.all_attempts == 1
    assert value.shares.input_tokens.numerator is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "primary,worker,numerator,denominator,share,coverage",
    [
        (None, None, None, None, None, 0.0),
        (0, 0, 0, 0, None, 1.0),
        (0, 10, 0, 10, 0.0, 1.0),
        (5, None, 5, 5, 1.0, 0.5),
        (5, 15, 5, 20, 0.25, 1.0),
    ],
)
async def test_measured_share_preserves_zero_partial_and_missing_observations(
    session_factory,
    persisted_run,
    primary,
    worker,
    numerator,
    denominator,
    share,
    coverage,
):
    await _mixed_attempts(
        session_factory,
        persisted_run,
        primary=AttemptTelemetry(input_tokens=primary),
        worker=AttemptTelemetry(input_tokens=worker),
    )
    value = await _assessment(session_factory, persisted_run.id)
    metric = value.shares.input_tokens
    assert (metric.numerator, metric.denominator, metric.share, metric.coverage) == (
        numerator,
        denominator,
        share,
        coverage,
    )


@pytest.mark.integration
async def test_no_attempts_has_unknown_coverage_and_no_wait_duration(
    session_factory, persisted_run
):
    value = await _assessment(session_factory, persisted_run.id)
    assert value.primary_turns == value.all_attempts == 0
    assert value.shares.input_tokens.coverage is value.shares.input_tokens.share is None
    assert value.waits.elapsed_ms is None and value.outcomes == []


@pytest.mark.integration
@pytest.mark.parametrize("quota", [False, True])
async def test_repair_debits_come_from_ledger_and_quota_deferral_spends_none(
    session_factory, persisted_run, quota
):
    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInvocationResult,
    )
    from forge.domain.provider_quota import QuotaExhaustion
    from subscription_launch_fixture import record_stopped_launch
    from test_subscription_execution_constraints import _admitted
    from test_subscription_usage import _known, _reservation

    executor, admission = await _admitted(session_factory, persisted_run)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        telemetry=_known(),
        failure=SubscriptionFailure.QUOTA if quota else SubscriptionFailure.PROTOCOL,
        quota_exhaustion=QuotaExhaustion(datetime.now(UTC), "provider_usage_exhausted")
        if quota
        else None,
        launch_proof=await record_stopped_launch(session_factory, admission),
    )
    settled = await executor.settle(admission, result)
    assert (await executor.settle(admission, result)).replayed
    value = await _assessment(session_factory, persisted_run.id)
    assert value.repair_debits == int(not quota)
    assert value.all_attempts == 1
    assert value.outcomes[0].latest_result_disposition == settled.disposition
    if quota:
        assert settled.disposition == "quota_deferred"
        assert await executor.admit_next("after-restart", _reservation()) is None
        assert await _assessment(session_factory, persisted_run.id) == value


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    ["missing_handoff", "handoff_digest", "handoff_application", "candidate_claim", "handoff_time"],
)
async def test_task_acceptance_requires_cross_source_lineage_even_with_a_matching_receipt_digest(
    session_factory,
    tmp_path,
    monkeypatch,
    mutation,
):
    from copy import deepcopy

    from forge.domain.subscription import decode_subscription_record
    from forge.persistence.models.subscription import SubscriptionDecisionRecord
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from sqlalchemy import select
    from test_subscription_task_acceptance import task_acceptance_case

    factory, primary, child, application = await task_acceptance_case(
        session_factory, tmp_path, monkeypatch
    )
    await application.prepare_acceptance(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        receipt = dict(result.application_payload)
        if mutation == "missing_handoff":
            receipt["handoff_attempt_id"] = str(uuid4())
        elif mutation == "handoff_digest":
            receipt["handoff_result_digest"] = "0" * 64
        elif mutation == "handoff_application":
            receipt["handoff_application_digest"] = "0" * 64
        elif mutation == "handoff_time":
            primary_record = await work.session.scalar(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.attempt_id == primary.attempt.attempt_id,
                    SubscriptionDecisionRecord.record_type == "AcceptDecision",
                )
            )
            child_record = await work.session.scalar(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.attempt_id == child.attempt.attempt_id,
                    SubscriptionDecisionRecord.record_type == "TaskHandoff",
                )
            )
            child_record.created_at = primary_record.created_at + timedelta(seconds=1)
        else:
            from dataclasses import replace

            payload = deepcopy(result.result_payload)
            decision = replace(
                decode_subscription_record(payload["decision"]), candidate_tree_digest="0" * 64
            )
            payload["decision"] = encode_subscription_record(decision)
            result.result_payload = payload
            result.result_digest = canonical_digest(payload)
            receipt["result_digest"] = result.result_digest
            record = await work.session.scalar(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.attempt_id == primary.attempt.attempt_id,
                    SubscriptionDecisionRecord.record_type == "AcceptDecision",
                )
            )
            record.payload = encode_subscription_record(decision)
        result.application_payload = receipt
        result.application_digest = canonical_digest(receipt)
        await work.commit()
    value = await _assessment(session_factory, primary.task.run_id)
    assert value.unverified_decisions == 1
    assert sum(item.task_acceptances for item in value.outcomes) == 0
    worker = next(item for item in value.outcomes if item.purpose == child.task.purpose.value)
    assert worker.completed_handoffs == 1


@pytest.mark.integration
async def test_scope_application_counts_survive_historical_child_contract_changes(
    session_factory, tmp_path
):
    from test_subscription_scope_response import response_case

    _, application, primary, _ = await response_case(session_factory, tmp_path)
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).accepted
    value = await _assessment(session_factory, primary.task.run_id)
    assert value.unverified_decisions == 0
    assert sum(item.applied_decisions for item in value.outcomes) == 3


@pytest.mark.integration
@pytest.mark.parametrize(
    "phase,applications", [("prepared_selection", 1), ("reopened", 2), ("acceptance_intent", 2)]
)
async def test_candidate_applications_are_distinct_from_task_or_human_acceptance(
    session_factory, tmp_path, phase, applications
):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication

    if phase == "prepared_selection":
        from test_subscription_review_selection import selection_case

        factory, primary, _ = await selection_case(session_factory, tmp_path)
        await SubscriptionDecisionApplication(factory).prepare_review_selection(
            primary.attempt.attempt_id
        )
    elif phase == "reopened":
        from test_subscription_candidate_redelegation import redelegation_case

        factory, primary, _ = await redelegation_case(session_factory, tmp_path)
        await SubscriptionDecisionApplication(factory).apply_delegation(primary.attempt.attempt_id)
    else:
        from test_subscription_acceptance_preparation import acceptance_case

        factory, primary, _ = await acceptance_case(session_factory, tmp_path)
        await SubscriptionDecisionApplication(factory).prepare_acceptance(
            primary.attempt.attempt_id
        )
    value = await _assessment(session_factory, primary.task.run_id)
    assert value.unverified_decisions == 0
    assert sum(item.applied_decisions for item in value.outcomes) == applications
    assert sum(item.task_acceptances for item in value.outcomes) == 0


@pytest.mark.integration
async def test_approved_reassignment_keeps_preferred_history_and_redacts_fallback_reason(
    session_factory, tmp_path, monkeypatch
):
    from dataclasses import replace

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import decode_subscription_record
    from test_subscription_reassignment import reassignment_case
    from test_subscription_usage import _reservation

    factory, application, primary, child, _ = await reassignment_case(
        session_factory, tmp_path, monkeypatch
    )
    assert (await application.apply_reassignment(primary.attempt.attempt_id)).accepted
    executor = SubscriptionDecisionExecutor(factory)
    admissions = [
        await executor.admit_next("after-reassignment-a", _reservation()),
        await executor.admit_next("after-reassignment-b", _reservation()),
    ]
    fallback = next(item for item in admissions if item and item.task.task_id == child.task.task_id)
    async with factory() as work:
        row = await work.session.get(SubscriptionAttempt, fallback.attempt.attempt_id)
        binding = decode_subscription_record(row.route_payload)
        mapping = replace(
            binding.mapping_applied,
            reason="Quota reported; token=[REDACTED] " + "Retained detail. " * 30,
        )
        row.route_payload = encode_subscription_record(replace(binding, mapping_applied=mapping))
        await work.commit()
    value = await _assessment(session_factory, primary.task.run_id)
    assert value.unverified_decisions == 0 and value.fallback_attempts == 1
    outcome = next(item for item in value.outcomes if item.fallback_attempts)
    assert outcome.effective_route.provider == "fallback"
    assert "REDACTED" in outcome.latest_fallback_reason
    assert len(outcome.latest_fallback_reason) == 255
    assert sum(item.task_acceptances for item in value.outcomes) == 0


@pytest.mark.integration
async def test_assessment_has_complete_bounded_groups_and_constant_query_count(
    session_factory, persisted_run, monkeypatch
):
    from forge.domain.run import RunSnapshot
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncSession

    for index in range(51):
        run = (
            persisted_run
            if index == 0
            else RunSnapshot(
                id=uuid4(),
                project_id=persisted_run.project_id,
                task_id=persisted_run.task_id,
                policy_version=1,
            )
        )
        if index:
            async with PostgresUnitOfWork(session_factory) as work:
                await work.runs.create(run)
                await work.commit()
        await _mixed_attempts(
            session_factory,
            run,
            primary=AttemptTelemetry(input_tokens=1),
            worker=AttemptTelemetry(input_tokens=3),
        )
    selects = []
    streams = []
    engine = session_factory.kw["bind"].sync_engine
    original = AsyncSession.stream

    async def streaming(session, statement, *args, **kwargs):
        assert statement.get_execution_options().get("yield_per") == 100
        value = await original(session, statement, *args, **kwargs)
        streams.append(value)
        return value

    def counted(connection, cursor, statement, parameters, context, executemany):
        selects.append(statement)

    monkeypatch.setattr(AsyncSession, "stream", streaming)
    event.listen(engine, "before_cursor_execute", counted)
    try:
        query = SubscriptionUsageQuery(session_factory)
        first = SubscriptionUsagePage.model_validate(
            await query.usage(limit=100, include_assessment=True)
        )
        count = len(selects)
        second = SubscriptionUsagePage.model_validate(
            await query.usage(offset=100, limit=100, include_assessment=True)
        )
    finally:
        event.remove(engine, "before_cursor_execute", counted)
    assert count == 4 and len(selects) == 8
    assert len(streams) == 6 and all(stream.closed for stream in streams)
    assert (len(first.items), len(second.items)) == (100, 2)
    assert len(first.assessment.outcomes) == 100 and len(second.assessment.outcomes) == 2
    assert first.assessment.outcomes_has_more and not second.assessment.outcomes_has_more
    assert first.assessment.shares == second.assessment.shares
    assert first.assessment.primary_turns == 51 and first.assessment.all_attempts == 102
    assert first.assessment.shares.input_tokens.share == 0.25
    assert {(item.run_id, item.purpose) for item in first.items}.isdisjoint(
        {(item.run_id, item.purpose) for item in second.items}
    )
