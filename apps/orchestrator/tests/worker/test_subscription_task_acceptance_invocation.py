"""Only current stopped child-acceptance decisions enter immediate application."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.domain.subscription import AcceptDecision
from forge.worker.subscription_invocation import SubscriptionInvocationSession
from test_subscription_invocation import worker_case


@pytest.mark.parametrize("disposition", ["decision_pending", "stale", "fenced"])
@pytest.mark.parametrize("child", [True, False])
@pytest.mark.parametrize("final_handler", [False, True])
async def test_immediate_task_acceptance_dispatch_preserves_settlement_boundary(
    disposition, child, final_handler
):
    def session(admission, request):
        class Gateway:
            async def execute(self, value):
                return SubscriptionInvocationResult(
                    attempt=value.attempt,
                    decision=AcceptDecision(
                        run_id=value.attempt.run_id,
                        task_id=uuid4() if child else value.attempt.task_id,
                        candidate_commit=None,
                        candidate_tree_digest="a" * 64,
                        evidence_receipt_ids=(str(uuid4()),),
                        rationale="Accept verified outcome",
                    ),
                )

        async def revoke():
            pass

        return SubscriptionInvocationSession(Gateway(), revoke)

    worker, work, admission, _ = worker_case(session)

    async def settle(value, result):
        work.results.append(result)
        return SubscriptionSettlement(False, disposition)

    work.settle = settle
    applied = []

    async def accept(attempt_id):
        assert len(work.results) == 1
        applied.append(attempt_id)
        return SubscriptionSettlement(True, "task_accepted")

    worker._decisions = SimpleNamespace(prepare_acceptance=accept)
    if final_handler:
        worker._acceptance = SimpleNamespace(apply=accept)
    outcome = await worker.run_once()
    expected = (child or final_handler) and disposition == "decision_pending"
    assert applied == ([admission.attempt.attempt_id] if expected else [])
    assert (outcome.application is not None) == expected
