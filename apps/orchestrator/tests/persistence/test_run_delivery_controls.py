"""PostgreSQL coverage for run delivery pause and remediation controls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from forge.domain.approval import ApprovalGate
from forge.domain.errors import InvalidTransition
from forge.domain.run import RunState, SuspensionKind
from forge.persistence.repositories.runs import ConcurrencyConflict, PersistenceError
from forge.persistence.unit_of_work import PostgresUnitOfWork


async def _transition(work, run_id, version, target):
    return await work.runs.transition(
        run_id, version, target, "run.transitioned", {"target": target.value}
    )


async def _to_validating(work, run_id):
    planning = await _transition(work, run_id, 0, RunState.PLANNING)
    approved = await work.runs.await_approval(
        run_id, planning.version, ApprovalGate.PLAN, "c" * 64, "run.awaiting_plan_approval", {}
    )
    prepared = await _transition(work, run_id, approved.version, RunState.PREPARING_WORKTREE)
    implementing = await _transition(work, run_id, prepared.version, RunState.IMPLEMENTING)
    return await _transition(work, run_id, implementing.version, RunState.VALIDATING)


@pytest.mark.integration
async def test_pause_resume_preserves_nested_intervention_and_gate_metadata(uow, persisted_run):
    async with uow:
        planning = await _transition(uow, persisted_run.id, 0, RunState.PLANNING)
        intervened = await uow.runs.intervene(
            persisted_run.id, planning.version, "run.intervened", {}
        )
        paused = await uow.runs.pause(
            persisted_run.id,
            intervened.version,
            "run.paused",
            {},
            actor_class="operator",
            occurred_at=datetime(2026, 9, 7, tzinfo=UTC),
        )
        resumed = await uow.runs.resume(
            persisted_run.id, paused.version, "run.resumed", {}, actor_class="operator"
        )
        await uow.commit()

    assert paused.suspended_state is RunState.AWAITING_HUMAN_INTERVENTION
    assert paused.suspension_kind is SuspensionKind.PAUSE
    assert resumed.state is RunState.AWAITING_HUMAN_INTERVENTION
    assert resumed.suspended_state is RunState.PLANNING
    assert resumed.suspension_kind is SuspensionKind.INTERVENTION


@pytest.mark.integration
async def test_pause_resume_preserves_approval_gate_metadata(uow, persisted_run):
    digest = "a" * 64
    async with uow:
        planning = await _transition(uow, persisted_run.id, 0, RunState.PLANNING)
        gate = await uow.runs.await_approval(
            persisted_run.id,
            planning.version,
            ApprovalGate.PLAN,
            digest,
            "run.awaiting_plan_approval",
            {},
        )
        paused = await uow.runs.pause(persisted_run.id, gate.version, "run.paused", {})
        resumed = await uow.runs.resume(persisted_run.id, paused.version, "run.resumed", {})
        await uow.commit()

    assert resumed.state is RunState.AWAITING_PLAN_APPROVAL
    assert resumed.pending_gate is ApprovalGate.PLAN
    assert resumed.pending_evidence_digest == digest
    assert resumed.version == gate.version + 2


@pytest.mark.integration
async def test_pause_stale_version_and_event_failure_leave_no_change(
    uow, persisted_run, monkeypatch
):
    with pytest.raises(ConcurrencyConflict):
        async with uow:
            await uow.runs.pause(persisted_run.id, 1, "run.paused", {})

    async with uow:
        append = uow.events.append

        async def append_then_fail(event):
            await append(event)
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(uow.events, "append", append_then_fail)
        with pytest.raises(RuntimeError, match="event failure"):
            await uow.runs.pause(persisted_run.id, 0, "run.paused", {})
        await uow.commit()

    async with uow:
        assert await uow.runs.get(persisted_run.id) == persisted_run
        assert await uow.events.list_after(persisted_run.id, 0) == []


@pytest.mark.integration
async def test_automatic_remediation_counts_until_limit_then_intervenes(uow, persisted_run):
    async with uow:
        current = await _to_validating(uow, persisted_run.id)
        for count in range(1, 4):
            current = await uow.runs.begin_local_remediation(
                persisted_run.id,
                current.version,
                automatic=True,
                limit=3,
                event_type="run.local_remediation",
                event_payload={"cycle": count},
            )
            assert current.state is RunState.REMEDIATING
            assert current.local_remediation_count == count
            current = await _transition(uow, persisted_run.id, current.version, RunState.VALIDATING)
        intervened = await uow.runs.begin_local_remediation(
            persisted_run.id,
            current.version,
            automatic=True,
            limit=3,
            event_type="run.local_remediation_exhausted",
            event_payload={},
        )
        await uow.commit()

    assert intervened.state is RunState.AWAITING_HUMAN_INTERVENTION
    assert intervened.suspended_state is RunState.VALIDATING
    assert intervened.local_remediation_count == 3


@pytest.mark.integration
async def test_zero_limit_intervenes_without_increment(uow, persisted_run):
    async with uow:
        current = await _to_validating(uow, persisted_run.id)
        changed = await uow.runs.begin_local_remediation(
            persisted_run.id,
            current.version,
            automatic=True,
            limit=0,
            event_type="run.local_remediation_exhausted",
            event_payload={},
        )
        await uow.commit()
    assert changed.state is RunState.AWAITING_HUMAN_INTERVENTION
    assert changed.local_remediation_count == 0


@pytest.mark.integration
async def test_remediation_event_failure_rolls_back_counter_and_state(
    uow, persisted_run, monkeypatch
):
    async with uow:
        current = await _to_validating(uow, persisted_run.id)
        await uow.commit()

    async with uow:
        append = uow.events.append

        async def append_then_fail(event):
            await append(event)
            raise RuntimeError("injected remediation event failure")

        monkeypatch.setattr(uow.events, "append", append_then_fail)
        with pytest.raises(RuntimeError, match="remediation event failure"):
            await uow.runs.begin_local_remediation(
                persisted_run.id,
                current.version,
                automatic=True,
                limit=3,
                event_type="run.local_remediation",
                event_payload={},
            )
        await uow.commit()

    async with uow:
        loaded = await uow.runs.get(persisted_run.id)
        events = await uow.events.list_after(persisted_run.id, 0)
    assert loaded == current
    assert all(event.event_type != "run.local_remediation" for event in events)


@pytest.mark.integration
async def test_human_pr_feedback_is_uncounted_and_wrong_phases_or_limits_fail(uow, persisted_run):
    digest = "b" * 64
    async with uow:
        current = await _to_validating(uow, persisted_run.id)
        current = await _transition(uow, persisted_run.id, current.version, RunState.REVIEWING)
        approval = await uow.runs.await_approval(
            persisted_run.id,
            current.version,
            ApprovalGate.PR,
            digest,
            "run.awaiting_pr_approval",
            {},
        )
        changed = await uow.runs.begin_local_remediation(
            persisted_run.id,
            approval.version,
            automatic=False,
            limit=0,
            event_type="run.pr_feedback",
            event_payload={},
        )
        await uow.commit()
    assert changed.state is RunState.REMEDIATING
    assert changed.local_remediation_count == 0
    assert changed.pending_gate is None

    for limit in (True, -1, 1.0):
        with pytest.raises((TypeError, ValueError, PersistenceError)):
            async with uow:
                await uow.runs.begin_local_remediation(
                    persisted_run.id,
                    changed.version,
                    automatic=True,
                    limit=limit,
                    event_type="run.local_remediation",
                    event_payload={},
                )
    with pytest.raises(InvalidTransition):
        async with uow:
            await uow.runs.begin_local_remediation(
                persisted_run.id,
                changed.version,
                automatic=False,
                limit=1,
                event_type="run.pr_feedback",
                event_payload={},
            )
    with pytest.raises(InvalidTransition):
        async with uow:
            await uow.runs.begin_local_remediation(
                persisted_run.id,
                changed.version,
                automatic=True,
                limit=1,
                event_type="run.local_remediation",
                event_payload={},
            )


@pytest.mark.integration
async def test_concurrent_same_version_remediation_increments_once(session_factory, persisted_run):
    setup = PostgresUnitOfWork(session_factory)
    async with setup:
        current = await _to_validating(setup, persisted_run.id)
        await setup.commit()

    async def attempt():
        work = PostgresUnitOfWork(session_factory)
        async with work:
            changed = await work.runs.begin_local_remediation(
                persisted_run.id,
                current.version,
                automatic=True,
                limit=3,
                event_type="run.local_remediation",
                event_payload={},
            )
            await work.commit()
            return changed

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert sum(isinstance(result, ConcurrencyConflict) for result in results) == 1
    assert sum(getattr(result, "local_remediation_count", None) == 1 for result in results) == 1
