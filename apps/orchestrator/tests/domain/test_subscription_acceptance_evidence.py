"""Acceptance evidence retains real subscription sources and explicit review choices."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.evidence import (
    EvidenceManifestError,
    SubscriptionAcceptanceEvidenceManifest,
    decode_evidence_manifest,
    encode_evidence_manifest,
)
from forge.domain.subscription import (
    AcceptDecision,
    AuthMode,
    BillingMode,
    HandoffStatus,
    ReasoningEffort,
    ReviewedTaskHandoff,
    ReviewSelection,
    RouteSpec,
)


def acceptance_manifest(*, reviewed=False):
    run_id, primary_task_id = uuid4(), uuid4()
    handoff = None
    if reviewed:
        handoff = ReviewedTaskHandoff(
            run_id=run_id,
            task_id=uuid4(),
            attempt_id=uuid4(),
            status=HandoffStatus.COMPLETED,
            candidate_commit="b" * 40,
            candidate_tree_digest="c" * 64,
            evidence_receipt_ids=(str(uuid4()),),
            summary="Reviewed selected contents",
            review_output=ReviewOutput(
                decision=ReviewDecision.APPROVE,
                findings=(),
                tested_claims=("Focused checks",),
                missing_evidence=(),
                summary="Candidate is ready for primary acceptance",
            ),
        )
    selection = ReviewSelection(
        run_id=run_id,
        candidate_commit="b" * 40,
        candidate_tree_digest="c" * 64,
        review_required=reviewed,
        no_review_reason=None if reviewed else "Small repair with focused verification",
        reviewer_route=RouteSpec(
            provider="reviewer",
            client="reviewer-client",
            model="approved-reviewer",
            effort=ReasoningEffort.LOW,
            auth_mode=AuthMode.SUBSCRIPTION,
            billing_mode=BillingMode.ALLOWANCE_ONLY,
        )
        if reviewed
        else None,
        review_task_id=handoff.task_id if handoff else None,
    )
    return SubscriptionAcceptanceEvidenceManifest(
        evidence_set_id=uuid4(),
        run_id=run_id,
        step_id=uuid4(),
        policy_version=1,
        head_sha="b" * 40,
        base_sha="a" * 40,
        candidate_tree_digest="c" * 64,
        candidate_manifest_digest="d" * 64,
        candidate_epoch=1,
        producer_task_id=primary_task_id,
        producer_attempt_id=uuid4(),
        producer_result_digest="e" * 64,
        acceptance=AcceptDecision(
            run_id=run_id,
            task_id=primary_task_id,
            candidate_commit="b" * 40,
            candidate_tree_digest="c" * 64,
            evidence_receipt_ids=(str(uuid4()),),
            rationale="Accept the verified candidate",
        ),
        selection_attempt_id=uuid4(),
        selection_result_digest="f" * 64,
        selection_application_digest="1" * 64,
        selection=selection,
        review_handoff=handoff,
        review_result_digest="2" * 64 if reviewed else None,
        review_application_digest="3" * 64 if reviewed else None,
        receipt_evidence_digest="4" * 64,
        validation_evidence_set_id=uuid4(),
    )


@pytest.mark.parametrize("reviewed", [False, True])
def test_acceptance_manifest_round_trip_preserves_subscription_source_and_review_choice(reviewed):
    manifest = acceptance_manifest(reviewed=reviewed)
    wire = encode_evidence_manifest(manifest)
    decoded = decode_evidence_manifest(wire)
    assert decoded == manifest
    assert encode_evidence_manifest(decoded) == wire
    document = json.loads(wire)
    assert document["kind"] == "acceptance"
    assert document["producer_attempt_id"] == str(manifest.producer_attempt_id)
    assert "producer_execution_id" not in document
    assert document["receipt_evidence_digest"] == manifest.receipt_evidence_digest
    if reviewed:
        assert decoded.review_handoff.attempt_id == manifest.review_handoff.attempt_id
        assert decoded.review_handoff.review_output.decision is ReviewDecision.APPROVE
    else:
        assert decoded.selection.no_review_reason == manifest.selection.no_review_reason
        assert decoded.review_handoff is None
        assert decoded.review_result_digest is decoded.review_application_digest is None


@pytest.mark.parametrize(
    "change",
    [
        "foreign_primary",
        "foreign_selection",
        "same_attempt",
        "acceptance_tree",
        "selection_head",
        "review_task",
        "review_attempt",
        "review_tree",
        "review_verdict",
        "absent_review_proof",
    ],
)
def test_acceptance_manifest_refuses_crossed_source_bindings_even_after_model_copy(change):
    manifest = acceptance_manifest(reviewed=True)
    if change == "foreign_primary":
        updates = {"producer_task_id": uuid4()}
    elif change == "foreign_selection":
        updates = {"selection": replace(manifest.selection, run_id=uuid4())}
    elif change == "same_attempt":
        updates = {"producer_attempt_id": manifest.selection_attempt_id}
    elif change == "acceptance_tree":
        updates = {"acceptance": replace(manifest.acceptance, candidate_tree_digest="9" * 64)}
    elif change == "selection_head":
        updates = {"selection": replace(manifest.selection, candidate_commit="9" * 40)}
    elif change == "review_task":
        updates = {"review_handoff": replace(manifest.review_handoff, task_id=uuid4())}
    elif change == "review_attempt":
        updates = {
            "review_handoff": replace(
                manifest.review_handoff, attempt_id=manifest.producer_attempt_id
            )
        }
    elif change == "review_tree":
        updates = {
            "review_handoff": replace(manifest.review_handoff, candidate_tree_digest="9" * 64)
        }
    elif change == "review_verdict":
        updates = {
            "review_handoff": replace(
                manifest.review_handoff,
                review_output=ReviewOutput(
                    decision=ReviewDecision.REQUEST_CHANGES,
                    findings=(),
                    tested_claims=(),
                    missing_evidence=("Required check proof",),
                    summary="Repair required",
                ),
            )
        }
    else:
        updates = {"review_application_digest": None}
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(manifest.model_copy(update=updates))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("candidate_epoch", 1.0),
        ("policy_version", False),
        ("receipt_evidence_digest", None),
        ("producer_execution_id", "invented legacy execution"),
        ("unexpected", "must not be silently discarded"),
    ],
)
def test_acceptance_manifest_revalidates_bypassed_scalar_state(field, value):
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(acceptance_manifest().model_copy(update={field: value}))


def test_no_review_acceptance_cannot_smuggle_an_approving_report():
    unreviewed, reviewed = acceptance_manifest(), acceptance_manifest(reviewed=True)
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(
            unreviewed.model_copy(
                update={
                    "review_handoff": reviewed.review_handoff,
                    "review_result_digest": reviewed.review_result_digest,
                    "review_application_digest": reviewed.review_application_digest,
                }
            )
        )


@pytest.mark.parametrize("claims", [("not-a-receipt",), (str(uuid4()),) * 2])
def test_acceptance_manifest_requires_real_unique_receipt_identifiers(claims):
    manifest = acceptance_manifest()
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(
            manifest.model_copy(
                update={"acceptance": replace(manifest.acceptance, evidence_receipt_ids=claims)}
            )
        )


def test_acceptance_codec_rejects_nested_schema_alias_and_noncanonical_wire():
    wire = encode_evidence_manifest(acceptance_manifest())
    raw = json.loads(wire)
    raw["selection"]["schema_version"] = True
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(wire + b"\n")


def test_acceptance_codec_revalidates_nested_reviewer_model_state():
    manifest = acceptance_manifest(reviewed=True)
    handoff = replace(manifest.review_handoff)
    object.__setattr__(
        handoff,
        "review_output",
        handoff.review_output.model_copy(
            update={"unexpected": "must not disappear during serialization"}
        ),
    )
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(manifest.model_copy(update={"review_handoff": handoff}))
