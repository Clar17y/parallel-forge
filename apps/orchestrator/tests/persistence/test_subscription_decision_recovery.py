"""Recovery consumes settled evidence without another provider invocation."""

from datetime import UTC, datetime, timedelta

import pytest
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.domain.run import RunState
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_plan_gate import proposal_case


@pytest.mark.integration
async def test_recovery_publishes_plan_after_original_lease_expires(session_factory, tmp_path):
    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    async with factory() as work:
        row = await work.session.get(SubscriptionScheduledTask, evidence.producer.task_id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(hours=1)
        await work.commit()
    recovery = SubscriptionDecisionRecovery(factory, store)
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 1
        assert (await work.subscription_budget.usage(run.id)).consumed.provider_attempts == 1


@pytest.mark.integration
async def test_paused_source_is_deferred_without_weakening_gate(session_factory, tmp_path):
    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    async with factory() as work:
        run = await work.runs.get_for_update(evidence.producer.run_id)
        await work.runs.pause(run.id, run.version, "run.paused", {})
        await work.commit()
    report = await SubscriptionDecisionRecovery(factory, store).reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (0, 1, 0)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.PAUSED and run.pending_gate is None
        assert len(await work.subscription_decisions.pending_applications(None, 100)) == 1


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["delegate", "wait"])
async def test_recovery_applies_existing_task_decisions_once(session_factory, tmp_path, kind):
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from test_subscription_delegation_application import delegation_case
    from test_subscription_wait_application import settled_wait

    if kind == "delegate":
        factory, admission, _, _ = await delegation_case(session_factory, tmp_path)
    else:
        factory, _, _, admission, _ = await settled_wait(session_factory, tmp_path)
    async with factory() as work:
        count = await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
    recovery = SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    )
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.accepted and result.disposition == (
            "delegated" if kind == "delegate" else "waiting"
        )
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
            == count
        )


@pytest.mark.integration
async def test_pending_decision_scan_uses_keyset_pages(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import PendingDecisionKind

    first_root, second_root = tmp_path / "one", tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    factory, store, first, _, _ = await proposal_case(session_factory, first_root)
    _, _, second, _, _ = await proposal_case(session_factory, second_root)
    expected = sorted((first.producer.attempt_id, second.producer.attempt_id))
    async with factory() as work:
        first_page = await work.subscription_decisions.pending_applications(None, 1)
        second_page = await work.subscription_decisions.pending_applications(
            first_page[0].attempt_id, 1
        )
        assert [first_page[0].attempt_id, second_page[0].attempt_id] == expected
        assert first_page[0].kind is second_page[0].kind is PendingDecisionKind.PLAN
        assert await work.subscription_decisions.pending_applications(expected[-1], 1) == ()
    report = await SubscriptionDecisionRecovery(factory, store, page_size=1).reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (2, 0, 0)


@pytest.mark.integration
async def test_periodic_retry_applies_plan_after_storage_recovers(
    session_factory, tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from forge.worker.main import _poll_decisions

    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    recovery = SubscriptionDecisionRecovery(factory, store)
    original_put = store.put_bytes

    async def unavailable(*args, **kwargs):
        raise OSError("temporary storage failure")

    monkeypatch.setattr(store, "put_bytes", unavailable)
    assert (await recovery.reconcile_all()).deferred == 1
    monkeypatch.setattr(store, "put_bytes", original_put)
    stop = asyncio.Event()

    async def retry():
        report = await recovery.reconcile_all()
        assert report.applied == 1 and report.deferred == 0
        stop.set()
        return report

    await asyncio.wait_for(_poll_decisions(SimpleNamespace(reconcile_all=retry), stop, 0.01), 5)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 1
        assert (await work.subscription_budget.usage(run.id)).consumed.provider_attempts == 1
