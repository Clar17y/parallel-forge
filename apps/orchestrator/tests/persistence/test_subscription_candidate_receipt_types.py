"""Canonical candidate receipts reject numeric aliases, even when rehashed."""

from copy import deepcopy

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_candidate import SubscriptionCandidateApplication
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.domain.operation import canonical_digest
from forge.domain.subscription import decode_subscription_record
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from sqlalchemy.orm.attributes import flag_modified
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_review_selection import selection_case


@pytest.mark.integration
@pytest.mark.parametrize("change", [
    "schema_bool", "epoch_bool", "epoch_float", "rejected_epoch", "selected_schema", "child_schema"
])
async def test_candidate_application_refuses_numeric_aliases_in_retained_receipts(
    session_factory, tmp_path, change
):
    snapshot = GitWorkingTreeSnapshot(head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=())
    selected = change in {"selected_schema", "child_schema"}
    factory, primary, _ = await selection_case(
        session_factory, tmp_path, review_required=selected,
        tree_digest=snapshot.candidate_tree_digest if selected else "a" * 64,
    )

    async def capture(proposal):
        return snapshot

    service = SubscriptionCandidateApplication(factory, capture)
    if change == "rejected_epoch" or selected:
        await service.apply(primary.attempt.attempt_id)
    else:
        await SubscriptionDecisionApplication(factory).prepare_review_selection(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        payload = deepcopy(result.application_payload)
        if change == "schema_bool":
            payload["schema_version"] = True
        elif change == "epoch_bool":
            assert payload["candidate_epoch"] == 1
            payload["candidate_epoch"] = True
        elif change == "epoch_float":
            payload["candidate_epoch"] = float(payload["candidate_epoch"])
        elif change == "rejected_epoch":
            payload["rejection"]["reopened_epoch"] = float(payload["rejection"]["reopened_epoch"])
        elif change == "selected_schema":
            payload["selection"]["review_task"]["schema_version"] = True
        else:
            contract = decode_subscription_record(payload["selection"]["review_task"])
            child = await work.session.get(SubscriptionTask, contract.task_id)
            child.payload = {**child.payload, "schema_version": True}
            flag_modified(child, "payload")
        result.application_payload = payload
        flag_modified(result, "application_payload")
        result.application_digest = canonical_digest(payload)
        await work.commit()
    with pytest.raises(SubscriptionDecisionError, match="candidate intent replay differs"):
        await service.apply(primary.attempt.attempt_id)
