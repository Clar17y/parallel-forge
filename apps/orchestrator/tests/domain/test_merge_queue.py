"""Queue admission is an immutable receipt, never evidence of a completed merge."""

import pytest
from pydantic import ValidationError


def receipt():
    return {
        "repository": "owner/repo",
        "pull_request_number": 7,
        "pull_request_node_id": "PR_exact",
        "entry_id": "MQE_exact",
        "head_sha": "a" * 40,
        "merge_method": "squash",
    }


def test_queue_receipt_is_frozen_and_round_trips_without_merge_completion():
    from forge.domain.merge_queue import MergeQueueReceipt

    value = MergeQueueReceipt.model_validate(receipt())
    assert value.model_dump(mode="json") == receipt()
    assert "merged" not in MergeQueueReceipt.model_fields
    with pytest.raises(ValidationError):
        value.head_sha = "b" * 40


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "owner/other/path"},
        {"repository": "owner/repo\n"},
        {"pull_request_number": True},
        {"pull_request_number": 0},
        {"pull_request_node_id": " "},
        {"entry_id": ""},
        {"head_sha": "a" * 39},
        {"head_sha": "A" * 40},
        {"merge_method": "MERGE"},
        {"merge_method": "unknown"},
        {"merged": True},
        {"merge_sha": "b" * 40},
    ],
)
def test_queue_receipt_rejects_invalid_identity_or_completion_fields(change):
    from forge.domain.merge_queue import MergeQueueReceipt

    with pytest.raises(ValidationError):
        MergeQueueReceipt.model_validate({**receipt(), **change})
