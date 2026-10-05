"""Domain unit tests for epic decomposition proposals and graph validation."""

from uuid import uuid4

import pytest
from forge.domain.epic_brief import BriefContent, BriefRequirement
from forge.domain.epic_decomposition import (
    DecompositionProposal,
    DecompositionValidationError,
    require_brief_sources,
    validate_decomposition_proposal,
)
from forge.domain.epic_items import GraphValidationError, ItemInput, validate_graph
from pydantic import ValidationError


def _make_brief(req_ids: list | None = None) -> tuple[BriefContent, str]:
    if req_ids is None:
        req_ids = [uuid4()]
    requirements = [
        BriefRequirement(requirement_id=rid, text=f"Requirement {i}", acceptance_criteria=["Criteria 1"])
        for i, rid in enumerate(req_ids)
    ]
    brief = BriefContent(
        problem="Test problem description",
        outcomes=["Observable outcome 1"],
        scope=["In scope"],
        exclusions=["Out of scope"],
        requirements=requirements,
        decisions=["Decision 1"],
        assumptions=["Assumption 1"],
        open_questions=["Open question 1?"],
    )
    brief.require_adoptable()
    from forge.domain.operation import canonical_digest
    digest = canonical_digest(brief.model_dump(mode="json"))
    return brief, digest


def test_valid_decomposition_proposal_round_trip() -> None:
    req_id = uuid4()
    brief, brief_digest = _make_brief([req_id])
    item_id_1 = uuid4()
    item_id_2 = uuid4()

    item1 = ItemInput(
        item_id=item_id_1,
        disposition="required",
        ordinal=0,
        title="First work item",
        outcome="First outcome achieved",
        acceptance_criteria=["Criterion 1a", "Criterion 1b"],
        source_requirement_ids=[req_id],
        dependency_item_ids=[],
    )
    item2 = ItemInput(
        item_id=item_id_2,
        disposition="required",
        ordinal=1,
        title="Second work item",
        outcome="Second outcome achieved",
        acceptance_criteria=["Criterion 2a"],
        source_requirement_ids=[req_id],
        dependency_item_ids=[item_id_1],
    )

    epic_id = uuid4()
    project_id = uuid4()
    brief_revision_id = uuid4()
    turn_id = uuid4()

    proposal = DecompositionProposal(
        turn_id=turn_id,
        epic_id=epic_id,
        project_id=project_id,
        brief_revision_id=brief_revision_id,
        brief_digest=brief_digest,
        items=(item1, item2),
        summary="Proposed decomposition into two items",
        problem="Proposed decomposition into two items",
        assumptions=tuple(brief.assumptions),
        open_questions=tuple(brief.open_questions),
    )

    validated = validate_decomposition_proposal(proposal, brief)
    assert validated == proposal
    assert isinstance(validated, DecompositionProposal)
    assert proposal.schema_version == 1
    assert proposal.digest is not None
    assert len(proposal.digest) == 64
    assert proposal.assumptions == ("Assumption 1",)
    assert proposal.open_questions == ("Open question 1?",)


def test_require_brief_sources_accepts_valid_and_rejects_missing() -> None:
    req_id = uuid4()
    unknown_req_id = uuid4()
    brief, _ = _make_brief([req_id])
    valid_item = ItemInput(
        item_id=uuid4(),
        disposition="required",
        ordinal=0,
        title="Valid item",
        outcome="Outcome",
        acceptance_criteria=["Criterion"],
        source_requirement_ids=[req_id],
        dependency_item_ids=[],
    )
    require_brief_sources([valid_item], brief)

    invalid_item = ItemInput(
        item_id=uuid4(),
        disposition="required",
        ordinal=1,
        title="Invalid item",
        outcome="Outcome",
        acceptance_criteria=["Criterion"],
        source_requirement_ids=[unknown_req_id],
        dependency_item_ids=[],
    )
    with pytest.raises(
        DecompositionValidationError,
        match=f"^source requirement is missing from accepted brief: {unknown_req_id}$",
    ):
        require_brief_sources([valid_item, invalid_item], brief)


def test_reject_proposal_with_unknown_requirement_reference() -> None:
    req_id_1 = uuid4()
    unknown_req_id = uuid4()
    brief, brief_digest = _make_brief([req_id_1])

    item = ItemInput(
        item_id=uuid4(),
        disposition="required",
        ordinal=0,
        title="Item referencing nonexistent req",
        outcome="Outcome",
        acceptance_criteria=["Criterion"],
        source_requirement_ids=[unknown_req_id],
        dependency_item_ids=[],
    )

    proposal = DecompositionProposal(
        turn_id=uuid4(),
        epic_id=uuid4(),
        project_id=uuid4(),
        brief_revision_id=uuid4(),
        brief_digest=brief_digest,
        items=(item,),
        summary="Summary",
        problem="Summary",
        assumptions=tuple(brief.assumptions),
        open_questions=tuple(brief.open_questions),
    )

    with pytest.raises(DecompositionValidationError, match="source requirement is missing from accepted brief"):
        validate_decomposition_proposal(proposal, brief)


def test_reject_proposal_attempting_to_resolve_pending_choices() -> None:
    req_id = uuid4()
    _brief, brief_digest = _make_brief([req_id])
    item = ItemInput(
        item_id=uuid4(),
        disposition="required",
        ordinal=0,
        title="Item",
        outcome="Outcome",
        acceptance_criteria=["Criterion"],
        source_requirement_ids=[req_id],
        dependency_item_ids=[],
    )

    with pytest.raises((ValueError, ValidationError), match="cannot resolve pending choices"):
        DecompositionProposal(
            turn_id=uuid4(),
            epic_id=uuid4(),
            project_id=uuid4(),
            brief_revision_id=uuid4(),
            brief_digest=brief_digest,
            items=(item,),
            summary="Summary",
            problem="Summary",
            resolved_turn_ids=(uuid4(),),
        )


def test_reject_cross_graph_cycle_and_self_edge() -> None:
    item_id_1 = uuid4()
    item_id_2 = uuid4()
    req_id = uuid4()
    _brief, _brief_digest = _make_brief([req_id])

    # Self-edge
    with pytest.raises(GraphValidationError, match="graph edge is invalid"):
        validate_graph([
            ItemInput(
                item_id=item_id_1,
                disposition="required",
                ordinal=0,
                title="Self cycle",
                outcome="Outcome",
                acceptance_criteria=["Criterion"],
                source_requirement_ids=[req_id],
                dependency_item_ids=[item_id_1],
            )
        ])

    # Mutual cycle
    with pytest.raises(GraphValidationError, match="graph contains cycle"):
        validate_graph([
            ItemInput(
                item_id=item_id_1,
                disposition="required",
                ordinal=0,
                title="Node 1",
                outcome="Outcome",
                acceptance_criteria=["Criterion"],
                source_requirement_ids=[req_id],
                dependency_item_ids=[item_id_2],
            ),
            ItemInput(
                item_id=item_id_2,
                disposition="required",
                ordinal=1,
                title="Node 2",
                outcome="Outcome",
                acceptance_criteria=["Criterion"],
                source_requirement_ids=[req_id],
                dependency_item_ids=[item_id_1],
            ),
        ])


def test_required_cannot_depend_on_deferred_at_adoption() -> None:
    item_id_req = uuid4()
    item_id_def = uuid4()
    req_id = uuid4()

    items = [
        ItemInput(
            item_id=item_id_def,
            disposition="deferred",
            ordinal=0,
            title="Deferred item",
            outcome="Outcome",
            acceptance_criteria=["Criterion"],
            source_requirement_ids=[req_id],
            dependency_item_ids=[],
        ),
        ItemInput(
            item_id=item_id_req,
            disposition="required",
            ordinal=1,
            title="Required item depending on deferred",
            outcome="Outcome",
            acceptance_criteria=["Criterion"],
            source_requirement_ids=[req_id],
            dependency_item_ids=[item_id_def],
        ),
    ]

    # Non-adoption validation permits this during editing
    validate_graph(items, adoption=False)

    # Adoption validation strictly forbids this
    with pytest.raises(GraphValidationError, match="required item depends on deferred item"):
        validate_graph(items, adoption=True)


def test_deferred_can_depend_on_required_at_adoption() -> None:
    item_id_req = uuid4()
    item_id_def = uuid4()
    req_id = uuid4()

    items = [
        ItemInput(
            item_id=item_id_req,
            disposition="required",
            ordinal=0,
            title="Required item",
            outcome="Outcome",
            acceptance_criteria=["Criterion"],
            source_requirement_ids=[req_id],
            dependency_item_ids=[],
        ),
        ItemInput(
            item_id=item_id_def,
            disposition="deferred",
            ordinal=1,
            title="Deferred item depending on required",
            outcome="Outcome",
            acceptance_criteria=["Criterion"],
            source_requirement_ids=[req_id],
            dependency_item_ids=[item_id_req],
        ),
    ]

    # Valid in both modes
    validate_graph(items, adoption=False)
    validate_graph(items, adoption=True)
