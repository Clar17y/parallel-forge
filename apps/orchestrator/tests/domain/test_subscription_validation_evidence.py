"""Versioned validation evidence binds working-tree contents and preserves legacy bytes."""

import json

import pytest
from forge.domain.evidence import (
    EvidenceManifestError,
    decode_evidence_manifest,
    encode_evidence_manifest,
)
from test_evidence import make_member, make_validation_manifest


def test_validation_v2_binds_candidate_contents_and_preserves_v1_wire():
    legacy = make_validation_manifest()
    legacy_wire = encode_evidence_manifest(legacy)
    assert json.loads(legacy_wire)["schema_version"] == 1
    assert "candidate_tree_digest" not in json.loads(legacy_wire)
    current = legacy.model_copy(update={"schema_version": 2, "candidate_tree_digest": "c" * 64})
    current_wire = encode_evidence_manifest(current)
    assert json.loads(current_wire)["candidate_tree_digest"] == "c" * 64
    assert decode_evidence_manifest(current_wire) == current
    assert encode_evidence_manifest(decode_evidence_manifest(legacy_wire)) == legacy_wire


@pytest.mark.parametrize(
    "schema,tree",
    [
        (2, None),
        (1, "c" * 64),
        (2.0, "c" * 64),
        (True, None),
        ("2", "c" * 64),
        (3, "c" * 64),
        (2, "C" * 64),
        (2, "c" * 63),
        (2, 0),
    ],
)
def test_validation_candidate_binding_rejects_schema_and_scalar_aliases(schema, tree):
    manifest = make_validation_manifest().model_copy(
        update={
            "schema_version": schema,
            "candidate_tree_digest": tree,
        }
    )
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(manifest)


def test_validation_v2_requires_a_controller_receipt_for_each_result():
    manifest = make_validation_manifest().model_copy(
        update={
            "schema_version": 2,
            "candidate_tree_digest": "c" * 64,
            "members": (make_member(),),
        }
    )
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(manifest)


def test_validation_v2_retains_receipt_and_v1_cannot_adopt_it():
    member = make_member().model_copy(update={"controller_receipt_digest": "d" * 64})
    manifest = make_validation_manifest().model_copy(
        update={
            "schema_version": 2,
            "candidate_tree_digest": "c" * 64,
            "members": (member,),
        }
    )
    wire = encode_evidence_manifest(manifest)
    assert json.loads(wire)["members"][0]["controller_receipt_digest"] == "d" * 64
    assert decode_evidence_manifest(wire) == manifest
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(
            manifest.model_copy(update={"schema_version": 1, "candidate_tree_digest": None})
        )
