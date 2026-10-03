"""Brief document validation and selection rules."""

from uuid import uuid4

import pytest
from forge.domain.epic_brief import BriefContent, BriefRequirement, EpicCreateRequest
from pydantic import ValidationError


def test_brief_content_preserves_order_and_rejects_duplicate_requirement_ids() -> None:
    identity = uuid4()
    content = BriefContent(
        problem="Problem",
        outcomes=["First", "Second"],
        requirements=[
            BriefRequirement(requirement_id=identity, text="Need", acceptance_criteria=["A", "B"])
        ],
    )
    assert content.outcomes == ["First", "Second"]
    assert content.requirements[0].acceptance_criteria == ["A", "B"]
    with pytest.raises(ValidationError):
        BriefContent(requirements=[content.requirements[0], content.requirements[0]])


@pytest.mark.parametrize("bad", ["\x00", "Bearer abcdefghijklmnop", "a" * 5001])
def test_unrepresentable_or_unsafe_text_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        BriefContent(outcomes=[bad])


def test_document_and_title_byte_bounds() -> None:
    with pytest.raises(ValidationError):
        EpicCreateRequest(project_id=uuid4(), title="é" * 129)
    with pytest.raises(ValidationError):
        BriefContent(outcomes=["x" * 5000] * 27)
    assert BriefContent(outcomes=["line\n\tUnicode é"]).outcomes == ["line\n\tUnicode é"]
