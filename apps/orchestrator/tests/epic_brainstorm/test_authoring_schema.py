from __future__ import annotations

from uuid import uuid4

import pytest
from forge.agents.epic_brainstorm_protocol import authoring_schema, proposal_from_output
from forge.agents.subscription_protocol import ProtocolError
from forge.domain.epic_brainstorm import BrainstormProposal
from forge.domain.epic_decomposition import DecompositionProposal

from apps.orchestrator.tests.epic_brainstorm.test_gateway import _make_snapshot
from apps.orchestrator.tests.epic_decomposition.test_protocols import (
    _make_decomposition_snapshot,
    _make_valid_decomposition_proposal,
)


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _assert_strict_schema(schema: dict[str, object]) -> None:
    definitions = schema.get("$defs")
    assert isinstance(definitions, dict)
    for node in _walk(schema):
        if "$ref" in node:
            ref = node["$ref"]
            assert isinstance(ref, str) and ref.startswith("#/$defs/")
            resolved: object = schema
            for part in ref[2:].split("/"):
                assert isinstance(resolved, dict) and part in resolved
                resolved = resolved[part]
        assert "default" not in node
        assert "oneOf" not in node
        if node.get("type") == "object":
            properties = node.get("properties", {})
            assert isinstance(properties, dict)
            assert node.get("additionalProperties") is False
            assert set(node.get("required", [])) == set(properties)


@pytest.mark.parametrize("kind", ["brainstorm", "decomposition"])
def test_authoring_schema_is_root_resolvable_and_strict(kind: str) -> None:
    schema = authoring_schema(kind)
    _assert_strict_schema(schema)
    assert "proposal" in schema["required"]
    proposal_properties = schema["properties"]["proposal"]["properties"]
    if kind == "brainstorm":
        criteria = proposal_properties["requirement_criteria"]
        assert criteria["type"] == "array"
        assert criteria["maxItems"] == 32
        assert criteria["items"]["properties"] == {
            "requirement": {"type": "string"},
            "criteria": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
        }
    else:
        assert "items" in proposal_properties


def test_brainstorm_wire_criteria_roundtrip_and_legacy_compatibility() -> None:
    job = _make_snapshot()
    proposal = BrainstormProposal(
        turn_id=job.prompt_turn_id,
        problem="Help the team find the right alert channel",
        requirements=("Notify the team",),
        requirement_criteria={"Notify the team": ("Within five minutes", "Include a link")},
        decisions=("Use email alerts",),
        assumptions=("Email is available",),
        open_questions=("Who receives alerts?",),
    )
    wire = proposal.model_dump(mode="json")
    wire["requirement_criteria"] = [
        {"requirement": requirement, "criteria": list(criteria)}
        for requirement, criteria in proposal.requirement_criteria.items()
    ]
    decoded = proposal_from_output({"proposal": wire}, job)
    assert decoded == proposal

    legacy = proposal.model_dump(mode="json")
    assert proposal_from_output({"proposal": legacy}, job) == proposal

    empty = proposal.model_dump(mode="json")
    empty["requirement_criteria"] = [
        {"requirement": "Notify the team", "criteria": []}
    ]
    assert proposal_from_output({"proposal": empty}, job).requirement_criteria == {
        "Notify the team": ()
    }
    empty["requirement_criteria"] = []
    assert proposal_from_output({"proposal": empty}, job).requirement_criteria == {}


@pytest.mark.parametrize(
    "criteria",
    [
        [{"requirement": "Need", "criteria": ["a"]}, {"requirement": "Need", "criteria": ["b"]}],
        [{"requirement": "Need", "criteria": ["a"], "extra": True}],
        [{"requirement": "Need"}],
        [{"requirement": "Need", "criteria": [123]}],
        [{"requirement": "Need", "criteria": [""]}],
        [{"requirement": "Need", "criteria": ["criterion"] * 65}],
        [
            {"requirement": f"Requirement {index}", "criteria": []}
            for index in range(33)
        ],
    ],
)
def test_invalid_wire_criteria_are_rejected(criteria: list[object]) -> None:
    job = _make_snapshot()
    wire = BrainstormProposal(
        turn_id=job.prompt_turn_id,
        problem="Problem",
        requirements=("Need",),
    ).model_dump(mode="json")
    wire["requirement_criteria"] = criteria
    with pytest.raises(ProtocolError, match="invalid authoring proposal"):
        proposal_from_output({"proposal": wire}, job)


def test_wire_criteria_still_enforce_domain_constraints_and_turn_identity() -> None:
    job = _make_snapshot()
    wire = BrainstormProposal(
        turn_id=job.prompt_turn_id,
        problem="Problem",
        requirements=("Need",),
    ).model_dump(mode="json")
    wire["requirement_criteria"] = [{"requirement": "Other", "criteria": ["criterion"]}]
    with pytest.raises(ProtocolError, match="invalid authoring proposal"):
        proposal_from_output({"proposal": wire}, job)
    wire["requirement_criteria"] = []
    wire["turn_id"] = str(uuid4())
    with pytest.raises(ProtocolError, match="foreign authoring turn"):
        proposal_from_output({"proposal": wire}, job)


def test_decomposition_output_conversion_is_unchanged() -> None:
    job, requirement = _make_decomposition_snapshot()
    wire = _make_valid_decomposition_proposal(job, requirement)
    result = proposal_from_output({"proposal": wire}, job)
    assert result == DecompositionProposal.model_validate(wire)
