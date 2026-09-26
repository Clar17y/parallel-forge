"""Every scheduler terminalization delivers the child's outcome to its parent."""
import pytest
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_wait_application import settled_wait


@pytest.mark.integration
@pytest.mark.parametrize("parent_kind", ["delegated", "waiting"])
@pytest.mark.parametrize("terminalizer", ["cancel", "reconcile"])
async def test_terminal_child_wakes_parent_without_normal_finish(session_factory, tmp_path, parent_kind, terminalizer):
    if parent_kind == "waiting":
        factory, _, application, parent, target = await settled_wait(session_factory, tmp_path)
        await application.apply_wait(parent.attempt.attempt_id)
    else:
        factory, parent, children, _ = await delegation_case(session_factory, tmp_path)
        target = children[0].task_id
        await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    async with factory() as work:
        version = (await work.session.get(SubscriptionTask, parent.task.task_id)).version
        if terminalizer == "reconcile":
            # No attempt was launched for this fixture child; exercise the
            # scheduler's explicit terminal reconciliation transition.
            row = await work.session.get(SubscriptionScheduledTask, target)
            row.state = "reconciling"
            await work.commit()
    async with factory() as work:
        if terminalizer == "cancel":
            await work.scheduler.request_stop(parent.task.run_id, target, cancel=True)
        else:
            await work.scheduler.reconcile_expired(parent.task.run_id, target, retry=False)
        await work.commit()
    async with factory() as work:
        logical = await work.session.get(SubscriptionTask, parent.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, parent.task.task_id)
        assert logical.state == scheduled.state == "queued"
        assert logical.version == version + 1
