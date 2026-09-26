"""A paused retained plan reaches its existing human approval gate on resume."""

import pytest
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_plan_gate import proposal_case
from test_subscription_resume_controls import pause_and_resume


@pytest.mark.integration
@pytest.mark.parametrize("scoped", [False, True])
async def test_pending_plan_resume_preserves_result_and_reaches_human_gate(
    session_factory, tmp_path, scoped
):
    factory, store, evidence, _plan, _ = await proposal_case(
        session_factory,
        tmp_path,
        plan_scope=("apps",) if scoped else None,
        plan_checks=("unit",) if scoped else ("plan",),
    )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, evidence.producer.attempt_id)
        attempt = await work.session.get(SubscriptionAttempt, evidence.producer.attempt_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, evidence.producer.task_id)
        original = canonical_digest(result.result_payload), result.result_digest
        assert attempt.status == scheduled.state == "reconciling"
        repairs = scheduled.repairs

    assert (
        await pause_and_resume(
            factory,
            session_factory,
            evidence.producer.run_id,
            FilesystemArtifactStore(tmp_path / "resume-artifacts"),
        )
        is None
    )
    proposal = await SubscriptionPlanGateService(store, factory).request_settled(
        evidence.producer.attempt_id
    )
    assert proposal.evidence_digest == canonical_digest(evidence.model_dump(mode="json"))
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, evidence.producer.attempt_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, evidence.producer.task_id)
        assert (canonical_digest(result.result_payload), result.result_digest) == original
        assert scheduled.repairs == repairs
        assert (
            await work.runs.get(evidence.producer.run_id)
        ).state is RunState.AWAITING_PLAN_APPROVAL
