"""Reuse the exact validated plan for material-question decisions."""

import hashlib
from collections import Counter

import pytest
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.domain.plan import decode_plan_output, material_questions
from legacy_upgrade_case import legacy_plan_case
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_plan_gate import proposal_case


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["legacy", "subscription", "subscription_questions"])
async def test_validation_returns_bound_plan_with_one_read_per_artifact(
    session_factory, tmp_path, monkeypatch, kind
):
    handlers = None
    questions = ("Retention period?",) if kind == "subscription_questions" else ()
    if kind == "legacy":
        case = await legacy_plan_case(session_factory, tmp_path)
        factory, store, validator = case.factory, case.store, case.validator
        expected, run_id, evidence_digest = (
            case.evidence,
            case.run.id,
            case.run.pending_evidence_digest,
        )
        handlers = case.handlers
    else:
        factory, store, expected, proposed, validator = await proposal_case(
            session_factory,
            tmp_path,
            plan_scope=("apps",),
            material_questions=questions,
        )
        outcome = await SubscriptionPlanGateService(store, factory).request(expected, proposed)
        run_id, evidence_digest = expected.producer.run_id, outcome.evidence_digest
    reads = []
    contents = {}
    open_bytes = store.open_bytes

    async def record_read(digest):
        reads.append(digest)
        contents[digest] = await open_bytes(digest)
        return contents[digest]

    monkeypatch.setattr(store, "open_bytes", record_read)
    try:
        async with factory() as work:
            evidence, plan = await validator.validate_with_plan(work, run_id)
            assert evidence == expected
            assert (
                hashlib.sha256(contents[expected.plan_digest]).hexdigest() == expected.plan_digest
            )
            assert plan == decode_plan_output(contents[expected.plan_digest])
            assert plan.summary and material_questions(plan) == questions
        assert Counter(reads) == Counter({evidence_digest: 1, expected.plan_digest: 1})
    finally:
        if handlers is not None:
            await handlers.aclose()
