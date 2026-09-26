"""Explicit subscription approval versions preserve retained legacy evidence."""

import hashlib
import json

import pytest
from forge.domain.approval import (
    PrApprovalEvidence,
    SubscriptionMergeApprovalEvidence,
    SubscriptionPrApprovalEvidence,
    canonical_digest,
    decode_merge_approval_evidence,
    decode_pr_approval_evidence,
)
from pydantic import ValidationError

from apps.orchestrator.tests.domain.test_approval import merge_evidence


def pr_evidence():
    return PrApprovalEvidence(
        candidate_commit="a" * 40,
        diff_digest="b" * 64,
        validation_digest="c" * 64,
        review_digest="d" * 64,
        repository="example/project",
        base_ref="refs/heads/main",
        base_sha="e" * 40,
        title="Approved task",
        body_digest="f" * 64,
        runner_mode="docker",
        runner_evidence_digest="0" * 64,
        remote_remediation_limit=2,
    )


@pytest.mark.parametrize(
    "factory,decoder,current",
    [
        (pr_evidence, decode_pr_approval_evidence, SubscriptionPrApprovalEvidence),
        (merge_evidence, decode_merge_approval_evidence, SubscriptionMergeApprovalEvidence),
    ],
)
def test_publication_versions_preserve_legacy_wire_and_use_actual_acceptance(
    factory, decoder, current
):
    legacy = factory()
    old = legacy.model_dump(mode="json")
    assert "schema_version" not in old and "acceptance_digest" not in old
    wire = json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
    assert canonical_digest(legacy) == hashlib.sha256(wire).hexdigest()
    assert type(decoder(wire)) is type(legacy) and decoder(wire) == legacy
    new = {key: value for key, value in old.items() if key != "review_digest"}
    new |= {"schema_version": 2, "acceptance_digest": "1" * 64, "candidate_tree_digest": "2" * 64}
    parsed = decoder(json.dumps(new))
    assert type(parsed) is current and "review_digest" not in parsed.model_dump()
    for version in (True, 1, 3, "2", None):
        with pytest.raises(ValidationError):
            decoder(json.dumps(new | {"schema_version": version}))
    with pytest.raises(ValidationError):
        decoder(json.dumps(new | {"review_digest": old["review_digest"]}))
    with pytest.raises(ValidationError):
        decoder(json.dumps({key: value for key, value in new.items() if key != "schema_version"}))
