"""Recovery resumes final acceptance through verification and queue admission."""

import pytest
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_acceptance_dispatch import dispatch_case


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["prepared", "observed", "verified"])
async def test_recovery_queues_acceptance_validation_and_leaves_pending_scan(
    session_factory, tmp_path, phase
):
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    if phase != "prepared":
        await SubscriptionAcceptanceInspection(factory, service._snapshot).inspect(
            proposal.attempt_id
        )
    if phase == "verified":
        assert await service._receipts.verify(proposal.attempt_id) is not None
    async with factory() as work:
        pending = await work.subscription_decisions.pending_applications(None, 100)
        assert [(item.attempt_id, item.kind.value) for item in pending] == [
            (proposal.attempt_id, "final_acceptance")
        ]
    unconfigured = await SubscriptionDecisionRecovery(factory, service._store).reconcile_all()
    assert (unconfigured.applied, unconfigured.deferred, unconfigured.unsupported) == (0, 0, 1)
    recovery = SubscriptionDecisionRecovery(factory, service._store, acceptance=service)
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        assert await work.subscription_decisions.pending_applications(None, 100) == ()
