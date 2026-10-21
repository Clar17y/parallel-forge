"""Validated shared workflow preference value model and plan material questions."""

from __future__ import annotations

import hashlib
import json

import pytest
from forge.domain.plan import ScopedPlanOutput, decode_plan_output
from forge.domain.workflow import WorkflowPreferences


def test_workflow_preferences_defaults_are_ordinary() -> None:
    assert WorkflowPreferences() == WorkflowPreferences(
        review_plan_first=False, delegation="auto", optional_review="auto"
    )
    assert WorkflowPreferences.DEFAULT.to_dict() == {
        "review_plan_first": False,
        "delegation": "auto",
        "optional_review": "auto",
    }


def test_workflow_preferences_round_trip_and_reject_unknown_values() -> None:
    parsed = WorkflowPreferences.from_mapping(
        {"review_plan_first": True, "delegation": "requested", "optional_review": "skip"}
    )
    assert parsed.review_plan_first is True
    assert parsed.delegation == "requested"
    assert WorkflowPreferences.from_mapping(parsed.to_dict()) == parsed
    assert WorkflowPreferences.from_mapping(None) is None
    with pytest.raises((TypeError, ValueError)):
        WorkflowPreferences.from_mapping({"delegation": "sometimes"})
    with pytest.raises((TypeError, ValueError)):
        WorkflowPreferences.from_mapping({"review_plan_first": "yes"})
    with pytest.raises((TypeError, ValueError)):
        WorkflowPreferences.from_mapping({"unexpected": True})


def _scoped_plan_bytes(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "summary": "Do it",
        "assumptions": [],
        "affected_components": ["apps"],
        "steps": ["Implement"],
        "required_checks": ["unit"],
        "risks": ["Risk"],
        "security_considerations": [],
        "dependency_changes": [],
        "owned_paths": ["apps"],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_legacy_scoped_plan_without_material_questions_decodes_with_same_digest() -> None:
    """Retained plan bytes keep their exact digest after the schema addition."""
    from forge.domain.plan import plan_digest_bytes

    legacy = _scoped_plan_bytes()
    plan = decode_plan_output(legacy)
    assert isinstance(plan, ScopedPlanOutput)
    assert plan.material_questions == ()
    # Re-serializing a question-free plan must reproduce the legacy digest input.
    expected_legacy_bytes = json.dumps(json.loads(legacy), separators=(",", ":")).encode()
    assert (
        hashlib.sha256(plan_digest_bytes(plan)).hexdigest()
        == hashlib.sha256(expected_legacy_bytes).hexdigest()
    )
    assert b"material_questions" not in plan_digest_bytes(plan)


def test_scoped_plan_material_questions_are_explicit_and_bounded() -> None:
    from forge.domain.plan import plan_digest_bytes

    plan = decode_plan_output(_scoped_plan_bytes(material_questions=["Which database?"]))
    assert isinstance(plan, ScopedPlanOutput)
    assert plan.material_questions == ("Which database?",)
    assert b"Which database?" in plan_digest_bytes(plan)
    with pytest.raises(ValueError):
        decode_plan_output(_scoped_plan_bytes(material_questions=["  "]))
    with pytest.raises(ValueError):
        decode_plan_output(_scoped_plan_bytes(material_questions=["Q?", "Q?"]))
