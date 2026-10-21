"""Primary-directed feedback domain bounds (issue #137)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.domain.subscription_feedback import (
    MAX_PRIMARY_FEEDBACK_PENDING,
    PrimaryFeedbackReceipt,
    SubscriptionPrimaryFeedbackRequest,
)


def test_primary_request_reuses_bounded_validation():
    req = SubscriptionPrimaryFeedbackRequest(
        expected_run_version=1, expected_task_version=2, feedback="Focus on the parser root cause."
    )
    assert req.feedback.startswith("Focus")
    with pytest.raises(ValueError):
        SubscriptionPrimaryFeedbackRequest(
            expected_run_version=1, expected_task_version=2, feedback="x" * 5000
        )
    with pytest.raises(ValueError, match="credential"):
        SubscriptionPrimaryFeedbackRequest(
            expected_run_version=1, expected_task_version=2, feedback="Authorization: Bearer [REDACTED]"
        )
    assert MAX_PRIMARY_FEEDBACK_PENDING == 8


def test_primary_receipt_requires_primary_target():
    receipt = PrimaryFeedbackReceipt(
        receipt_id=uuid4(), operator_id=uuid4(), run_id=uuid4(),
        primary_task_id=(pid := uuid4()), task_id=pid, status="pending",
        run_version=1, task_version=2, feedback_digest="a" * 64,
        binding_digest="b" * 64, feedback_bytes=10, observed_at=datetime.now(UTC),
    )
    assert receipt.primary_task_id == receipt.task_id
    with pytest.raises(ValueError):
        PrimaryFeedbackReceipt(
            receipt_id=uuid4(), operator_id=uuid4(), run_id=uuid4(),
            primary_task_id=uuid4(), task_id=uuid4(), status="pending",
            run_version=1, task_version=2, feedback_digest="a" * 64,
            binding_digest="b" * 64, feedback_bytes=10, observed_at=datetime.now(UTC),
        )
