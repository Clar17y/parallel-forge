"""Definite handoff mismatches consume bounded repair or wake the primary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_handoff import RejectedSubscriptionHandoff
from forge.domain.subscription import TaskBudget
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.subscription_handoff_evidence import (
    PostgresSubscriptionHandoffEvidence,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_handoff_application import application_case


@pytest.mark.integration
@pytest.mark.parametrize("repairs", [0, 1])
async def test_rejected_output_is_repaired_or_reported_once(
    session_factory, tmp_path, monkeypatch, repairs
):
    factory, parent, child, application, observation, proof = await application_case(
        session_factory,
        tmp_path,
        monkeypatch,
        repairs=repairs,
    )
    rejected = RejectedSubscriptionHandoff(
        replace(proof, current_tree_digest=None),
        proof.current_tree_digest,
        "4" * 64,
        "a" * 40,
    )
    result = await application.reject_handoff(observation, rejected)
    assert not result.accepted and not result.replayed
    assert result.disposition == ("handoff_repair_queued" if repairs else "handoff_rejected")
    assert (await application.reject_handoff(observation, rejected)).replayed
    assert (await application.handoff_replay(child.attempt.attempt_id)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
        stored = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert task.state == scheduled.state == ("queued" if repairs else "terminal")
        assert scheduled.lease_owner is None
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == (
            "blocked" if repairs else "queued"
        )
        assert stored.application_payload["rejection_reason"] == "outputs_changed"
        assert (await work.subscription_budget.usage(task.run_id)).consumed.repairs == repairs
    if repairs:
        from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
        from test_subscription_usage import _reservation

        retry = await SubscriptionDecisionExecutor(factory).admit_next(
            "repair-child",
            replace(
                _reservation(),
                max_duration_seconds=1,
                max_input_tokens=1,
                max_output_tokens=1,
                max_cost_minor=1,
                max_tool_calls=1,
                max_named_checks=0,
            ),
        )
        assert retry is not None and retry.attempt.attempt_number == 2
        assert (await application.reject_handoff(observation, rejected)).replayed
        async with factory() as work:
            assert (await work.session.get(SubscriptionTask, child.task.task_id)).state == "running"
            assert (
                await work.session.get(SubscriptionScheduledTask, child.task.task_id)
            ).lease_owner == "repair-child"


@pytest.mark.integration
async def test_rejection_respects_exhausted_run_budget(session_factory, tmp_path, monkeypatch):
    factory, _, child, application, observation, proof = await application_case(
        session_factory,
        tmp_path,
        monkeypatch,
        repairs=1,
        primary_budget=TaskBudget(),
    )
    rejected = RejectedSubscriptionHandoff(
        replace(proof, current_tree_digest=None), "3" * 64, "4" * 64, "a" * 40
    )
    assert (
        await application.reject_handoff(observation, rejected)
    ).disposition == "handoff_rejected"
    async with factory() as work:
        assert (await work.subscription_budget.usage(child.task.run_id)).consumed.repairs == 0


@pytest.mark.integration
@pytest.mark.parametrize("change", ["expired", "cancel", "evidence", "no_mismatch"])
async def test_stale_or_unproved_rejection_never_debits_repair(
    session_factory, tmp_path, monkeypatch, change
):
    factory, _, child, application, observation, proof = await application_case(
        session_factory, tmp_path, monkeypatch, repairs=1
    )
    rejected = RejectedSubscriptionHandoff(
        replace(proof, current_tree_digest=None),
        "3" * 64,
        proof.output_digest if change == "no_mismatch" else "4" * 64,
        "a" * 40,
    )
    if change == "evidence":

        async def unavailable(self, value):
            return False

        monkeypatch.setattr(PostgresSubscriptionHandoffEvidence, "verify", unavailable)
    elif change in {"expired", "cancel"}:
        async with factory() as work:
            if change == "expired":
                row = await work.session.get(
                    SubscriptionHandoffFence, observation.proposal.worktree.identity.worktree_name
                )
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                row = await work.session.get(SubscriptionTask, child.task.task_id)
                row.cancel_requested = True
            await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.reject_handoff(observation, rejected)
    async with factory() as work:
        assert (await work.subscription_budget.usage(child.task.run_id)).consumed.repairs == 0
        stored = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert stored.application_payload is None and stored.disposition == "decision_pending"
