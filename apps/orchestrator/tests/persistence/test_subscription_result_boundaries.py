"""Adversarial settlement boundaries for admitted subscription attempts."""

import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.domain.subscription import HandoffStatus, TaskHandoff, decode_subscription_record
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.repositories.subscription import SubscriptionConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, select, update
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_execution_constraints import _admitted
from test_subscription_usage import _known, _reservation


def _failure(admission, **changes):
    return replace(
        SubscriptionInvocationResult(
            attempt=admission.attempt,
            failure=SubscriptionFailure.PROTOCOL,
            failure_detail="invalid terminal frame",
            telemetry=_known(input_tokens=5),
        ),
        **changes,
    )


async def _usage_and_rows(session_factory, run_id, attempt_id):
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(run_id)
        result = await work.session.get(SubscriptionAttemptResult, attempt_id)
        repairs = await work.session.scalar(
            select(func.count())
            .select_from(SubscriptionRepairDebit)
            .where(SubscriptionRepairDebit.attempt_id == attempt_id)
        )
        attempt = await work.session.get(SubscriptionAttempt, attempt_id)
        result_snapshot = (
            None
            if result is None
            else {"accepted": result.accepted, "payload": dict(result.result_payload)}
        )
        attempt_status = None if attempt is None else attempt.status
        return usage, result_snapshot, repairs, attempt_status


@pytest.mark.integration
@pytest.mark.parametrize(
    "changed",
    [
        {"telemetry": _known(input_tokens=6)},
        {"failure": SubscriptionFailure.DEADLINE},
        {"failure_detail": "different failure"},
    ],
)
async def test_result_replay_is_exact_and_conflicts_do_not_change_first_settlement(
    session_factory, persisted_run, changed
):
    executor, admission = await _admitted(session_factory, persisted_run)
    original = _failure(admission)
    first = await executor.settle(admission, original)
    replay = await executor.settle(admission, original)
    assert replay == replace(first, replayed=True)

    with pytest.raises(SubscriptionConflict, match="result replay conflicts"):
        await executor.settle(admission, replace(original, **changed))

    usage, result, repairs, _ = await _usage_and_rows(
        session_factory, persisted_run.id, admission.attempt.attempt_id
    )
    assert usage.consumed.provider_attempts == 1
    assert usage.consumed.input_tokens == 5
    assert usage.outstanding.provider_attempts == 1
    assert result is not None and result["payload"]["failure_detail"] == "invalid terminal frame"
    assert repairs == 1


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["expired", "cancelled"])
async def test_stale_or_cancelled_lease_retains_usage_without_accepted_result_or_repair(
    session_factory, persisted_run, boundary
):
    executor, admission = await _admitted(session_factory, persisted_run)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(SubscriptionScheduledTask)
            .where(SubscriptionScheduledTask.task_id == admission.attempt.task_id)
            .values(
                **(
                    {"cancel_requested": True}
                    if boundary == "cancelled"
                    else {"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
                )
            )
        )

    settlement = await executor.settle(admission, _failure(admission))
    assert not settlement.accepted and settlement.disposition == "stale"
    usage, result, repairs, attempt_status = await _usage_and_rows(
        session_factory, persisted_run.id, admission.attempt.attempt_id
    )
    assert usage.consumed.provider_attempts == 1 and usage.consumed.input_tokens == 5
    assert usage.outstanding.provider_attempts == 0
    assert result is not None and result["accepted"] is False
    assert repairs == 0
    assert attempt_status == "reconciling"


@pytest.mark.integration
@pytest.mark.parametrize("fence", ["effect", "uncertain_launch"])
async def test_unresolved_effect_or_uncertain_launch_keeps_result_fenced(
    session_factory, persisted_run, fence
):
    executor, admission = await _admitted(session_factory, persisted_run)
    async with session_factory() as session, session.begin():
        if fence == "effect":
            session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=admission.attempt.run_id,
                    task_id=admission.attempt.task_id,
                    lease_owner=admission.lease.owner,
                    lease_generation=admission.lease.generation,
                    candidate_epoch=admission.candidate_epoch,
                    whole_worktree_exclusive=False,
                    state="admitted",
                )
            )
        else:
            session.add(
                SubscriptionClientLaunch(
                    attempt_id=admission.attempt.attempt_id,
                    launch_id="uncertain-launch",
                    worker_identity=admission.lease.owner,
                    state="uncertain",
                    terminal_payload=None,
                )
            )

    settlement = await executor.settle(admission, _failure(admission))
    assert not settlement.accepted and settlement.disposition == "fenced"
    usage, result, repairs, attempt_status = await _usage_and_rows(
        session_factory, persisted_run.id, admission.attempt.attempt_id
    )
    assert usage.consumed.provider_attempts == 1 and usage.consumed.input_tokens == 5
    assert usage.outstanding.provider_attempts == 0
    assert result is not None and result["accepted"] is False
    assert repairs == 0
    assert attempt_status == "reconciling"


@pytest.mark.integration
async def test_rolled_back_settlement_retries_once_without_duplicate_usage_or_repair(
    session_factory, persisted_run
):
    executor, admission = await _admitted(session_factory, persisted_run)
    result = _failure(admission)
    async with PostgresUnitOfWork(session_factory) as work:
        provisional = await work.subscription_execution.settle(admission, result)
        assert provisional.accepted and provisional.disposition == "repair_queued"
        await work.rollback()

    usage, stored, repairs, attempt_status = await _usage_and_rows(
        session_factory, persisted_run.id, admission.attempt.attempt_id
    )
    assert usage.consumed.provider_attempts == 0
    assert usage.outstanding.provider_attempts == 1
    assert stored is None and repairs == 0
    assert attempt_status == "running"

    settled = await executor.settle(admission, result)
    assert settled.accepted and settled.disposition == "repair_queued"
    assert (await executor.settle(admission, result)).replayed
    usage, stored, repairs, _ = await _usage_and_rows(
        session_factory, persisted_run.id, admission.attempt.attempt_id
    )
    assert usage.consumed.provider_attempts == 1 and usage.consumed.input_tokens == 5
    assert usage.outstanding.provider_attempts == 1
    assert stored is not None and repairs == 1


@pytest.mark.integration
async def test_policy_denial_is_terminal_blocked_without_repair_or_fallback(
    session_factory, persisted_run
):
    executor, admission = await _admitted(session_factory, persisted_run)
    result = _failure(admission, failure=SubscriptionFailure.POLICY_DENIED)
    settlement = await executor.settle(admission, result)
    assert settlement.accepted and settlement.disposition == "failed"
    usage, stored, repairs, attempt_status = await _usage_and_rows(
        session_factory, persisted_run.id, admission.attempt.attempt_id
    )
    assert usage.consumed.provider_attempts == 1
    assert usage.outstanding.provider_attempts == 0
    assert stored is not None and stored["accepted"] is True
    assert repairs == 0 and attempt_status == "terminal"
    async with session_factory() as session:
        decisions = (
            await session.scalars(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.attempt_id == admission.attempt.attempt_id
                )
            )
        ).all()
        assert len(decisions) == 1
        assert decisions[0].record_type == "TaskHandoff"
        handoff = decode_subscription_record(decisions[0].payload)
        assert isinstance(handoff, TaskHandoff)
        assert handoff.status is HandoffStatus.BLOCKED


@pytest.mark.integration
async def test_failed_repair_admission_rolls_back_slot_transfer(
    session_factory, persisted_run, monkeypatch
):
    executor, admission = await _admitted(session_factory, persisted_run)
    assert (await executor.settle(admission, _failure(admission))).disposition == "repair_queued"

    async def fail_before_commit(work):
        transferred = await work.session.get(SubscriptionRepairDebit, admission.attempt.attempt_id)
        assert transferred is not None and transferred.next_attempt_id is not None
        raise RuntimeError("injected failure before admission commit")

    with monkeypatch.context() as scoped:
        scoped.setattr(PostgresUnitOfWork, "commit", fail_before_commit)
        with pytest.raises(RuntimeError, match="before admission commit"):
            await executor.admit_next("worker", _reservation())
    async with session_factory() as session:
        debit = await session.get(SubscriptionRepairDebit, admission.attempt.attempt_id)
        attempts = (
            await session.scalars(
                select(SubscriptionAttempt).where(
                    SubscriptionAttempt.task_row_id == admission.attempt.task_id
                )
            )
        ).all()
        assert debit is not None and debit.next_attempt_id is None
        assert len(attempts) == 1
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.outstanding.provider_attempts == 1
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.attempt.task_id)
        assert scheduled.state == "queued" and scheduled.lease_owner is None
    retried = await executor.admit_next("restarted-worker", _reservation())
    assert retried is not None and retried.attempt.attempt_number == 2


@pytest.mark.integration
async def test_result_only_evidence_blocks_result_migration_downgrade(
    session_factory, persisted_run
):
    executor, admission = await _admitted(session_factory, persisted_run)
    result = _failure(admission, failure=SubscriptionFailure.POLICY_DENIED)
    assert (await executor.settle(admission, result)).accepted
    async with session_factory() as session:
        assert await session.get(SubscriptionRepairDebit, admission.attempt.attempt_id) is None

    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260910_0014_subscription_results.py"
    )
    spec = importlib.util.spec_from_file_location("subscription_results_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    async with session_factory() as session, session.begin():
        connection = await session.connection()
        with pytest.raises(RuntimeError, match="cannot discard subscription result"):
            await connection.run_sync(downgrade)
