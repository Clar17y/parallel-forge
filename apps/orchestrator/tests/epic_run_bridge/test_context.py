"""Focused contract for bounded, base-bound epic task context."""

import json
from uuid import UUID

import pytest
from forge.application.services.epic_run_bridge import build_task_context
from forge.domain.epic_brief import BriefContent, BriefRequirement
from forge.domain.epic_items import ItemInput, make_snapshot
from forge.domain.epic_run_bridge import DependencyEvidence, LaunchRequest
from pydantic import ValidationError


def test_context_contains_selected_evidence_and_actual_base_only():
    requirement = UUID(int=10)
    item_id = UUID(int=20)
    brief = BriefContent(
        problem="Unrelated long conversation must stay out",
        decisions=["Use PostgreSQL"],
        requirements=[
            BriefRequirement(
                requirement_id=requirement, text="Deliver work", acceptance_criteria=["Pass"]
            ),
            BriefRequirement(
                requirement_id=UUID(int=11), text="Unrelated", acceptance_criteria=["Skip"]
            ),
        ],
    )
    items, _ = make_snapshot(
        UUID(int=30),
        [
            ItemInput(
                item_id=item_id,
                disposition="required",
                ordinal=0,
                title="Do work",
                outcome="Done",
                acceptance_criteria=["Checked"],
                source_requirement_ids=[requirement],
            )
        ],
        UUID(int=40),
        "a" * 64,
    )
    body, digest = build_task_context(
        execution_id=UUID(int=50),
        epic_id=UUID(int=1),
        expected_epic_version=5,
        actual_epic_version=5,
        brief_revision_id=UUID(int=40),
        brief_digest="a" * 64,
        graph_revision_id=UUID(int=30),
        graph_digest="b" * 64,
        item=items[0],
        brief=brief,
        base_ref="refs/heads/main",
        base_sha="c" * 40,
        dependency_evidence=[],
        owner_override=False,
        override_note=None,
        blocker_codes=[],
    )
    assert "Deliver work" in body
    assert "Use PostgreSQL" in body
    assert "Unrelated" not in body
    assert "conversation" not in body
    assert "c" * 40 in body
    assert len(digest) == 64


def test_client_cannot_submit_proof_or_ambiguous_override():
    data = {
        "expected_epic_version": 5,
        "brief_revision_id": UUID(int=1),
        "brief_digest": "a" * 64,
        "graph_revision_id": UUID(int=2),
        "graph_digest": "b" * 64,
        "item_id": UUID(int=3),
    }
    with pytest.raises(ValidationError):
        LaunchRequest.model_validate({**data, "dependency_evidence": [{"status": "verified"}]})
    with pytest.raises(ValidationError):
        LaunchRequest.model_validate({**data, "owner_override": "yes"})
    with pytest.raises(ValidationError):
        DependencyEvidence(item_id=UUID(int=3), status="verified")
    with pytest.raises(ValueError, match="credential"):
        LaunchRequest.model_validate({**data, "override_note": "password=synthetic_placeholder"})


def test_context_preserves_complete_criteria_and_binds_owner_action():
    requirement_id = UUID(int=1)
    criterion = "must pass " + "x" * 3000
    requirement_criterion = "requirement " + "y" * 3000
    brief = BriefContent(
        requirements=[
            BriefRequirement(
                requirement_id=requirement_id,
                text="Need",
                acceptance_criteria=[requirement_criterion],
            )
        ]
    )
    items, _ = make_snapshot(
        UUID(int=2),
        [
            ItemInput(
                item_id=UUID(int=3),
                disposition="deferred",
                ordinal=0,
                title="Work",
                outcome="Done",
                acceptance_criteria=[criterion],
                source_requirement_ids=[requirement_id],
            )
        ],
        UUID(int=4),
        "a" * 64,
    )
    arguments = {
        "execution_id": UUID(int=5),
        "epic_id": UUID(int=6),
        "expected_epic_version": 4,
        "actual_epic_version": 5,
        "brief_revision_id": UUID(int=4),
        "brief_digest": "a" * 64,
        "graph_revision_id": UUID(int=2),
        "graph_digest": "b" * 64,
        "item": items[0],
        "brief": brief,
        "base_ref": "refs/heads/main",
        "base_sha": "c" * 40,
        "dependency_evidence": [],
        "owner_override": True,
        "override_note": "Owner accepts warning",
        "blocker_codes": ["epic_version_stale", "item_deferred"],
    }
    body, digest = build_task_context(**arguments)
    parsed = json.loads(body)
    assert parsed["context_digest"] == digest
    assert parsed["execution_id"] == str(UUID(int=5))
    assert parsed["item_disposition"] == "deferred"
    assert parsed["owner_override"] is True
    assert parsed["expected_epic_version"] == 4
    assert parsed["actual_epic_version"] == 5
    assert parsed["blocker_codes"] == ["epic_version_stale", "item_deferred"]
    assert parsed["item"]["acceptance_criteria"] == [criterion]
    assert parsed["requirements"][0]["acceptance_criteria"] == [requirement_criterion]
    changed = {**arguments, "owner_override": False, "override_note": None}
    assert build_task_context(**changed)[1] != digest
