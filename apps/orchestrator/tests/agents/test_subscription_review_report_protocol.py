"""Reviewer verdicts survive protocol and durable record encoding."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import ProtocolError, decode_final, output_schema
from forge.domain.agent import ReviewDecision
from forge.domain.subscription import (
    SpecialistPurpose,
    decode_subscription_record,
    encode_subscription_record,
)
from test_subscription_protocol import _request


def reviewer_request():
    request = _request()
    purpose = SpecialistPurpose.INDEPENDENT_REVIEW
    return replace(
        request,
        task=replace(request.task, purpose=purpose),
        authorization=replace(request.authorization, role=purpose),
        envelope=replace(
            request.envelope, routes=(*request.envelope.routes, (purpose, request.task.route))
        ),
    )


def payload():
    return {
        "kind": "handoff",
        "status": "completed",
        "candidate_tree_digest": "a" * 64,
        "evidence_receipt_ids": [str(uuid4())],
        "summary": "Reviewed candidate",
        "review_output": {
            "decision": "approve",
            "findings": [],
            "tested_claims": ["Inspected candidate diff"],
            "missing_evidence": [],
            "summary": "No findings",
        },
    }


def test_reviewer_report_roundtrips_and_schema_requires_report():
    request = reviewer_request()
    decision = decode_final(payload(), request).decision
    assert decision.review_output.decision is ReviewDecision.APPROVE
    encoded = encode_subscription_record(decision)
    assert encoded["record"]["$record"] == "ReviewedTaskHandoff"
    assert decode_subscription_record(encoded) == decision
    schema = output_schema(request)
    handoff_schema = next(
        choice
        for choice in schema["properties"]["decision"]["anyOf"]
        if choice["properties"]["kind"]["const"] == "handoff"
    )
    assert "review_output" in handoff_schema["required"]
    old = payload()
    del old["review_output"]
    with pytest.raises(ProtocolError):
        decode_final(old, request)


@pytest.mark.parametrize(
    "purpose", [SpecialistPurpose.PRIMARY, SpecialistPurpose.ROUTINE_IMPLEMENTATION]
)
def test_other_roles_cannot_supply_review_verdict(purpose):
    with pytest.raises(ProtocolError):
        decode_final(payload(), _request(purpose=purpose))


def test_review_report_preserves_legacy_handoff_encoding():
    request = _request(purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION)
    old = payload()
    del old["review_output"]
    handoff = decode_final(old, request).decision
    encoded = encode_subscription_record(handoff)
    assert encoded["record"]["$record"] == "TaskHandoff"
    assert all(name != "review_output" for name, _ in encoded["record"]["fields"])
    assert encode_subscription_record(decode_subscription_record(encoded)) == encoded


@pytest.mark.parametrize(
    "change", ["missing_evidence", "unknown_decision", "extra_field", "blocked_without_evidence"]
)
def test_reviewer_report_rejects_invalid_verdicts(change):
    value = payload()
    report = value["review_output"]
    if change == "missing_evidence":
        report["missing_evidence"] = ["No validation evidence"]
    elif change == "unknown_decision":
        report["decision"] = "maybe"
    elif change == "extra_field":
        report["human_approved"] = True
    else:
        report["decision"] = "blocked"
    with pytest.raises(ProtocolError):
        decode_final(value, reviewer_request())


def test_encoded_report_does_not_accept_invalid_replacement():
    encoded = encode_subscription_record(decode_final(payload(), reviewer_request()).decision)
    report = next(value for name, value in encoded["record"]["fields"] if name == "review_output")
    report["$review_output"]["decision"] = "not-a-verdict"
    with pytest.raises(ValueError):
        decode_subscription_record(encoded)
