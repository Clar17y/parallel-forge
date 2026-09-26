"""Immediate candidate application requires a current stopped result."""

import asyncio
from types import SimpleNamespace

import pytest
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.domain.subscription import ReviewSelection
from forge.worker.subscription_invocation import SubscriptionInvocationSession
from test_subscription_invocation import worker_case


@pytest.mark.parametrize("disposition", ["decision_pending", "stale", "fenced"])
@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.parametrize("stopping", [False, True])
async def test_immediate_candidate_dispatch_requires_current_result_and_configured_service(
    disposition, configured, stopping
):
    def session(admission, request):
        class Gateway:
            async def execute(self, value):
                return SubscriptionInvocationResult(
                    attempt=value.attempt,
                    decision=ReviewSelection(
                        run_id=value.attempt.run_id,
                        candidate_commit=None,
                        candidate_tree_digest="a" * 64,
                        review_required=False,
                        no_review_reason="Policy permits focused checks for this small repair",
                    ),
                )

        async def revoke():
            pass

        return SubscriptionInvocationSession(Gateway(), revoke)

    applied = []

    async def apply(attempt_id):
        assert len(work.results) == 1
        applied.append(attempt_id)
        return SubscriptionSettlement(True, "review_selected")

    worker, work, admission, _ = worker_case(
        session, candidates=SimpleNamespace(apply=apply) if configured else None
    )
    stop = asyncio.Event()

    async def settle(value, result):
        work.results.append(result)
        if stopping:
            stop.set()
        return SubscriptionSettlement(False, disposition)

    work.settle = settle
    outcome = await worker.run_once(stop_event=stop)
    expected = configured and not stopping and disposition == "decision_pending"
    assert applied == ([admission.attempt.attempt_id] if expected else [])
    assert (outcome.application is not None) == expected
